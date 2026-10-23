"""The desk: one loopback HTTP server for every registered packet.

Routes (anything else is 404):
  GET /                                  index of registered packets
  GET /p/<pid>/                          packet page, rendered live from review.toml and the draft files
  GET /p/<pid>/raw/<item_id>             an item's draft file as text/plain (only files the manifest names)
  GET /api/health                        {"app": "review-desk"}
  GET /api/p/<pid>/state                 the packet's review-decisions.json
  GET /api/p/<pid>/history               the packet's review-history.json (notes archived when a revision cleared them)
  GET /api/p/<pid>/changes               the packet's review-changes.json items: {item_id: [{at, kind, detail?}]}, oldest first
                                         (external changes: draft file, manifest entry, agent decision/note writes)
  GET /api/p/<pid>/drafts                {item_id: version} for every item's rendered draft (the page polls this);
                                         X-Layout-Version says whether the manifest-derived page changed; with
                                         X-Static-Version ("<css>.<js>" fingerprints) lets an open page swap in a
                                         changed stylesheet or script without reloading (hot swap)
  GET /api/p/<pid>/drafts/<item_id>      {version, html}: one rendered draft, so the page can swap it in place
  PUT /api/p/<pid>/items/<item_id>       merge {"decision"?, "note"?, "ifDecision"?} into one item
                                         (409 and no change if ifDecision no longer matches the item's decision)
  GET /api/share                         share mode state: {enabled, active, remaining, seconds, bumpBelow, canControl, urls}
  POST /api/share                        {"action": "on"|"off"|"toggle"}: open, close or toggle the 4-hour share window
                                         (localhost only; 403 unless the desk was started with --share)
  GET /static/page.css|page.js

Security rules (each has a test in tests/test_server.py, tests/test_share.py or tests/test_changes.py):
  - bind 127.0.0.1 only (0.0.0.0 only with --share);
  - Host must be exactly 127.0.0.1:<port> or localhost:<port> (DNS rebinding), on every request; with --share also
    <address the connection arrived on>:<port> and <this machine's mDNS name, only with --share-mdns>:<port>, for a non-loopback peer only;
  - a peer that isn't loopback is dropped at accept time (no thread, no response) unless a share window is open, its
    address is RFC 1918, and it reached an address this machine had when the window opened;
  - concurrent connections are capped in two separate pools (MAX_LOOPBACK_CONNECTIONS, MAX_LAN_CONNECTIONS) and each is
    hung up after REQUEST_DEADLINE seconds, so no peer on the network can exhaust the desk's threads by opening or
    trickling, or crowd out the owner (a process on this machine can still fill the owner's own pool);
  - the share control route accepts only a loopback peer with a loopback Host, so no LAN client can open, extend or end a window;
  - writes need X-Review-Desk: 1 (a cross-origin form or no-cors fetch can't set it), JSON content type,
    and a same-origin Origin when one is sent;
  - only registered packets, item ids from the manifest, and manifest-validated files are reachable;
    path segments must match [a-z0-9-]+ with no decoding, so encoded traversal never routes;
  - request bodies are capped at MAX_BODY;
  - pages escape untrusted text (render.py) and carry a strict CSP.
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import urlsplit

from . import changes
from . import decisions as dec
from . import render
from . import share as sharing
from .fsutil import read_regular
from .manifest import ManifestError, load
from .registry import Registry, RegistryError
from .summary import summarize_entry

MAX_LOOPBACK_CONNECTIONS = 256  # separate pools: nothing on the network can use up the owner's
MAX_LAN_CONNECTIONS = 16
THREAD_STACK = 8 * 1024 * 1024  # musl (Alpine) gives threads 128 KB, and deeply nested JSON in a packet file then overflows the C stack: a crash, not a RecursionError
REQUEST_DEADLINE = 30  # seconds a connection may live; the per-read timeout alone lets a slow sender hold a thread forever
MAX_BODY = 64 * 1024
DRAIN_LIMIT = 16 * 1024 * 1024
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
SEG = r"([a-z0-9][a-z0-9-]{0,127})"
ROUTES_GET = [
    (re.compile(r"^/$"), "index"),
    (re.compile(rf"^/p/{SEG}/$"), "packet"),
    (re.compile(rf"^/p/{SEG}/raw/{SEG}$"), "raw"),
    (re.compile(r"^/api/health$"), "health"),
    (re.compile(r"^/api/share$"), "share"),
    (re.compile(rf"^/api/p/{SEG}/state$"), "state"),
    (re.compile(rf"^/api/p/{SEG}/history$"), "history"),
    (re.compile(rf"^/api/p/{SEG}/changes$"), "changes"),
    (re.compile(rf"^/api/p/{SEG}/drafts$"), "drafts"),
    (re.compile(rf"^/api/p/{SEG}/drafts/{SEG}$"), "draft"),
    (re.compile(r"^/static/(page\.css|page\.js)$"), "static"),
    (re.compile(r"^/favicon\.ico$"), "favicon"),
]
ROUTE_PUT = re.compile(rf"^/api/p/{SEG}/items/{SEG}$")
STATIC_TYPES = {"page.css": "text/css; charset=utf-8", "page.js": "text/javascript; charset=utf-8"}


class HttpError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code, self.msg = code, msg


class Handler(BaseHTTPRequestHandler):
    server_version = "review-desk"
    sys_version = ""
    timeout = 15  # per-connection socket timeout: a stalled client can't hold a thread forever

    # ---- plumbing -------------------------------------------------------------------------------
    def _send(self, code, body, ctype, headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code, obj, headers=None):
        self._send(code, json.dumps(obj), "application/json", headers)

    def _err(self, code, msg):
        if self.path.startswith("/api/"):
            self._json(code, {"error": msg})
        else:
            self._send(code, render.error_page(f"{code}", msg), "text/html; charset=utf-8")

    def log_message(self, *a):
        pass

    @property
    def port(self):
        return self.server.server_address[1]

    def _loopback_hosts(self):
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def _local_ip(self):
        return self.request.getsockname()[0]

    def _allowed_hosts(self):
        hosts = self._loopback_hosts()
        share = self.server.share
        # IP literals and the machine's own mDNS name, for a peer on the network only: a loopback peer (this machine's own
        # browser, or a rebound page in it) uses the loopback names, so a `.local` name someone else may have claimed never works
        if share.enabled and not sharing.is_loopback(self.client_address[0]):
            hosts.add(f"{self._local_ip()}:{self.port}")
            if share.mdns:
                hosts.add(f"{share.mdns}:{self.port}")
        return hosts

    def _check_peer(self):
        ip = self.client_address[0]
        if not sharing.is_loopback(ip) and not self.server.share.admits(ip, self._local_ip()):
            self.close_connection = True  # (the server already dropped such peers at accept; this covers a window that closed since)
            raise HttpError(403, "not shared")

    def _can_control(self):
        hosts = self.headers.get_all("Host") or []
        return (self.server.share.enabled and sharing.is_loopback(self.client_address[0])
                and len(hosts) == 1 and hosts[0].strip() in self._loopback_hosts())

    def _check_host(self):
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0].strip() not in self._allowed_hosts():
            raise HttpError(403, "bad Host header")

    def _dispatch(self, fn):
        try:
            self._check_peer()
            self._check_host()
            fn()
        except HttpError as e:
            self._err(e.code, e.msg)
        except Exception as e:  # last resort: never leak a traceback
            self._err(500, f"internal error: {type(e).__name__}")

    def _packet(self, pid):
        entry = self.server.registry.get(pid)
        if entry is None:
            raise HttpError(404, "unknown packet")
        try:
            return load(entry.path)
        except ManifestError as e:
            raise HttpError(500, f"manifest error: {e}")

    # ---- methods --------------------------------------------------------------------------------
    def do_GET(self):
        self._dispatch(self._get)

    def do_HEAD(self):
        self._dispatch(self._get)

    def do_PUT(self):
        self._dispatch(self._put)

    def do_POST(self):
        self._dispatch(self._post)

    def _not_allowed(self):
        def refuse():
            raise HttpError(405, "method not allowed")
        self._dispatch(refuse)

    do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    def _get(self):
        path = urlsplit(self.path).path
        for rx, name in ROUTES_GET:
            m = rx.match(path)
            if m:
                return getattr(self, f"_get_{name}")(*m.groups())
        raise HttpError(404, "not found")

    def _get_index(self):
        rows = [summarize_entry(e) for e in self.server.registry.entries()]
        self._send(200, render.index_page(rows), "text/html; charset=utf-8")

    def _get_packet(self, pid):
        p = self._packet(pid)
        self._send(200, render.packet_page(pid, p), "text/html; charset=utf-8")

    def _get_raw(self, pid, item_id):
        p = self._packet(pid)
        item = p.item(item_id)
        fp = p.file_path(item) if item else None
        if fp is None or not fp.is_file():
            raise HttpError(404, "no such item file")
        try:
            data = read_regular(fp)
        except OSError:  # became a FIFO or vanished since the check
            raise HttpError(404, "no such item file")
        self._send(200, data, "text/plain; charset=utf-8")

    def _get_health(self):
        self._json(200, {"app": "review-desk", "port": self.port})

    def _share_state(self):
        share = self.server.share
        return {"enabled": share.enabled, "active": share.active(), "remaining": sharing.seconds_left(share),
                "seconds": sharing.SHARE_SECONDS, "bumpBelow": sharing.BUMP_BELOW, "canControl": self._can_control(),
                "urls": share.urls(self.port)}

    def _get_share(self):
        self._json(200, self._share_state())

    def _get_state(self, pid):
        p = self._packet(pid)
        try:
            self._json(200, dec.read(p.dir))
        except dec.DecisionsError as e:
            raise HttpError(500, str(e))

    def _get_history(self, pid):
        p = self._packet(pid)
        try:
            self._json(200, dec.read_history(p.dir))
        except dec.DecisionsError as e:
            raise HttpError(500, str(e))

    def _get_changes(self, pid):
        p = self._packet(pid)
        self.server.watcher.touch(self.server.registry.get(pid))  # a page has this packet open: scan it now and keep scanning
        self._json(200, changes.read(p.dir))

    def _get_drafts(self, pid):
        p = self._packet(pid)
        headers = {"X-Layout-Version": render.layout_version(pid, p), "X-Static-Version": static_version()}
        self._json(200, {it.id: render.draft_version(render.draft_html(p, it)) for it in p.items}, headers)

    def _get_draft(self, pid, item_id):
        p = self._packet(pid)
        item = p.item(item_id)
        if item is None:
            raise HttpError(404, "unknown item")
        html = render.draft_html(p, item)
        self._json(200, {"version": render.draft_version(html), "html": html})

    def _get_favicon(self):
        self._send(204, b"", "image/x-icon")

    def _get_static(self, name):
        data = resources.files("review_desk").joinpath("static", name).read_bytes()
        self._send(200, data, STATIC_TYPES[name])

    def _put(self):
        m = ROUTE_PUT.match(urlsplit(self.path).path)
        if not m:
            raise HttpError(404, "not found")
        pid, item_id = m.groups()
        patch = self._json_write()
        p = self._packet(pid)
        if p.item(item_id) is None:
            raise HttpError(404, "unknown item")
        try:
            dec.validate_patch(patch)
        except dec.DecisionsError as e:
            raise HttpError(400, str(e))
        external = self.headers.get("X-Review-Page") != "1"  # the page marks its own writes; anything else is an agent
        try:
            with changes.LOCK:  # the scan must not see this write half done and call it a file edit
                try:
                    before = dec.read(p.dir).get(item_id)
                except dec.DecisionsError:
                    before = None
                rec = dec.merge_item(p.dir, item_id, patch)
                try:
                    changes.record_write(p.dir, item_id, before, rec, external)
                except Exception:  # the change log is derived data: never fail a saved decision over it
                    pass
        except dec.ConflictError as e:  # an ifDecision guard that no longer holds: the item changed under the caller
            raise HttpError(409, str(e))
        except dec.DecisionsError as e:  # unreadable decisions file: refuse rather than overwrite
            raise HttpError(500, str(e))
        self._json(200, rec)

    def _post(self):
        if urlsplit(self.path).path != "/api/share":
            raise HttpError(405, "method not allowed")
        if not self._can_control():
            raise HttpError(403, "sharing is controlled from localhost, and only when the desk runs with --share")
        action = self._json_write()
        action = action.get("action") if isinstance(action, dict) else None
        share = self.server.share
        if action == "on":
            share.on()
        elif action == "off":
            share.off()
        elif action == "toggle":
            share.toggle()
        else:
            raise HttpError(400, 'action must be "on", "off" or "toggle"')
        self._json(200, self._share_state())

    def _json_write(self):
        """The checks every write passes (custom header, same-origin, JSON), then the parsed body."""
        if self.headers.get("X-Review-Desk") != "1":
            raise HttpError(403, "missing X-Review-Desk: 1 header")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in self._allowed_hosts()}:
            raise HttpError(403, "cross-origin write refused")
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            raise HttpError(403, "cross-site write refused")
        if not (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() == "application/json":
            raise HttpError(415, "Content-Type must be application/json")
        body = self._read_body()
        try:
            return json.loads(body)
        except ValueError:
            raise HttpError(400, "body is not JSON")

    def _read_body(self) -> bytes:
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise HttpError(411, "Content-Length required")
        try:
            n = int(raw)
            if n < 0:
                raise ValueError
        except ValueError:
            raise HttpError(400, "bad Content-Length")
        if n > MAX_BODY:
            self.close_connection = True
            self._drain(n)
            raise HttpError(413, f"body over {MAX_BODY} bytes")
        return self.rfile.read(n)

    def _drain(self, n):
        """Discard an oversize body in small chunks (bounded memory) so the client can read the 413."""
        left = min(n, DRAIN_LIMIT)
        try:
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass


def static_version() -> str:
    """`<css>.<js>` fingerprints of the page's stylesheet and script, read fresh each time, so an edit shows up on the next poll."""
    def h(name):
        return hashlib.sha1(resources.files("review_desk").joinpath("static", name).read_bytes()).hexdigest()[:12]
    return f"{h('page.css')}.{h('page.js')}"


