import json
import re

import pytest

from conftest import make_packet


def test_index_lists_packet(desk):
    st, h, body = desk.get("/")
    assert st == 200 and b"Demo packet" in body and desk.pid.encode() in body
    assert "default-src 'none'" in h["Content-Security-Policy"]


def _row(pid, pending, total=2):
    counts = {"approve": total - pending, "revise": 0, "drop": 0, "pending": pending, "total": total}
    return {"id": pid, "path": f"/x/{pid}", "title": f"T {pid}", "counts": counts, "lastTouched": None, "error": None}


def test_index_hides_packets_with_nothing_pending_behind_a_checkbox():
    from review_desk import render
    html = render.index_page([_row("open-aaaaaa", 1), _row("done-bbbbbb", 0)])
    m = re.search(r'<input type="checkbox" id="done-toggle"[^>]*>', html)
    assert m and "checked" not in m.group(0)  # hidden by default
    assert "(1)" in html.split('id="done-toggle"', 1)[1].split("</label>", 1)[0]
    assert 'class="packet done"' in html and 'class="packet"' in html  # done ones are tagged, not dropped
    assert "done-toggle" not in render.index_page([_row("open-aaaaaa", 1)])  # nothing to toggle


def test_index_done_packets_hidden_by_css_and_toggle_persists(desk):
    css = desk.get("/static/page.css")[2].decode()
    assert re.search(r"body:not\(\.show-done\)\s+\.packet\.done\s*\{[^}]*display:\s*none", css)
    js = desk.get("/static/page.js")[2].decode()
    assert "done-toggle" in js and "show-done" in js and "localStorage" in js


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


def test_paths_under_home_show_as_tilde(desk, monkeypatch):
    """Screenshots and screen shares shouldn't reveal a username."""
    home = desk.packet.parent
    monkeypatch.setenv("HOME", str(home))
    expected = f"~/{desk.packet.name}".encode()
    for path in ("/", f"/p/{desk.pid}/"):
        body = desk.get(path)[2]
        assert expected in body, path
        assert str(home).encode() not in body, path


def test_notes_label_names_no_particular_agent(desk):
    body = desk.get(f"/p/{desk.pid}/")[2]
    assert b"Notes for the agent" in body and b"Claude" not in body


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


def test_packet_page_has_a_wrap_toggle(desk):
    """A viewing preference: a real checkbox in the bar, off by default; page.js restores and applies it."""
    body = desk.get(f"/p/{desk.pid}/")[2].decode()
    bar = body.split('id="bar"', 1)[1].split("</div>", 1)[0]
    m = re.search(r'<input type="checkbox" id="wrap-toggle"[^>]*>', bar)
    assert m and "checked" not in m.group(0)
    assert re.search(r'<label[^>]*for="wrap-toggle"[^>]*>[^<]*Wrap long lines', bar) or \
        re.search(r"<label[^>]*>\s*<input[^>]*id=\"wrap-toggle\"[^>]*>\s*Wrap long lines", bar)
    js = desk.get("/static/page.js")[2].decode()
    assert "wrap-toggle" in js and "localStorage" in js
    css = desk.get("/static/page.css")[2].decode()
    assert re.search(r"\.wrap-lines pre[^{]*\{[^}]*white-space:\s*pre-wrap", css)
    assert "wrap-toggle" not in desk.get("/")[2].decode()  # the index has no drafts to wrap


def test_packet_page_has_a_toggleable_help_dialog(desk):
    """Instructions live in a native <dialog>, closed by default; a bar button toggles it (no inline script)."""
    body = desk.get(f"/p/{desk.pid}/")[2].decode()
    bar = body.split('id="bar"', 1)[1].split("</div>", 1)[0]
    btn = re.search(r'<button[^>]*id="help-toggle"[^>]*>', bar)
    assert btn and 'type="button"' in btn.group(0)
    assert 'aria-controls="help-dialog"' in btn.group(0) and 'aria-expanded="false"' in btn.group(0)
    dlg = re.search(r'<dialog[^>]*id="help-dialog"[^>]*>(.*?)</dialog>', body, re.S)
    assert dlg and " open" not in dlg.group(0).split(">", 1)[0]
    text = dlg.group(1)
    for word in ("Approve", "Revise", "Drop", "Clear", "Notes for the agent", "review-decisions.json"):
        assert word in text
    assert 'id="help-close"' in text
    js = desk.get("/static/page.js")[2].decode()
    assert "showModal" in js and "help-toggle" in js and "aria-expanded" in js
    assert "<script>" not in body  # CSP forbids inline script
    assert "help-dialog" not in desk.get("/")[2].decode()  # the index has no decisions to explain


