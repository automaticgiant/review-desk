import json
import re
import threading

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


def test_packet_page_shows_the_build_tag(desk, monkeypatch):
    from review_desk import render
    monkeypatch.setattr(render, "GIT_SHA", "abc1234")
    assert b'<span class="build" title="git commit this desk is running">abc1234</span>' in desk.get(f"/p/{desk.pid}/")[2]
    monkeypatch.setattr(render, "GIT_SHA", "")
    assert b'class="build"' not in desk.get(f"/p/{desk.pid}/")[2]


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


def test_history_endpoint_serves_archived_notes(desk):
    assert json.loads(desk.get(f"/api/p/{desk.pid}/history")[2]) == {}
    desk.put_item(desk.pid, "first-draft", {"decision": "revise", "note": "shorter"})
    desk.put_item(desk.pid, "first-draft", {"decision": "", "note": ""})
    st, _, body = desk.get(f"/api/p/{desk.pid}/history")
    assert st == 200 and [e["note"] for e in json.loads(body)["first-draft"]] == ["shorter"]
    assert "shorter" not in desk.get(f"/api/p/{desk.pid}/state")[2].decode()


def test_history_endpoint_only_serves_registered_packets_with_a_good_host(desk):
    assert desk.get("/api/p/nope-123456/history")[0] == 404
    assert desk.get(f"/api/p/{desk.pid}/history", host="evil.example:80")[0] == 403
    assert desk.get(f"/api/p/{desk.pid}/history/x")[0] == 404
    (desk.packet / "review-history.json").write_text("{broken")
    assert desk.get(f"/api/p/{desk.pid}/history")[0] == 500


def test_history_route_segments_are_strict():
    from review_desk import server
    for seg in ("..", "%2e%2e", "A", "x/y", ""):
        assert not any(rx.match(f"/api/p/{seg}/history") for rx, _ in server.ROUTES_GET)


def test_cards_have_a_collapsed_earlier_notes_block_fed_from_history(desk):
    body = desk.get(f"/p/{desk.pid}/")[2].decode()
    assert body.count('<details class="earlier" data-earlier hidden>') == body.count('class="card"')
    assert body.index("data-earlier") > body.index("data-note") and body.index("data-earlier") < body.index('class="draft"')
    js = desk.get("/static/page.js")[2].decode()
    assert "/history" in js and "data-earlier-list" in js
    hist = js.split("async function refreshHistory", 1)[1].split("function refresh(", 1)[0]
    assert "textContent" in hist and "innerHTML" not in hist  # notes are the human's text, never parsed as HTML


def test_hiding_a_reviewed_card_folds_its_draft_first(desk):
    """collapse() closes the draft before it measures the card, so the card shrinks from its short form."""
    js = desk.get("/static/page.js")[2].decode()
    body = js.split("function collapse(card)", 1)[1].split("function scheduleHide", 1)[0]
    fold = body.index('querySelector("details.draft").open = false')
    assert fold < body.index("card.offsetHeight") and fold < body.index('classList.add("fading")')


# ---- hot swap: on by default, no flag ------------------------------------

def test_static_version_header_is_always_sent(desk):
    import hashlib
    from importlib import resources
    def h(n):
        return hashlib.sha1(resources.files("review_desk").joinpath("static", n).read_bytes()).hexdigest()[:12]
    assert desk.get(f"/api/p/{desk.pid}/drafts")[1].get("X-Static-Version") == f"{h('page.css')}.{h('page.js')}"


def test_static_version_follows_the_files(desk, monkeypatch):
    from review_desk import server
    before = desk.get(f"/api/p/{desk.pid}/drafts")[1]["X-Static-Version"]
    real = server.resources.files
    class Fake:
        def joinpath(self, *parts):
            f = real("review_desk").joinpath(*parts)
            class R:
                def read_bytes(s): return f.read_bytes() + (b"//x" if parts[-1] == "page.js" else b"")
            return R()
    monkeypatch.setattr(server.resources, "files", lambda _n: Fake())
    assert desk.get(f"/api/p/{desk.pid}/drafts")[1]["X-Static-Version"] != before


def test_static_version_splits_css_from_js(registry, packet, monkeypatch):
    """A css-only change must not look like a script change (that would needlessly re-run the script)."""
    from review_desk import server
    base = server.static_version()
    real = server.resources.files
    class Fake:
        def joinpath(self, *parts):
            f = real("review_desk").joinpath(*parts)
            class R:
                def read_bytes(s): return f.read_bytes() + (b"/*x*/" if parts[-1] == "page.css" else b"")
            return R()
    monkeypatch.setattr(server.resources, "files", lambda _n: Fake())
    css, js = server.static_version().split(".")
    assert css != base.split(".")[0] and js == base.split(".")[1]


