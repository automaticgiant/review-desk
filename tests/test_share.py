"""Share mode: the 4-hour window, who may connect while it is open, the localhost-only control route, the URLs the
button's tooltip lists, and the `share` command."""
import http.client
import json
import socket
import threading
import time

import pytest

from conftest import Client
from review_desk import cli, share as sh
from review_desk import server as srvmod
from review_desk.server import make_server

IFACES = [("en0", "192.168.1.5"), ("utun3", "10.8.0.2")]
LAN = "192.168.1.5"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ---- the window ------------------------------------------------------------------------------------

def make_share(skew=None):
    """A share whose wall clock follows the fake monotonic one; skew (a one-item list) pushes the wall clock ahead, as a sleep would."""
    c = Clock()
    skew = skew if skew is not None else [0]
    s = sh.Share(enabled=True, clock=c, wall=lambda: 1_700_000_000 + c.t + skew[0])
    return s, c


def test_window_is_closed_until_opened_and_lasts_four_hours():
    s, c = make_share()
    assert not s.active() and s.remaining() == 0
    s.on()
    assert s.remaining() == 4 * 3600
    c.t += 4 * 3600 - 1
    assert s.active()
    c.t += 1
    assert not s.active() and s.remaining() == 0


def test_disabled_share_never_opens():
    s = sh.Share(enabled=False)
    with pytest.raises(ValueError):
        s.on()
    assert not s.active()
    s._until = 10**12  # even a forced deadline doesn't count when sharing isn't enabled
    assert not s.active()


def test_off_closes_the_window():
    s, c = make_share()
    s.on()
    s.off()
    assert not s.active()


def test_toggle_opens_when_closed():
    s, c = make_share()
    s.toggle()
    assert s.remaining() == sh.SHARE_SECONDS


def test_toggle_right_after_a_start_ends_sharing():
    s, c = make_share()
    s.on()
    c.t += 30  # shows 3:59:30: at least 3:59 left
    s.toggle()
    assert not s.active()


def test_toggle_with_less_than_359_left_bumps_back_to_four_hours():
    s, c = make_share()
    s.on()
    c.t += 61  # shows 3:58:59
    s.toggle()
    assert s.remaining() == sh.SHARE_SECONDS


def test_toggle_boundary_is_exactly_359():
    s, c = make_share()
    s.on()
    c.t += 60  # exactly 3:59:00 left: still "3:59 or more", so it ends
    s.toggle()
    assert not s.active()
    s.on()
    c.t += 60.5  # just under 3:59:00
    s.toggle()
    assert s.remaining() == sh.SHARE_SECONDS


def test_toggle_after_a_bump_ends_sharing():
    s, c = make_share()
    s.on()
    c.t += 600
    s.toggle()  # bump
    s.toggle()  # now shows 4:00:00
    assert not s.active()


def test_toggle_on_an_expired_window_opens_a_new_one():
    s, c = make_share()
    s.on()
    c.t += 5 * 3600
    s.toggle()
    assert s.remaining() == sh.SHARE_SECONDS


# ---- who may connect (pure helpers) ------------------------------------------------------------------

@pytest.mark.parametrize("ip,lan", [("192.168.1.50", True), ("10.0.0.7", True), ("172.16.5.5", True), ("172.32.0.1", False),
                                    ("8.8.8.8", False), ("127.0.0.1", False), ("not-an-ip", False), ("", False)])
def test_is_lan_means_private_range_and_not_loopback(ip, lan):
    assert sh.is_lan(ip) is lan


@pytest.mark.parametrize("ip,lo", [("127.0.0.1", True), ("127.5.5.5", True), ("::1", True), ("192.168.1.5", False), ("x", False)])
def test_is_loopback(ip, lo):
    assert sh.is_loopback(ip) is lo


def test_bind_address_is_loopback_unless_sharing():
    assert sh.bind_address(False) == "127.0.0.1"
    assert sh.bind_address(True) == "0.0.0.0"


def test_a_desk_without_share_binds_loopback_only(registry):
    srv = make_server(0, registry)
    try:
        assert srv.server_address[0] == "127.0.0.1" and not srv.share.enabled
    finally:
        srv.server_close()


# ---- finding the URLs --------------------------------------------------------------------------------

IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::1%en0 prefixlen 64 scopeid 0x6
\tinet 192.168.1.5 netmask 0xffffff00 broadcast 192.168.1.255
utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
\tinet 10.8.0.2 --> 10.8.0.2 netmask 0xffffffff
en1: flags=8863<UP> mtu 1500
\tinet 169.254.9.9 netmask 0xffff0000
en2: flags=8863<UP> mtu 1500
\tinet 203.0.113.9 netmask 0xffffff00
"""
IP_ADDR = """1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever
2: eth0    inet 192.168.1.5/24 brd 192.168.1.255 scope global eth0\\       valid_lft forever
3: wg0    inet 10.8.0.2/32 scope global wg0\\       valid_lft forever
"""


def test_parse_ifconfig_pairs_each_address_with_its_interface():
    got = sh.parse_ifconfig(IFCONFIG)
    assert ("en0", "192.168.1.5") in got and ("utun3", "10.8.0.2") in got and ("lo0", "127.0.0.1") in got
    assert ("en0", "fe80::1%en0") not in got


def test_parse_ip_addr():
    assert sh.parse_ip_addr(IP_ADDR) == [("lo", "127.0.0.1"), ("eth0", "192.168.1.5"), ("wg0", "10.8.0.2")]


def test_usable_drops_loopback_link_local_public_and_repeats():
    got = sh.usable(sh.parse_ifconfig(IFCONFIG) + [("en9", "192.168.1.5")])
    assert got == [("en0", "192.168.1.5"), ("utun3", "10.8.0.2")]


def test_list_interfaces_reads_the_tool_output(monkeypatch):
    monkeypatch.setattr(sh.shutil, "which", lambda name: name if name == "ifconfig" else None)
    ran = []
    def run(cmd, **k):
        ran.append(cmd)
        return type("R", (), {"stdout": IFCONFIG})()
    monkeypatch.setattr(sh.subprocess, "run", run)
    assert sh.list_interfaces() == IFACES
    assert ran == [["ifconfig"]]


def test_list_interfaces_falls_back_to_the_routed_address(monkeypatch):
    monkeypatch.setattr(sh.shutil, "which", lambda name: None)
    monkeypatch.setattr(sh, "_routed_ip", lambda: "192.168.7.7")
    assert sh.list_interfaces() == [("", "192.168.7.7")]
    monkeypatch.setattr(sh, "_routed_ip", lambda: None)
    assert sh.list_interfaces() == []


def _scutil(monkeypatch, out, code=0):
    monkeypatch.setattr(sh, "PLATFORM", "darwin")
    monkeypatch.setattr(sh.subprocess, "run", lambda cmd, **k: type("R", (), {"stdout": out, "returncode": code})())


def test_mdns_name_on_macos_is_the_local_host_name(monkeypatch):
    _scutil(monkeypatch, "Hunters-MacBook\n")
    assert sh.mdns_name() == "hunters-macbook.local"


@pytest.mark.parametrize("out,code", [("", 0), ("Hunter's Mac\n", 0), ("evil.example#\n", 0), ("fine\n", 1)])
def test_mdns_name_rejects_names_that_could_not_be_a_host_label(monkeypatch, out, code):
    _scutil(monkeypatch, out, code)
    assert sh.mdns_name() is None


def test_mdns_name_elsewhere_needs_avahi(monkeypatch, tmp_path):
    monkeypatch.setattr(sh, "PLATFORM", "linux")
    monkeypatch.setattr(sh.socket, "gethostname", lambda: "box.corp.example")
    monkeypatch.setattr(sh, "AVAHI_SOCKET", tmp_path / "missing")
    assert sh.mdns_name() is None
    (tmp_path / "sock").write_text("")
    monkeypatch.setattr(sh, "AVAHI_SOCKET", tmp_path / "sock")
    assert sh.mdns_name() == "box.local"


def test_urls_are_listed_only_while_sharing_one_per_interface_plus_mdns(monkeypatch):
    monkeypatch.setattr(sh, "list_interfaces", lambda: IFACES)
    monkeypatch.setattr(sh, "mdns_name", lambda: "mac.local")
    s = sh.Share(enabled=True, clock=Clock(), mdns=True)
    assert s.urls(8791) == []
    s.on()
    assert s.urls(8791) == [{"label": "mDNS", "url": "http://mac.local:8791/"},
                            {"label": "en0", "url": "http://192.168.1.5:8791/"},
                            {"label": "utun3", "url": "http://10.8.0.2:8791/"}]


def test_the_mdns_name_is_off_unless_asked_for(monkeypatch):
    monkeypatch.setattr(sh, "list_interfaces", lambda: IFACES)
    asked = []
    monkeypatch.setattr(sh, "mdns_name", lambda: asked.append(1) or "mac.local")
    s = sh.Share(enabled=True, clock=Clock())
    s.on()
    assert s.mdns is None and not asked  # not even looked up
    assert [u["label"] for u in s.urls(8791)] == ["en0", "utun3"]
    assert sh.Share(enabled=False, mdns=True).mdns is None  # and only in share mode
    assert not asked
    assert sh.Share(enabled=True, mdns=True).mdns == "mac.local"


def test_urls_lookup_is_cached_briefly(monkeypatch):
    calls = []
    monkeypatch.setattr(sh, "list_interfaces", lambda: calls.append(1) or IFACES)
    monkeypatch.setattr(sh, "mdns_name", lambda: None)
    c = Clock()
    s = sh.Share(enabled=True, clock=c)
    s.on()
    s.urls(1); s.urls(1)
    assert len(calls) == 1
    c.t += sh.URL_CACHE_SECONDS + 1
    s.urls(1)
    assert len(calls) == 2


# ---- a share-mode desk, with a pretend LAN peer --------------------------------------------------------

class SockProxy:
    """A connected socket that reports a different local address, as one reached over the LAN would."""
    def __init__(self, sock, local_ip):
        self._sock, self._local = sock, local_ip

    def getsockname(self):
        return (self._local, self._sock.getsockname()[1])

    def __getattr__(self, name):
        return getattr(self._sock, name)


def _boot_share_desk(registry, packet, monkeypatch, mdns):
    monkeypatch.setattr(sh, "list_interfaces", lambda: IFACES)
    monkeypatch.setattr(sh, "mdns_name", lambda: "mac.local")
    entry = registry.add(packet)
    srv = make_server(0, registry, share=True, bind="127.0.0.1", mdns=mdns)
    clock = Clock()
    srv.share._clock = clock
    srv.share._wall = lambda: 1_700_000_000 + clock.t
    srv.fake_peer, srv.fake_local = None, LAN
    real = srv.get_request

    def get_request():
        sock, addr = real()
        if srv.fake_peer:
            return SockProxy(sock, srv.fake_local), (srv.fake_peer, addr[1])
        return sock, addr
    srv.get_request = get_request
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    c = Client(srv.server_address[1])
    c.pid, c.packet, c.srv, c.clock = entry.id, packet, srv, clock
    return c


@pytest.fixture
def sdesk(registry, packet, monkeypatch):
    """A share desk started with --share --share-mdns."""
    c = _boot_share_desk(registry, packet, monkeypatch, mdns=True)
    yield c
    c.srv.shutdown()
    c.srv.server_close()


@pytest.fixture
def plain_sdesk(registry, packet, monkeypatch):
    """A share desk started with just --share."""
    c = _boot_share_desk(registry, packet, monkeypatch, mdns=False)
    yield c
    c.srv.shutdown()
    c.srv.server_close()


def dropped(c, path="/", host=None, method="GET", **kw):
    """True if the desk hung up without answering (the connection was refused at accept time)."""
    try:
        c.request(method, path, host=host, **kw)
    except (http.client.RemoteDisconnected, http.client.BadStatusLine, ConnectionError, socket.timeout):
        return True
    return False


def post_share(c, action, **kw):
    h = {"X-Review-Desk": "1"}
    h.update(kw.pop("headers", {}))
    return c.request("POST", "/api/share", body={"action": action}, headers=h, **kw)


def state(c, **kw):
    return json.loads(c.get("/api/share", **kw)[2])


def lan_host(c):
    return f"{LAN}:{c.port}"


def test_nothing_is_shared_by_default(desk):
    s = json.loads(desk.get("/api/share")[2])
    assert s["enabled"] is False and s["active"] is False and s["urls"] == [] and s["canControl"] is False
    st, _, body = post_share(desk, "on")
    assert st == 403 and b"--share" in body


def test_localhost_can_open_a_four_hour_window(sdesk):
    st, _, body = post_share(sdesk, "on")
    s = json.loads(body)
    assert st == 200 and s["active"] and s["remaining"] == 4 * 3600 and s["enabled"] and s["canControl"]
    sdesk.clock.t += 3600
    assert state(sdesk)["remaining"] == 3 * 3600


def test_the_state_lists_each_url_only_while_sharing(sdesk):
    assert state(sdesk)["urls"] == [] and state(sdesk)["active"] is False
    post_share(sdesk, "on")
    urls = state(sdesk)["urls"]
    assert [u["label"] for u in urls] == ["mDNS", "en0", "utun3"]
    assert urls[1]["url"] == f"http://192.168.1.5:{sdesk.port}/"
    post_share(sdesk, "off")
    assert state(sdesk)["urls"] == []


def test_toggle_route_follows_the_button_rule(sdesk):
    post_share(sdesk, "toggle")
    assert state(sdesk)["active"]
    sdesk.clock.t += 120
    assert json.loads(post_share(sdesk, "toggle")[2])["remaining"] == 4 * 3600  # bump
    assert json.loads(post_share(sdesk, "toggle")[2])["active"] is False  # now at 4:00:00: off


def test_state_says_when_a_click_would_end_or_bump(sdesk):
    post_share(sdesk, "on")
    s = state(sdesk)
    assert s["bumpBelow"] == 4 * 3600 - 60 and s["seconds"] == 4 * 3600


def test_off_route_closes_the_window(sdesk):
    post_share(sdesk, "on")
    assert json.loads(post_share(sdesk, "off")[2])["active"] is False


@pytest.mark.parametrize("body", [{"action": "nope"}, {}, {"action": 1}, [1], "x"])
def test_bad_actions_are_rejected(sdesk, body):
    st, _, _ = sdesk.request("POST", "/api/share", body=body, headers={"X-Review-Desk": "1"})
    assert st == 400 and not state(sdesk)["active"]


def test_share_control_needs_the_write_headers(sdesk):
    assert sdesk.request("POST", "/api/share", body={"action": "on"})[0] == 403
    assert post_share(sdesk, "on", headers={"Origin": "https://evil.example"})[0] == 403
    assert post_share(sdesk, "on", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert sdesk.request("POST", "/api/share", raw_body=b'{"action":"on"}', headers={"X-Review-Desk": "1"})[0] == 415
    assert not state(sdesk)["active"]


def test_share_control_refuses_foreign_host_names(sdesk):
    assert post_share(sdesk, "on", host="evil.example:%d" % sdesk.port)[0] == 403
    assert not state(sdesk)["active"]


def test_a_loopback_peer_cannot_use_the_mdns_or_lan_host_names(sdesk):
    """A `.local` name can be claimed by anyone on the network, so this machine's own browser (or a page rebound to
    loopback) must not be able to reach the desk through it, for reading or writing."""
    post_share(sdesk, "on")
    for host in (f"mac.local:{sdesk.port}", f"{LAN}:{sdesk.port}"):
        assert sdesk.get("/", host=host)[0] == 403
        assert sdesk.get(f"/api/p/{sdesk.pid}/state", host=host)[0] == 403
        assert sdesk.put_item(sdesk.pid, "first-draft", {"decision": "approve"}, host=host,
                              headers={"Origin": f"http://{host}"})[0] == 403
        assert post_share(sdesk, "off", host=host)[0] == 403
    assert not (sdesk.packet / "review-decisions.json").exists() and state(sdesk)["active"]


def test_a_lan_peer_is_refused_while_the_window_is_closed(sdesk):
    sdesk.srv.fake_peer = "192.168.1.50"
    for path in ("/", "/api/health", f"/api/p/{sdesk.pid}/state", "/api/share", "/static/page.js"):
        assert dropped(sdesk, path, host=lan_host(sdesk)), path


def test_a_lan_peer_gets_in_while_the_window_is_open_and_not_after(sdesk):
    post_share(sdesk, "on")
    sdesk.srv.fake_peer = "192.168.1.50"
    assert sdesk.get("/", host=lan_host(sdesk))[0] == 200
    sdesk.clock.t += 4 * 3600
    assert dropped(sdesk, "/", host=lan_host(sdesk))


def test_closing_the_window_locks_lan_peers_out_at_once(sdesk):
    post_share(sdesk, "on")
    sdesk.srv.fake_peer = "192.168.1.50"
    assert sdesk.get("/", host=lan_host(sdesk))[0] == 200
    sdesk.srv.fake_peer = None
    post_share(sdesk, "off")
    sdesk.srv.fake_peer = "192.168.1.50"
    assert dropped(sdesk, "/", host=lan_host(sdesk))


@pytest.mark.parametrize("peer", ["8.8.8.8", "203.0.113.9", "172.32.0.1"])
def test_a_public_peer_is_refused_even_while_sharing(sdesk, peer):
    post_share(sdesk, "on")
    sdesk.srv.fake_peer = peer
    assert dropped(sdesk, "/", host=lan_host(sdesk))


def test_a_lan_peer_cannot_open_extend_or_end_the_window(sdesk):
    post_share(sdesk, "on")
    sdesk.clock.t += 600
    left = state(sdesk)["remaining"]
    sdesk.srv.fake_peer = "192.168.1.50"
    for host in (lan_host(sdesk), f"localhost:{sdesk.port}", f"127.0.0.1:{sdesk.port}"):
        for action in ("on", "toggle", "off"):
            assert post_share(sdesk, action, host=host)[0] == 403, (host, action)
    s = state(sdesk, host=lan_host(sdesk))
    assert s["remaining"] == left and s["canControl"] is False and s["urls"]  # it can see the countdown and URLs, not change them


def test_a_lan_peer_reviews_like_anyone_else(sdesk):
    post_share(sdesk, "on")
    sdesk.srv.fake_peer = "192.168.1.50"
    host = lan_host(sdesk)
    st, _, _ = sdesk.put_item(sdesk.pid, "first-draft", {"decision": "approve"}, host=host,
                              headers={"Origin": f"http://{host}"})
    assert st == 200
    assert json.loads((sdesk.packet / "review-decisions.json").read_text())["first-draft"]["decision"] == "approve"


def test_a_lan_peer_keeps_the_write_rules(sdesk):
    post_share(sdesk, "on")
    sdesk.srv.fake_peer = "192.168.1.50"
    host = lan_host(sdesk)
    assert sdesk.put_item(sdesk.pid, "first-draft", {"decision": "approve"}, host=host,
                          headers={"Origin": "https://evil.example"})[0] == 403
    assert sdesk.request("PUT", f"/api/p/{sdesk.pid}/items/first-draft", body={"decision": "approve"}, host=host)[0] == 403
    assert not (sdesk.packet / "review-decisions.json").exists()


@pytest.mark.parametrize("host", ["evil.example:{p}", "mac.local.evil.com:{p}", "192.168.1.6:{p}", "mac.local", "192.168.1.5", "localhost.evil.com:{p}"])
def test_a_lan_peer_must_use_an_address_or_name_of_this_machine(sdesk, host):
    post_share(sdesk, "on")
    sdesk.srv.fake_peer = "192.168.1.50"
    assert sdesk.get("/", host=host.format(p=sdesk.port))[0] == 403


def test_without_the_flag_the_mdns_name_is_refused_and_not_listed(plain_sdesk):
    c = plain_sdesk
    post_share(c, "on")
    assert [u["label"] for u in state(c)["urls"]] == ["en0", "utun3"]
    c.srv.fake_peer = "192.168.1.50"
    assert c.get("/", host=f"mac.local:{c.port}")[0] == 403
    assert c.get("/", host=lan_host(c))[0] == 200  # the address still works


def test_a_lan_peer_may_use_the_mdns_name(sdesk):
    post_share(sdesk, "on")
    sdesk.srv.fake_peer = "192.168.1.50"
    assert sdesk.get("/", host=f"mac.local:{sdesk.port}")[0] == 200


def test_the_mdns_name_is_not_trusted_when_sharing_is_not_enabled(registry, packet, monkeypatch):
    monkeypatch.setattr(sh, "mdns_name", lambda: "mac.local")
    registry.add(packet)
    srv = make_server(0, registry)  # not a share desk
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        c = Client(srv.server_address[1])
        assert srv.share.mdns is None
        assert c.get("/", host=f"mac.local:{c.port}")[0] == 403
        assert c.get("/", host=f"127.0.0.1:{c.port}")[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_pages_have_a_hidden_share_slot_and_the_script_drives_it():
    from pathlib import Path
    from review_desk import render
    assert 'id="share" hidden' in render.index_page([])
    js = (Path(sh.__file__).parent / "static" / "page.js").read_text()
    assert '"/api/share"' in js and '"X-Review-Desk": "1"' in js and 'action: "toggle"' not in js and '"toggle"' in js
    assert "Open from this network" in js and "s.bumpBelow" in js


def test_packet_page_has_a_share_slot_in_the_bar(desk):
    body = desk.get(f"/p/{desk.pid}/")[2].decode()
    assert 'id="share" hidden' in body and body.index('id="share"') < body.index('id="help-toggle"')


def test_layout_version_does_not_depend_on_share_mode(desk):
    st, h, _ = desk.get(f"/api/p/{desk.pid}/drafts")
    page = desk.get(f"/p/{desk.pid}/")[2].decode()
    assert f'data-layout-version="{h["X-Layout-Version"]}"' in page


# ---- the `share` command ------------------------------------------------------------------------------

@pytest.fixture
def cli_desk(sdesk, monkeypatch):
    monkeypatch.setattr(cli, "probe", lambda port: "desk")
    return sdesk


def run(cli_desk, capsys, *args):
    code = cli.main(["share", *args, "--port", str(cli_desk.port)])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_share_command_opens_reports_and_closes(cli_desk, capsys, state_dir):
    code, out, _ = run(cli_desk, capsys)
    assert code == 0 and out.strip() == "sharing: off"
    code, out, _ = run(cli_desk, capsys, "on")
    assert code == 0 and "sharing: on, 4:00:00 left" in out and f"http://192.168.1.5:{cli_desk.port}/  (en0)" in out
    assert "mac.local" in out
    cli_desk.clock.t += 3725
    assert "sharing: on, 2:57:55 left" in run(cli_desk, capsys, "status")[1]
    assert cli_desk.srv.share.active()
    code, out, _ = run(cli_desk, capsys, "off")
    assert code == 0 and out.strip() == "sharing: off" and not cli_desk.srv.share.active()


def test_share_on_bumps_a_running_window_to_four_hours(cli_desk, capsys, state_dir):
    run(cli_desk, capsys, "on")
    cli_desk.clock.t += 3600
    assert "4:00:00 left" in run(cli_desk, capsys, "on")[1]


def test_share_command_json(cli_desk, capsys, state_dir):
    code, out, _ = run(cli_desk, capsys, "on", "--json")
    s = json.loads(out)
    assert code == 0 and s["active"] and s["remaining"] == 4 * 3600 and len(s["urls"]) == 3


def test_share_command_says_when_the_desk_is_not_in_share_mode(desk, capsys, state_dir, monkeypatch):
    monkeypatch.setattr(cli, "probe", lambda port: "desk")
    assert cli.main(["share", "--port", str(desk.port)]) == 0
    assert "share mode is off" in capsys.readouterr().out
    assert cli.main(["share", "on", "--port", str(desk.port)]) == 1
    assert "--share" in capsys.readouterr().err


def test_share_command_needs_a_desk(capsys, state_dir, monkeypatch):
    monkeypatch.setattr(cli, "probe", lambda port: "none")
    assert cli.main(["share", "on", "--port", "1"]) == 1
    assert "no desk" in capsys.readouterr().err


# ---- starting a desk in share mode ---------------------------------------------------------------------

def test_serve_share_flag_and_env_reach_the_server(monkeypatch, state_dir, capsys):
    from review_desk import server
    seen = []

    class Stop:
        watcher = type("W", (), {"start": staticmethod(lambda: None)})()
        def serve_forever(self): pass
        def server_close(self): pass
    monkeypatch.setattr(server, "make_server", lambda port, reg, share=False, mdns=False: seen.append((share, mdns)) or Stop())
    monkeypatch.setattr(cli, "probe", lambda port: "none")
    monkeypatch.delenv("REVIEW_DESK_SHARE", raising=False)
    assert cli.main(["serve", "--port", "1"]) == 0
    assert cli.main(["serve", "--share", "--port", "1"]) == 0
    monkeypatch.setenv("REVIEW_DESK_SHARE", "1")
    assert cli.main(["serve", "--port", "1"]) == 0
    assert seen == [(False, False), (True, False), (True, False)]
    assert "share mode" in capsys.readouterr().out


def test_detached_desk_gets_share_from_the_flag_or_env(monkeypatch, state_dir):
    calls = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    for share, env, want in [(True, None, True), (False, "1", True), (False, None, False)]:
        monkeypatch.setattr(cli, "probe", lambda port, _s=iter(["none", "desk"]): next(_s))
        monkeypatch.delenv("REVIEW_DESK_SHARE", raising=False)
        if env:
            monkeypatch.setenv("REVIEW_DESK_SHARE", env)
        cli.ensure_running(1234, share=share)
        assert ("--share" in calls[-1]) is want


def test_serve_background_passes_share_to_the_launcher(monkeypatch, state_dir):
    seen = []
    monkeypatch.setattr(cli, "ensure_running", lambda port, reload=False, share=False, mdns=False: seen.append(share))
    assert cli.main(["serve", "--background", "--share", "--port", "1234"]) == 0
    assert cli.main(["serve", "--background", "--port", "1234"]) == 0
    assert seen == [True, False]


def test_share_on_an_already_running_desk_says_it_did_nothing(monkeypatch, state_dir, capsys):
    monkeypatch.setattr(cli, "probe", lambda port: "desk")
    cli.ensure_running(1234, share=True)
    assert "--share only applies" in capsys.readouterr().err
    cli.ensure_running(1234)
    assert capsys.readouterr().err == ""


def test_reload_supervisor_passes_share_to_its_desk(monkeypatch, state_dir):
    from review_desk import reload as rl
    seen = {}
    monkeypatch.setattr(rl, "supervise", lambda spawn, snap, **k: seen.update(spawn=spawn) or 0)
    monkeypatch.setattr(cli, "probe", lambda port: "none")
    popen = []
    monkeypatch.setattr(rl.subprocess, "Popen", lambda cmd, **k: popen.append(cmd) or object())
    monkeypatch.delenv("REVIEW_DESK_SHARE", raising=False)
    assert cli.main(["serve", "--reload", "--share", "--port", "1234"]) == 0
    seen["spawn"]()
    assert popen[0][-4:] == ["serve", "--port", "1234", "--share"] and "--reload" not in popen[0]


# ---- the window follows the machine, not just the clock ---------------------------------------------------

def test_the_window_ends_if_the_machine_sleeps():
    skew = [0]
    s, c = make_share(skew)
    s.on()
    c.t += 120
    assert s.active()
    skew[0] = 3600  # the wall clock ran an hour while the monotonic one stood still
    assert not s.active() and s.remaining() == 0
    skew[0] = 0
    assert not s.active()  # and it stays closed


def test_a_short_pause_or_a_small_clock_adjustment_does_not_end_it():
    skew = [0]
    s, c = make_share(skew)
    s.on()
    skew[0] = sh.SLEEP_SLACK - 1
    assert s.active()


def test_a_wall_clock_set_back_does_not_extend_the_window():
    skew = [0]
    s, c = make_share(skew)
    s.on()
    c.t += 4 * 3600 - 10
    skew[0] = -7200
    assert s.remaining() <= 10


def test_admits_needs_an_open_window_a_private_peer_and_an_address_the_machine_had_when_it_opened(monkeypatch):
    monkeypatch.setattr(sh, "list_interfaces", lambda: IFACES)
    s, c = make_share()
    assert not s.admits("192.168.1.50", LAN)
    s.on()
    assert s.admits("192.168.1.50", LAN) and s.admits("10.1.1.1", "10.8.0.2")
    assert not s.admits("192.168.1.50", "172.20.0.9")  # reached an address from some other network
    assert not s.admits("8.8.8.8", LAN) and not s.admits("203.0.113.9", LAN)
    s.off()
    assert not s.admits("192.168.1.50", LAN)


def test_reopening_takes_a_fresh_snapshot_of_the_addresses(monkeypatch):
    found = [[("en0", "192.168.1.5")]]
    monkeypatch.setattr(sh, "list_interfaces", lambda: found[0])
    s, c = make_share()
    s.on()
    found[0] = [("en0", "172.20.0.9")]  # moved to another network
    assert not s.admits("172.20.0.50", "172.20.0.9")
    s.on()
    assert s.admits("172.20.0.50", "172.20.0.9") and not s.admits("192.168.1.50", "192.168.1.5")


def test_a_peer_who_reached_an_address_from_before_a_network_change_is_dropped(sdesk):
    post_share(sdesk, "on")
    sdesk.srv.fake_peer, sdesk.srv.fake_local = "172.20.0.50", "172.20.0.9"
    assert dropped(sdesk, "/", host=f"172.20.0.9:{sdesk.port}")


def test_the_tooltip_lists_only_addresses_that_will_be_admitted(sdesk, monkeypatch):
    post_share(sdesk, "on")
    monkeypatch.setattr(sh, "list_interfaces", lambda: IFACES + [("en7", "192.168.50.2")])  # came up after the window opened
    sdesk.clock.t += sh.URL_CACHE_SECONDS + 1
    urls = [u["url"] for u in state(sdesk)["urls"]]
    assert f"http://{LAN}:{sdesk.port}/" in urls and not any("192.168.50.2" in u for u in urls)
    post_share(sdesk, "on")  # reopening takes a new snapshot
    assert any("192.168.50.2" in u["url"] for u in state(sdesk)["urls"])


def test_the_dispatch_check_still_refuses_if_the_window_closes_between_accept_and_dispatch(sdesk, monkeypatch):
    monkeypatch.setattr(srvmod.DeskServer, "verify_request", lambda self, request, addr: True)
    sdesk.srv.fake_peer = "192.168.1.50"
    assert sdesk.get("/", host=lan_host(sdesk))[0] == 403
    post_share(sdesk, "on")
    sdesk.srv.fake_peer = None
    post_share(sdesk, "on")
    sdesk.srv.fake_peer = "192.168.1.50"
    assert sdesk.get("/", host=lan_host(sdesk))[0] == 200


# ---- no peer can use up the desk -------------------------------------------------------------------------

def _hold(port, n):
    socks = []
    for _ in range(n):
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        socks.append(s)
    return socks


def _closed_by_server(s, wait=2.0):
    s.settimeout(wait)
    try:
        return s.recv(1) == b""
    except ConnectionError:
        return True
    except socket.timeout:
        return False


def _wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_loopback_connections_over_the_cap_are_dropped_and_room_returns_when_one_closes(sdesk, monkeypatch):
    monkeypatch.setattr(srvmod, "MAX_LOOPBACK_CONNECTIONS", 3)
    held = _hold(sdesk.port, 3)  # three idle connections, each owning a thread
    try:
        assert _wait_for(lambda: len(sdesk.srv._conns) == 3)
        assert dropped(sdesk, "/")  # the fourth gets nothing
        held.pop().close()
        assert _wait_for(lambda: len(sdesk.srv._conns) == 2)
        assert sdesk.get("/")[0] == 200
    finally:
        for s in held:
            s.close()
    assert _wait_for(lambda: len(sdesk.srv._conns) == 0)  # every slot is released


def test_lan_and_loopback_connections_are_capped_in_separate_pools(sdesk, monkeypatch):
    monkeypatch.setattr(srvmod, "MAX_LOOPBACK_CONNECTIONS", 3)
    monkeypatch.setattr(srvmod, "MAX_LAN_CONNECTIONS", 2)
    post_share(sdesk, "on")
    sdesk.srv.fake_peer = "192.168.1.50"
    lan = _hold(sdesk.port, 2)
    try:
        assert _wait_for(lambda: len(sdesk.srv._conns) == 2)
        assert dropped(sdesk, "/", host=lan_host(sdesk))  # a third LAN connection is refused
        sdesk.srv.fake_peer = None
        loop = _hold(sdesk.port, 3)  # the owner still has all of its own pool, however many the network holds
        try:
            assert _wait_for(lambda: len(sdesk.srv._conns) == 5)
            assert dropped(sdesk, "/")  # and its own pool is capped too
        finally:
            for s in loop:
                s.close()
    finally:
        for s in lan:
            s.close()


def test_a_refused_peer_never_gets_a_thread(sdesk):
    sdesk.srv.fake_peer = "192.168.1.50"
    before = threading.active_count()
    socks = _hold(sdesk.port, 30)
    try:
        assert all(_closed_by_server(s) for s in socks)
        assert threading.active_count() <= before + 2 and len(sdesk.srv._conns) == 0
    finally:
        for s in socks:
            s.close()


def test_a_connection_is_hung_up_after_the_deadline_however_slowly_it_trickles(sdesk, monkeypatch):
    monkeypatch.setattr(srvmod, "REQUEST_DEADLINE", 0.4)
    s = socket.create_connection(("127.0.0.1", sdesk.port), timeout=5)
    try:
        s.sendall(b"GET / HTTP/1.1\r\n")
        t0 = time.time()
        for _ in range(8):  # one more header line every 0.2 s, well inside the per-read timeout
            time.sleep(0.2)
            try:
                s.sendall(b"X-A: b\r\n")
            except OSError:
                break
        s.settimeout(3)
        try:
            assert s.recv(1) == b""
        except ConnectionError:
            pass
        assert time.time() - t0 < 3
    finally:
        s.close()
    assert _wait_for(lambda: len(sdesk.srv._conns) == 0)
    assert sdesk.get("/")[0] == 200


# ---- --share-mdns -----------------------------------------------------------------------------------------

def _serve_stub(monkeypatch, seen):
    from review_desk import server

    class Stop:
        watcher = type("W", (), {"start": staticmethod(lambda: None)})()
        def serve_forever(self): pass
        def server_close(self): pass
    monkeypatch.setattr(server, "make_server", lambda port, reg, share=False, mdns=False: seen.append((share, mdns)) or Stop())
    monkeypatch.setattr(cli, "probe", lambda port: "none")


def test_serve_share_mdns_flag_and_env_reach_the_server(monkeypatch, state_dir, capsys):
    seen = []
    _serve_stub(monkeypatch, seen)
    monkeypatch.delenv("REVIEW_DESK_SHARE", raising=False)
    monkeypatch.delenv("REVIEW_DESK_SHARE_MDNS", raising=False)
    assert cli.main(["serve", "--share", "--port", "1"]) == 0
    assert cli.main(["serve", "--share", "--share-mdns", "--port", "1"]) == 0
    monkeypatch.setenv("REVIEW_DESK_SHARE_MDNS", "1")
    assert cli.main(["serve", "--share", "--port", "1"]) == 0
    assert cli.main(["serve", "--port", "1"]) == 0  # the env switch without sharing is ignored
    assert seen == [(True, False), (True, True), (True, True), (False, False)]
    assert ".local name is accepted" in capsys.readouterr().out


def test_share_mdns_without_share_is_an_error(monkeypatch, state_dir, capsys):
    seen = []
    _serve_stub(monkeypatch, seen)
    monkeypatch.delenv("REVIEW_DESK_SHARE", raising=False)
    assert cli.main(["serve", "--share-mdns", "--port", "1"]) == 1
    assert "--share-mdns needs --share" in capsys.readouterr().err and seen == []
    monkeypatch.setenv("REVIEW_DESK_SHARE", "1")  # sharing from the env is enough
    assert cli.main(["serve", "--share-mdns", "--port", "1"]) == 0 and seen == [(True, True)]


def test_detached_desk_gets_share_mdns_only_with_share(monkeypatch, state_dir):
    calls = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    for share, mdns, env, want in [(True, True, None, True), (True, False, "1", True), (True, False, None, False),
                                   (False, True, None, False), (False, False, "1", False)]:
        monkeypatch.setattr(cli, "probe", lambda port, _s=iter(["none", "desk"]): next(_s))
        monkeypatch.delenv("REVIEW_DESK_SHARE", raising=False)
        monkeypatch.delenv("REVIEW_DESK_SHARE_MDNS", raising=False)
        if env:
            monkeypatch.setenv("REVIEW_DESK_SHARE_MDNS", env)
        cli.ensure_running(1234, share=share, mdns=mdns)
        assert ("--share-mdns" in calls[-1]) is want, (share, mdns, env)


def test_serve_background_passes_share_mdns_to_the_launcher(monkeypatch, state_dir):
    seen = []
    monkeypatch.setattr(cli, "ensure_running", lambda port, reload=False, share=False, mdns=False: seen.append((share, mdns)))
    assert cli.main(["serve", "--background", "--share", "--share-mdns", "--port", "1234"]) == 0
    assert cli.main(["serve", "--background", "--share", "--port", "1234"]) == 0
    assert seen == [(True, True), (True, False)]


def test_share_mdns_on_an_already_running_desk_says_it_did_nothing(monkeypatch, state_dir, capsys):
    monkeypatch.setattr(cli, "probe", lambda port: "desk")
    cli.ensure_running(1234, share=True, mdns=True)
    assert "--share-mdns only applies" in capsys.readouterr().err


def test_reload_supervisor_passes_share_mdns_to_its_desk(monkeypatch, state_dir):
    from review_desk import reload as rl
    seen = {}
    monkeypatch.setattr(rl, "supervise", lambda spawn, snap, **k: seen.update(spawn=spawn) or 0)
    monkeypatch.setattr(cli, "probe", lambda port: "none")
    popen = []
    monkeypatch.setattr(rl.subprocess, "Popen", lambda cmd, **k: popen.append(cmd) or object())
    monkeypatch.delenv("REVIEW_DESK_SHARE", raising=False)
    monkeypatch.delenv("REVIEW_DESK_SHARE_MDNS", raising=False)
    assert cli.main(["serve", "--reload", "--share", "--share-mdns", "--port", "1234"]) == 0
    seen["spawn"]()
    assert popen[0][-4:] == ["--port", "1234", "--share", "--share-mdns"] and "--reload" not in popen[0]


# ---- portability (macOS and Linux, glibc and musl) ----------------------------------------------------------

def test_server_threads_get_a_full_size_stack(registry):
    """musl's 128 KB default overflows on deeply nested JSON in a packet file (a segfault, not a RecursionError)."""
    srv = make_server(0, registry)
    try:
        assert threading.stack_size() >= srvmod.THREAD_STACK >= 8 * 1024 * 1024
    finally:
        srv.server_close()


def test_a_connection_reset_before_accept_does_not_end_the_server(sdesk):
    class Broken(SockProxy):
        def getsockname(self):
            raise OSError("not connected")
    real = sdesk.srv.get_request
    sdesk.srv.get_request = lambda: (lambda sa: (Broken(sa[0], LAN), sa[1]))(real())
    assert dropped(sdesk, "/")
    sdesk.srv.get_request = real
    assert sdesk.get("/")[0] == 200  # still serving


def test_the_linux_ip_command_output_parses(monkeypatch):
    """Both tools' real output formats, including a labelled secondary address and a busybox-style line."""
    text = ("1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n"
            "2: eth0    inet 192.168.1.5/24 brd 192.168.1.255 scope global dynamic eth0\\       valid_lft 85934sec\n"
            "2: eth0    inet 192.168.1.9/24 brd 192.168.1.255 scope global secondary eth0:1\\       valid_lft forever\n"
            "4: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever\n")
    assert sh.usable(sh.parse_ip_addr(text)) == [("eth0", "192.168.1.5"), ("eth0", "192.168.1.9"), ("docker0", "172.17.0.1")]
