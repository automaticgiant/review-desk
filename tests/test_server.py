import json

import pytest

from conftest import make_packet


def test_index_lists_packet(desk):
    st, h, body = desk.get("/")
    assert st == 200 and b"Demo packet" in body and desk.pid.encode() in body
    assert "default-src 'none'" in h["Content-Security-Policy"]


def test_packet_page_renders_items(desk):
    st, _, body = desk.get(f"/p/{desk.pid}/")
    assert st == 200
    for s in (b'data-id="first-draft"', b'data-id="second-draft"', b"body one", b"body two", b"<strong>File</strong>"):
        assert s in body
    assert b'href="/"' in body  # link back to the index


def test_pages_are_titled_as_the_desk(desk):
    """So a desk tab can't be mistaken for another page with the same name."""
    for path in ("/", f"/p/{desk.pid}/"):
        body = desk.get(path)[2]
        title = body.split(b"<title>", 1)[1].split(b"</title>", 1)[0]
        assert title.endswith(b"\xc2\xb7 review desk"), title


def test_packet_page_has_a_disconnected_banner(desk):
    """The page shows it (and locks the controls) whenever it can't save."""
    body = desk.get(f"/p/{desk.pid}/")[2]
    banner = body.split(b'id="offline"', 1)[1].split(b"</div>", 1)[0]
    assert b"hidden" in banner.split(b">", 1)[0]  # hidden until the script finds the desk unreachable
    assert b"not saving" in banner.lower()
    assert b'role="alert"' in body.split(b'id="offline"', 1)[0][-120:]


def test_page_is_live(desk):
    (desk.packet / "first-draft.md").write_text("edited text after registration")
    assert b"edited text after registration" in desk.get(f"/p/{desk.pid}/")[2]


def test_state_and_put_roundtrip(desk):
    st, _, body = desk.put_item(desk.pid, "first-draft", {"decision": "approve"})
    assert st == 200 and json.loads(body)["decision"] == "approve"
    st, _, body = desk.put_item(desk.pid, "second-draft", {"note": "hello"})
    assert st == 200
    state = json.loads(desk.get(f"/api/p/{desk.pid}/state")[2])
    assert state["first-draft"]["decision"] == "approve" and state["second-draft"]["note"] == "hello"


def test_health(desk):
    st, _, body = desk.get("/api/health")
    assert st == 200 and json.loads(body)["app"] == "review-desk"


def test_static_assets(desk):
    assert desk.get("/static/page.js")[0] == 200
    assert desk.get("/static/page.css")[0] == 200
    assert desk.get("/static/../server.py")[0] == 404


# ---- security rules -------------------------------------------------------

@pytest.mark.parametrize("host", ["evil.example:{p}", "127.0.0.1", "127.0.0.1:1", "localhost.evil.com:{p}", "", "0.0.0.0:{p}"])
def test_wrong_host_rejected(desk, host):
    host = host.format(p=desk.port)
    assert desk.get("/", host=host or " ")[0] == 403
    assert desk.get(f"/api/p/{desk.pid}/state", host=host or " ")[0] == 403
    assert desk.put_item(desk.pid, "first-draft", {"decision": "approve"}, host=host or " ")[0] == 403
    assert not (desk.packet / "review-decisions.json").exists()


def test_localhost_host_accepted(desk):
    assert desk.get("/", host=f"localhost:{desk.port}")[0] == 200


def test_missing_custom_header_rejected(desk):
    st, _, _ = desk.request("PUT", f"/api/p/{desk.pid}/items/first-draft", body={"decision": "approve"})
    assert st == 403
    st, _, _ = desk.put_item(desk.pid, "first-draft", {"decision": "approve"}, headers={"X-Review-Desk": "0"})
    assert st == 403
    assert not (desk.packet / "review-decisions.json").exists()


def test_cross_origin_rejected(desk):
    st, _, _ = desk.put_item(desk.pid, "first-draft", {"decision": "approve"}, headers={"Origin": "https://evil.example"})
    assert st == 403
    st, _, _ = desk.put_item(desk.pid, "first-draft", {"decision": "approve"}, headers={"Origin": f"http://127.0.0.1:{desk.port}"})
    assert st == 200


def test_non_json_content_type_rejected(desk):
    st, _, _ = desk.request("PUT", f"/api/p/{desk.pid}/items/first-draft", raw_body=b"decision=approve",
                            headers={"X-Review-Desk": "1", "Content-Type": "application/x-www-form-urlencoded"})
    assert st == 415


def test_unknown_packet_rejected(desk):
    assert desk.get("/p/nope-123456/")[0] == 404
    assert desk.get("/api/p/nope-123456/state")[0] == 404
    assert desk.put_item("nope-123456", "first-draft", {"decision": "approve"})[0] == 404


def test_unregistered_packet_dir_not_reachable(desk, tmp_path):
    other = make_packet(tmp_path / "other")
    # same manifest, never registered: no id can reach it
    for e in desk.registry.entries():
        assert e.path != str(other.resolve())


def test_unknown_item_rejected(desk):
    st, _, _ = desk.put_item(desk.pid, "not-in-manifest", {"decision": "approve"})
    assert st == 404
    assert not (desk.packet / "review-decisions.json").exists()