def test_page_js_swaps_code_in_place_and_tears_the_old_copy_down(desk):
    js = desk.get("/static/page.js")[2].decode()
    block = js.split("X-Static-Version", 1)[1].split("const versions", 1)[0]
    assert "swapStylesheet(" in block and "loadScript(" in block
    # one place binds listeners, always tied to the abort signal, so a replaced copy can detach every one of them
    assert js.count("addEventListener") == 1 and "signal: ac.signal" in js and "ac.abort()" in js
    assert "clearInterval(poll)" in js and "window.__deskDispose" in js
    # a hot swap must not disable the fields (that would drop focus mid-note) or blank them with an empty first render
    assert "let connected = hot" in js and "if (!hot) { render(); setConn(false" in js


def test_serve_has_no_dev_flag_and_background_desks_hot_swap_too(state_dir, capsys):
    """Hot swap is the desk's normal behaviour, so there is nothing to opt into, background or not."""
    from review_desk import cli
    with pytest.raises(SystemExit):
        cli.main(["serve", "--dev", "--port", "1"])
    assert "unrecognized arguments: --dev" in capsys.readouterr().err


def test_put_if_decision_conflict_is_409_and_changes_nothing(desk):
    desk.put_item(desk.pid, "first-draft", {"decision": "revise", "note": "shorter"})
    desk.put_item(desk.pid, "first-draft", {"decision": "approve"})  # the human moved on
    before = (desk.packet / "review-decisions.json").read_bytes()
    st, _, body = desk.put_item(desk.pid, "first-draft", {"decision": "", "note": "", "ifDecision": "revise"})
    assert st == 409 and b"revise" in body
    assert (desk.packet / "review-decisions.json").read_bytes() == before
    desk.put_item(desk.pid, "first-draft", {"decision": "revise"})
    assert desk.put_item(desk.pid, "first-draft", {"decision": "", "note": "", "ifDecision": "revise"})[0] == 200
    assert desk.put_item(desk.pid, "first-draft", {"ifDecision": "revise"})[0] == 400


# ---- review findings: stale note, hot-swap teardown, one load per script change -----------------

def test_a_focused_field_follows_the_server_and_only_unsaved_typing_is_protected(desk):
    """A cleared note must replace stale text in a field the cursor is still in, or blur would write it back."""
    js = desk.get("/static/page.js")[2].decode()
    render = js.split("function render()", 1)[1].split("function setConn", 1)[0]
    assert "activeElement" not in render and "!note.dataset.dirty" in render
    # a save that went out before more typing must not mark the newer text as saved
    assert "field.value === patch.note" in js


def test_a_replaced_script_stops_touching_the_page(desk):
    js = desk.get("/static/page.js")[2].decode()
    assert "dead = true" in js
    for fn in ("function render()", "function setConn("):
        assert "if (dead) return;" in js.split(fn, 1)[1].split("\n", 3)[1] + js.split(fn, 1)[1].split("\n", 3)[2]
    assert "const d = card.dataset.decision;" in js and "state[card.dataset.id]?.decision" not in js
    assert "if (!r.ok || dead) return;" in js and "if (dead || w !== writes) return; state = s" in js


def test_a_changed_script_is_loaded_once_per_change(desk):
    js = desk.get("/static/page.js")[2].decode()
    assert "{ staticVersion.js = js; return loadScript(js); }" in js


def test_put_if_note_conflict_is_409(desk):
    desk.put_item(desk.pid, "first-draft", {"decision": "revise", "note": "fix A, and also B"})
    st, _, body = desk.put_item(desk.pid, "first-draft", {"decision": "", "note": "", "ifDecision": "revise", "ifNote": "fix A"})
    assert st == 409 and b"note" in body
    assert json.loads(desk.get(f"/api/p/{desk.pid}/state")[2])["first-draft"]["note"] == "fix A, and also B"


# ---- second review: poll responses vs saves, hiding while typing ----------------------------------

def test_a_poll_that_raced_a_save_is_discarded(desk):
    """A /state response that was in flight while a save started or finished is older than the page; applying it would
    put stale text back in the note field (a lost keystroke)."""
    js = desk.get("/static/page.js")[2].decode()
    assert js.count("writes++") == 3  # at the start of a save and when it succeeds or fails
    refresh = js.split("function refresh(first)", 1)[1].split("\n  }\n", 1)[0]
    assert "const w = writes;" in refresh and "w !== writes" in refresh


def test_hide_reviewed_waits_while_a_note_is_being_typed(desk):
    js = desk.get("/static/page.js")[2].decode()
    timer = js.split("function scheduleHide(card)", 1)[1].split("listen(hideToggle", 1)[0]
    assert "document.activeElement === note || note.dataset.dirty" in timer and "return scheduleHide(card)" in timer
    # but a focused Approve button must not hold the card back, or clicking with the mouse would never hide it
    assert "card.contains(document.activeElement)" not in timer


def test_help_text_matches_the_agent_clearing_the_mark(desk):
    body = desk.get(f"/p/{desk.pid}/")[2].decode()
    assert "until you decide again, or the agent has revised" in body and "Not reviewed" in body.split("<dt>Revise</dt>", 1)[1].split("<dt>Drop</dt>", 1)[0]
    js = desk.get("/static/page.js")[2].decode()
    assert "serve --dev" not in js
