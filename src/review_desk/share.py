"""Share mode: let devices on the local network open the desk, for 4 hours at a time.

Off unless the desk is started with `serve --share` (or REVIEW_DESK_SHARE=1). Even then nothing is reachable from another
machine until a share window is switched on, from the page's Share button or `POST /api/share` on localhost. The window is
held in memory only, so a restarted desk is unshared. While it is open, a peer must have an RFC 1918 address (`is_lan`)
and must have reached one of the addresses this machine had when the window opened (every private address on every
interface, VPN tunnels and Docker or VM bridges included), so moving to a network that hands out different addresses ends
the sharing; one that hands out the same address does not, which is why the sleep rule below also exists. The Host header must name an address this desk answers on (see server.py), plus this machine's `.local` name when
`--share-mdns` was given (off by default: anyone on the network can claim a `.local` name). The window also ends if the
machine sleeps (the monotonic clock stops during sleep, the wall clock doesn't). This module holds the window and works
out which URLs to tell the human about.
"""
from __future__ import annotations

import ipaddress
import math
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

SHARE_SECONDS = 4 * 3600
BUMP_BELOW = SHARE_SECONDS - 60  # a click with less than 3:59 left resets the window to 4:00:00; with 3:59 or more it ends it
URL_CACHE_SECONDS = 30  # the page asks every few seconds; interface lookups shell out
SLEEP_SLACK = 30  # the wall clock running this far ahead of the monotonic one means the machine slept
PLATFORM = sys.platform  # module attribute so tests can pick a platform
AVAHI_SOCKET = Path("/run/avahi-daemon/socket")
LAN_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]  # RFC 1918, not is_private (it admits documentation ranges)
MDNS_NAME = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?\.local")


class Share:
    def __init__(self, enabled: bool = False, clock=time.monotonic, wall=time.time, mdns: bool = False):
        self.enabled = enabled
        self._clock, self._wall = clock, wall
        self._until = None
        self._opened = None  # (monotonic, wall) when the window opened
        self._ips: frozenset = frozenset()  # this machine's addresses when it opened
        self._lock = threading.Lock()
        self._found_at = None
        self._interfaces: list[tuple[str, str]] = []
        self.mdns = mdns_name() if enabled and mdns else None  # opt-in (--share-mdns); looked up once: the Host check reads it on every request

    def remaining(self) -> float:
        """Seconds left in the window; 0 when it is closed, expired, or sharing is not enabled."""
        if not self.enabled or self._until is None:
            return 0.0
        mono_elapsed, wall_elapsed = self._clock() - self._opened[0], self._wall() - self._opened[1]
        if wall_elapsed - mono_elapsed > SLEEP_SLACK:  # the machine slept: it may be on another network now
            self._until = None
            return 0.0
        return max(0.0, self._until - self._clock())

    def active(self) -> bool:
        return self.remaining() > 0

    def on(self) -> None:
        if not self.enabled:
            raise ValueError("sharing is not enabled")
        found = list_interfaces()  # outside the lock: it runs a subprocess
        with self._lock:
            self._until = self._clock() + SHARE_SECONDS
            self._opened = (self._clock(), self._wall())
            self._ips = frozenset(ip for _, ip in found)
            self._interfaces, self._found_at = found, self._clock()

    def off(self) -> None:
        with self._lock:
            self._until = None

    def toggle(self) -> None:
        """Closed or nearly spent: open (or reset to) a full window. Just started or just reset: close it."""
        if self.remaining() >= BUMP_BELOW:
            self.off()
        else:
            self.on()

    def admits(self, peer_ip: str, local_ip: str) -> bool:
        """A peer may connect: a window is open, the peer is on a private network, and it reached an address this machine
        had when the window opened."""
        return self.active() and is_lan(peer_ip) and local_ip in self._ips

    def urls(self, port: int) -> list[dict]:
        """One URL per non-loopback interface, plus the mDNS name when there is one. Only while a window is open."""
        if not self.active():
            return []
        now = self._clock()
        if self._found_at is None or now - self._found_at > URL_CACHE_SECONDS:
            self._interfaces, self._found_at = list_interfaces(), now
        out = [{"label": "mDNS", "url": f"http://{self.mdns}:{port}/"}] if self.mdns else []
        out += [{"label": name or "network", "url": f"http://{ip}:{port}/"} for name, ip in self._interfaces
                if ip in self._ips]  # an interface that came up after the window opened would be refused: don't offer it
        return out


# ---- who may connect ------------------------------------------------------------------------------

def is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def is_lan(ip: str) -> bool:
    """An RFC 1918 address: the local network, not the internet."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in LAN_NETS)


def bind_address(share: bool) -> str:
    return "0.0.0.0" if share else "127.0.0.1"


# ---- where this machine can be reached ------------------------------------------------------------

def parse_ip_addr(text: str) -> list[tuple[str, str]]:
    """(interface, ipv4) pairs from `ip -o -4 addr show`."""
    return [(m[1], m[2]) for m in re.finditer(r"^\d+:\s+(\S+)\s+inet (\d+\.\d+\.\d+\.\d+)/", text, re.M)]


def parse_ifconfig(text: str) -> list[tuple[str, str]]:
    """(interface, ipv4) pairs from `ifconfig` (macOS, BSD)."""
    out, name = [], None
    for line in text.splitlines():
        m = re.match(r"^([\w.-]+):", line)
        if m:
            name = m[1]
            continue
        m = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+)\b", line)
        if m and name:
            out.append((name, m[1]))
    return out


def usable(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep RFC 1918 addresses, once each: a peer has to be on a private network to get in, so others are no use in a URL."""
    seen, out = set(), []
    for name, ip in pairs:
        if not is_lan(ip) or ip in seen:
            continue
        seen.add(ip)
        out.append((name, ip))
    return out


def list_interfaces() -> list[tuple[str, str]]:
    for cmd, parse in ((["ip", "-o", "-4", "addr", "show"], parse_ip_addr), (["ifconfig"], parse_ifconfig)):
        if not shutil.which(cmd[0]):
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            continue
        found = usable(parse(r.stdout))
        if found:
            return found
    return usable([("", ip)]) if (ip := _routed_ip()) else []


def _routed_ip() -> str | None:
    """The address the OS would use to reach the network. A UDP connect sends nothing; it only picks a route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def mdns_name() -> str | None:
    """`<name>.local` if this machine advertises one: macOS always does (its LocalHostName); on Linux only with avahi running."""
    name = None
    if PLATFORM == "darwin":
        try:
            r = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode == 0:
            name = r.stdout.strip()
    elif AVAHI_SOCKET.exists():
        name = socket.gethostname().split(".")[0]
    name = f"{name}.local".lower() if name else ""
    return name if MDNS_NAME.fullmatch(name) else None  # the name goes into the Host allowlist and into URLs


def seconds_left(share: Share) -> int:
    return math.ceil(share.remaining())