def _hang_up(request) -> None:
    try:
        request.shutdown(socket.SHUT_RDWR)  # wakes a handler blocked reading a trickled request
    except OSError:
        pass


class DeskServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, registry: Registry, share: bool = False, bind: str | None = None, mdns: bool = False):
        threading.stack_size(THREAD_STACK)  # for every thread started from here on: handlers, timers, the change watcher
        self.registry = registry
        self.share = sharing.Share(enabled=share, mdns=mdns)
        self.watcher = changes.Watcher(registry)  # `serve` starts its thread; the changes route touches it
        self._slots = threading.Lock()
        self._conns: dict[int, bool] = {}  # id(request) -> held by a non-loopback peer
        super().__init__((bind or sharing.bind_address(share), port), Handler)

    def verify_request(self, request, client_address):
        """Runs in the accept loop, before a thread is spawned: refuse a peer that may not connect, and anyone over the caps."""
        ip = client_address[0]
        lan = not sharing.is_loopback(ip)
        try:
            local = request.getsockname()[0]
        except OSError:  # the peer already reset the connection; an exception here would end serve_forever
            return False
        if lan and not self.share.admits(ip, local):
            return False
        with self._slots:
            held_by_lan = sum(self._conns.values())
            if held_by_lan >= MAX_LAN_CONNECTIONS if lan else len(self._conns) - held_by_lan >= MAX_LOOPBACK_CONNECTIONS:
                return False
            self._conns[id(request)] = lan
        return True

    def process_request_thread(self, request, client_address):
        timer = threading.Timer(REQUEST_DEADLINE, _hang_up, (request,))
        timer.daemon = True
        timer.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()

    def shutdown_request(self, request):
        with self._slots:
            self._conns.pop(id(request), None)
        super().shutdown_request(request)


def make_server(port: int, registry: Registry, share: bool = False, bind: str | None = None, mdns: bool = False) -> DeskServer:
    """`bind` overrides the address (tests only); by default loopback, or all interfaces in share mode. `mdns` (share mode
    only) also accepts, and lists, this machine's `.local` name."""
    return DeskServer(port, registry, share, bind, mdns)


__all__ = ["make_server", "DeskServer", "MAX_BODY", "RegistryError"]