# ---- live draft refresh: the page swaps one changed draft in place, never reloads ----------------

def _versions(desk):
    st, h, body = desk.get(f"/api/p/{desk.pid}/drafts")
    assert st == 200 and h["Content-Type"].startswith("application/json")
    return json.loads(body)


def test_draft_versions_change_only_for_the_edited_draft(desk):
    v0 = _versions(desk)
    assert set(v0) == {"first-draft", "second-draft"} and all(isinstance(v, str) and v for v in v0.values())
    assert _versions(desk) == v0  # stable while nothing changes
    (desk.packet / "first-draft.md").write_text("# First\n\nrewritten by the agent")
    v1 = _versions(desk)
    assert v1["first-draft"] != v0["first-draft"] and v1["second-draft"] == v0["second-draft"]


def test_draft_fragment_returns_rendered_html_and_matching_version(desk):
    st, h, body = desk.get(f"/api/p/{desk.pid}/drafts/first-draft")
    got = json.loads(body)
    assert st == 200 and h["Content-Type"].startswith("application/json") and set(got) == {"version", "html"}
    assert "<h1>First</h1>" in got["html"] and "body one" in got["html"]
    assert got["version"] == _versions(desk)["first-draft"]
    (desk.packet / "first-draft.md").write_text("# First\n\nnew words")
    after = json.loads(desk.get(f"/api/p/{desk.pid}/drafts/first-draft")[2])
    assert "new words" in after["html"] and "body one" not in after["html"] and after["version"] != got["version"]


def test_draft_fragment_matches_what_the_page_renders(desk):
    page = desk.get(f"/p/{desk.pid}/")[2].decode()
    frag = json.loads(desk.get(f"/api/p/{desk.pid}/drafts/second-draft")[2])
    card = page.split('data-id="second-draft"', 1)[1]
    assert f'data-draft-version="{frag["version"]}"' in page.split('data-id="second-draft"', 1)[0] + card[:200]
    assert f'<div class="draft-body">{frag["html"]}</div>' in card


def test_draft_fragment_escapes_untrusted_text(desk):
    (desk.packet / "first-draft.md").write_text("<script>alert(1)</script>\n\n[c](javascript:alert(2))")
    for body in (desk.get(f"/api/p/{desk.pid}/drafts/first-draft")[2],):
        html = json.loads(body)["html"]
        assert "<script>" not in html and "javascript:" not in html and "&lt;script&gt;" in html


def test_draft_endpoints_cover_a_draft_that_goes_missing_and_returns(desk):
    (desk.packet / "sub" / "second.md").unlink()
    gone = json.loads(desk.get(f"/api/p/{desk.pid}/drafts/second-draft")[2])
    assert "not found" in gone["html"].lower() and gone["version"] == _versions(desk)["second-draft"]
    (desk.packet / "sub" / "second.md").write_text("# Back")
    back = json.loads(desk.get(f"/api/p/{desk.pid}/drafts/second-draft")[2])
    assert "Back" in back["html"] and back["version"] != gone["version"]


def test_draft_endpoints_only_serve_manifest_items_of_registered_packets(desk):
    assert desk.get(f"/api/p/{desk.pid}/drafts/not-in-manifest")[0] == 404
    assert desk.get(f"/api/p/{desk.pid}/drafts/review")[0] == 404  # review.toml is not an item
    assert desk.get("/api/p/nope-123456/drafts")[0] == 404
    assert desk.get("/api/p/nope-123456/drafts/first-draft")[0] == 404
    for path in ("/api/p/{pid}/drafts/../state", "/api/p/{pid}/drafts/%2e%2e%2fstate", "/api/p/{pid}/drafts/first-draft/x",
                 "/api/p/{pid}/drafts/first-draft.md"):
        st, _, body = desk.get(path.format(pid=desk.pid))
        assert st == 404 and b"body one" not in body