@pytest.mark.parametrize("path", [
    "/p/{pid}/raw/../../../etc/passwd",
    "/p/{pid}/raw/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    "/p/{pid}/raw/review.toml",
    "/p/{pid}/raw/first-draft.md",
    "/p/{pid}/../../etc/passwd",
    "/static/%2e%2e/server.py",
    "/p/{pid}/review-decisions.json",
])
def test_traversal_rejected(desk, path):
    st, _, body = desk.get(path.format(pid=desk.pid))
    assert st == 404 and b"root:" not in body and b"[[items]]" not in body


def test_raw_serves_only_manifest_files(desk):
    st, h, body = desk.get(f"/p/{desk.pid}/raw/second-draft")
    assert st == 200 and body == b"# Second\n\nbody two" and h["Content-Type"].startswith("text/plain")


def test_manifest_with_escaping_file_does_not_leak(desk, tmp_path):
    secret = desk.packet.parent / "secret.md"
    secret.write_text("TOP SECRET")
    (desk.packet / "review.toml").write_text('title="t"\n[[items]]\nid="a"\ntitle="x"\nfile="../secret.md"\n')
    st, _, body = desk.get(f"/p/{desk.pid}/")
    assert st == 500 and b"TOP SECRET" not in body
    assert desk.get(f"/p/{desk.pid}/raw/a")[2].find(b"TOP SECRET") == -1


@pytest.mark.parametrize("size", [70 * 1024, 10 * 1024 * 1024])
def test_oversize_body_rejected(desk, size):
    big = json.dumps({"note": "x" * size}).encode()
    st, _, _ = desk.request("PUT", f"/api/p/{desk.pid}/items/first-draft", raw_body=big,
                            headers={"X-Review-Desk": "1", "Content-Type": "application/json"})
    assert st == 413
    assert not (desk.packet / "review-decisions.json").exists()


def test_missing_length_rejected(desk):
    st, _, _ = desk.request("PUT", f"/api/p/{desk.pid}/items/first-draft",
                            headers={"X-Review-Desk": "1", "Content-Type": "application/json"})
    assert st == 411


@pytest.mark.parametrize("raw", [b"[1]", b"not json", b'{"decision": "maybe"}', b'{"note": 3}'])
def test_bad_body_rejected(desk, raw):
    st, _, _ = desk.request("PUT", f"/api/p/{desk.pid}/items/first-draft", raw_body=raw,
                            headers={"X-Review-Desk": "1", "Content-Type": "application/json"})
    assert st == 400


@pytest.mark.parametrize("method", ["POST", "DELETE", "PATCH"])
def test_other_methods_rejected(desk, method):
    st, _, _ = desk.request(method, f"/api/p/{desk.pid}/items/first-draft", body={"decision": "approve"},
                            headers={"X-Review-Desk": "1"})
    assert st == 405


def test_untrusted_text_escaped(registry, tmp_path):
    from review_desk.server import make_server
    import threading
    from conftest import Client
    evil = make_packet(tmp_path / "evil", '''
title = "<script>alert('t')</script>"
intro = "<img src=x onerror=alert(1)> [x](javascript:alert(2))"
[[items]]
id = "a"
title = "<b onmouseover=alert(3)>t</b>"
kind = "<i>k</i>"
recommendation = "<script>alert(4)</script>"
file = "a.md"
''', files={"a.md": "<script>alert(5)</script>\n\n<iframe src=x></iframe>\n\n[c](javascript:alert(6))\n\n`<code>`"})
    e = registry.add(evil)
    srv = make_server(0, registry)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        c = Client(srv.server_address[1])
        for path in ("/", f"/p/{e.id}/"):
            body = c.get(path)[2].decode()
            assert "<script>alert" not in body and "<iframe" not in body and "<img" not in body
            assert "javascript:" not in body and "<b onmouseover" not in body and "<i>k</i>" not in body
            assert "&lt;script&gt;" in body
        assert "&lt;code&gt;" in c.get(f"/p/{e.id}/")[2].decode()
    finally:
        srv.shutdown()
        srv.server_close()


def test_binds_loopback_only(registry):
    from review_desk.server import make_server
    srv = make_server(0, registry)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()


# Each traversal layer tested on its own (mutation check: each was masked by the other).

def test_raw_never_serves_unlisted_files(desk):
    """Layer 2: even a route-valid segment only resolves through the manifest, never as a filename."""
    (desk.packet / "unlisted-secret").write_text("UNLISTED")
    st, _, body = desk.get(f"/p/{desk.pid}/raw/unlisted-secret")
    assert st == 404 and b"UNLISTED" not in body


@pytest.mark.parametrize("seg", ["review.toml", "..", "%2e%2e", "a%2fb", "A", "first-draft.md", "x/y", ""])
def test_route_segments_are_strict_slugs(seg):
    """Layer 1: path segments never decode and never contain dots, slashes or percent signs."""
    from review_desk import server
    assert not server.ROUTE_PUT.match(f"/api/p/pkt-1/items/{seg}")
    paths = [f"/p/{seg}/", f"/p/pkt-1/raw/{seg}", f"/api/p/{seg}/state"]
    assert not any(rx.match(p) for p in paths for rx, _ in server.ROUTES_GET)


def test_favicon_is_empty_204(desk):
    assert desk.get("/favicon.ico")[0] == 204