@pytest.mark.parametrize("suffix", ["drafts", "drafts/first-draft"])
def test_draft_endpoints_check_the_host_header(desk, suffix):
    assert desk.get(f"/api/p/{desk.pid}/{suffix}", host="evil.example:80")[0] == 403
    assert desk.get(f"/api/p/{desk.pid}/{suffix}", host=f"localhost:{desk.port}")[0] == 200


def test_packet_page_and_script_support_in_place_draft_refresh(desk):
    page = desk.get(f"/p/{desk.pid}/")[2].decode()
    versions = _versions(desk)
    for item, v in versions.items():
        art = re.search(rf'<article[^>]*data-id="{item}"[^>]*>', page).group(0)
        assert f'data-draft-version="{v}"' in art
    assert page.count("data-draft-updated") == len(versions)
    js = desk.get("/static/page.js")[2].decode()
    assert "/drafts" in js and "draftVersion" in js and ".draft-body" in js
    assert "location.reload" not in js  # never a reload: it would lose notes, scroll and open drafts


# ---- issue #2: hide reviewed items ---------------------------------------------------------------

def test_packet_page_has_a_hide_reviewed_toggle(desk):
    """A per-viewer preference like wrap: a checkbox in the bar, off by default, kept in localStorage.
    Approved and dropped cards hide; revise stays visible because it still needs a decision."""
    body = desk.get(f"/p/{desk.pid}/")[2].decode()
    bar = body.split('id="bar"', 1)[1].split("</div>", 1)[0]
    m = re.search(r'<input type="checkbox" id="hide-reviewed-toggle"[^>]*>', bar)
    assert m and "checked" not in m.group(0)
    assert "Hide reviewed" in bar
    css = desk.get("/static/page.css")[2].decode()
    rule = re.search(r"body\.hide-reviewed\s+\.card:not\(\.just-decided\)\[data-decision=\"approve\"\][^{]*\{[^}]*display:\s*none", css)
    assert rule and 'data-decision="drop"' in css.split("body.hide-reviewed", 1)[1].split("{", 1)[0]
    assert 'data-decision="revise"' not in css.split("body.hide-reviewed", 1)[1].split("{", 1)[0]
    js = desk.get("/static/page.js")[2].decode()
    assert "hide-reviewed-toggle" in js and "hide-reviewed" in js and "just-decided" in js and "localStorage" in js
    assert "hide-reviewed-toggle" not in desk.get("/")[2].decode()


# ---- issue #1: a changed manifest is noticed, without a reload ------------------------------------

def _layout(desk):
    st, h, _ = desk.get(f"/api/p/{desk.pid}/drafts")
    assert st == 200 and h.get("X-Layout-Version")
    return h["X-Layout-Version"]


def test_layout_version_tracks_the_manifest_not_the_draft_text(desk):
    v0 = _layout(desk)
    assert _layout(desk) == v0
    (desk.packet / "first-draft.md").write_text("# First\n\nonly the draft text changed")
    assert _layout(desk) == v0  # draft text is swapped in place, so it must not trigger the reload prompt
    toml = desk.packet / "review.toml"
    toml.write_text(toml.read_text().replace('title = "Second draft"', 'title = "Second draft, retitled"'))
    v1 = _layout(desk)
    assert v1 != v0
    toml.write_text(toml.read_text() + '\n[[items]]\nid = "third-draft"\ntitle = "Third"\nkind = "x"\nfile = "first-draft.md"\n')
    assert _layout(desk) not in (v0, v1)


def test_layout_version_matches_the_page_and_script_prompts_instead_of_reloading(desk):
    page = desk.get(f"/p/{desk.pid}/")[2].decode()
    assert re.search(rf'<body[^>]*data-layout-version="{_layout(desk)}"', page)
    assert re.search(r'id="layout-changed"[^>]*hidden', page)  # prompt exists, hidden until the manifest changes
    js = desk.get("/static/page.js")[2].decode()
    assert "X-Layout-Version" in js and "layout-changed" in js
    assert "location.reload" not in js  # the user chooses when to reload; a reload would lose open drafts and notes
