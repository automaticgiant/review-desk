"""External changes to items: the scan (draft, manifest, decisions file), agent writes through the API, the page's own
writes staying out of the list, the /changes route, and the card markup."""
import json
import os
import shutil
import subprocess
import time

import pytest

from conftest import MANIFEST, make_packet
from review_desk import changes
from review_desk.changes import Watcher


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Wall:
    """A timestamp source that moves forward on every call, so entries are ordered and distinct."""
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return f"2026-01-01T00:00:{self.n:02}.000Z"


def watcher(registry, clock=None):
    return Watcher(registry, clock=clock or Clock(), wall=Wall(), settle=0)


def entries(packet, item=None):
    items = changes.read(packet)
    return items if item is None else items.get(item, [])


def kinds(packet, item):
    return [e["kind"] for e in entries(packet, item)]


def edit(path, text, keep_mtime=False):
    if not path.exists():
        path.write_text(text)
        return
    st = path.stat()
    path.write_text(text)
    if keep_mtime:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    elif path.stat().st_mtime_ns == st.st_mtime_ns:  # coarse clocks: make the stat differ
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))


@pytest.fixture
def w(registry, packet):
    registry.add(packet)
    wt = watcher(registry)
    wt.scan_all()  # the baseline
    return wt


# ---- the scan -------------------------------------------------------------------------------------

def test_first_scan_is_a_baseline_and_records_nothing(registry, packet):
    registry.add(packet)
    watcher(registry).scan_all()
    assert changes.read(packet) == {}
    assert changes._read_file(packet)["seen"]  # but it remembered what it saw


def test_an_unchanged_packet_records_nothing_however_often_it_is_scanned(w, packet):
    for _ in range(3):
        w.scan_all()
    assert changes.read(packet) == {}


def test_editing_a_draft_records_a_draft_change_for_that_item_only(w, packet):
    edit(packet / "first-draft.md", "# First\n\nrevised body")
    w.scan_all()
    assert kinds(packet, "first-draft") == ["draft"] and "second-draft" not in entries(packet)
    w.scan_all()
    assert kinds(packet, "first-draft") == ["draft"]  # reported once


def test_each_further_edit_adds_an_entry_with_its_time(w, packet):
    edit(packet / "first-draft.md", "one")
    w.scan_all()
    edit(packet / "first-draft.md", "two")
    w.scan_all()
    got = entries(packet, "first-draft")
    assert [e["kind"] for e in got] == ["draft", "draft"] and got[0]["at"] < got[1]["at"]


def test_a_save_with_the_same_text_is_not_a_change(w, packet):
    edit(packet / "first-draft.md", "# First\n\nbody one")  # same bytes, new mtime
    w.scan_all()
    assert changes.read(packet) == {}


def test_a_same_size_edit_with_a_kept_mtime_is_caught_by_the_sweep(registry, packet):
    registry.add(packet)
    clock = Clock()
    wt = watcher(registry, clock)
    wt.scan_all()
    edit(packet / "first-draft.md", "# First\n\nbody ONE", keep_mtime=True)  # same length, same mtime
    wt.scan_all()
    assert changes.read(packet) == {}  # the stat says nothing changed, so it isn't rehashed yet
    clock.t += changes.SWEEP_SECONDS + 1
    wt.scan_all()
    assert kinds(packet, "first-draft") == ["draft"]


def test_an_oversize_file_is_fingerprinted_by_stat_not_read(registry, packet, monkeypatch):
    registry.add(packet)
    monkeypatch.setattr(changes, "MAX_HASHED", 4)
    wt = watcher(registry)
    wt.scan_all()
    hashed = []
    real = changes._sha
    monkeypatch.setattr(changes, "_sha", lambda data: hashed.append(data) or real(data))
    edit(packet / "first-draft.md", "a different and longer text")
    wt.scan_all()
    assert kinds(packet, "first-draft") == ["draft"]
    assert b"a different and longer text" not in hashed


def test_a_draft_that_goes_missing_and_returns_is_two_changes(w, packet):
    p = packet / "first-draft.md"
    text = p.read_text()
    p.unlink()
    w.scan_all()
    edit(p, text)
    w.scan_all()
    assert kinds(packet, "first-draft") == ["draft", "draft"]


def test_editing_an_items_manifest_entry_records_a_manifest_change(w, packet):
    edit(packet / "review.toml", MANIFEST.replace('title = "First draft"', 'title = "First draft, renamed"'))
    w.scan_all()
    assert kinds(packet, "first-draft") == ["manifest"] and "second-draft" not in entries(packet)


def test_editing_a_fact_or_link_is_a_manifest_change_for_that_item(w, packet):
    edit(packet / "review.toml", MANIFEST.replace("issues/1", "issues/2"))
    w.scan_all()
    assert kinds(packet, "second-draft") == ["manifest"] and "first-draft" not in entries(packet)


def test_a_manifest_edit_with_a_kept_mtime_is_caught_by_the_sweep(registry, packet):
    registry.add(packet)
    clock = Clock()
    wt = watcher(registry, clock)
    wt.scan_all()
    edit(packet / "review.toml", MANIFEST.replace('title = "First draft"', 'title = "First DRAFT"'), keep_mtime=True)
    wt.scan_all()
    assert changes.read(packet) == {}
    clock.t += changes.SWEEP_SECONDS + 1
    wt.scan_all()
    assert kinds(packet, "first-draft") == ["manifest"]


def test_editing_the_packet_intro_is_not_an_item_change(w, packet):
    edit(packet / "review.toml", MANIFEST.replace("Two drafts for", "Two new drafts for"))
    w.scan_all()
    assert changes.read(packet) == {}


def test_editing_the_decisions_file_by_hand_is_classified_as_such(w, packet):
    (packet / "review-decisions.json").write_text(json.dumps({"first-draft": {"decision": "approve", "note": "", "updatedAt": "x"}}))
    w.scan_all()
    assert kinds(packet, "first-draft") == ["decisions-file"]


def test_an_updatedat_only_change_to_the_decisions_file_is_not_a_change(registry, packet):
    (packet / "review-decisions.json").write_text(json.dumps({"first-draft": {"decision": "approve", "note": "n", "updatedAt": "a"}}))
    registry.add(packet)
    wt = watcher(registry)
    wt.scan_all()
    (packet / "review-decisions.json").write_text(json.dumps({"first-draft": {"decision": "approve", "note": "n", "updatedAt": "b"}}))
    wt.scan_all()
    assert changes.read(packet) == {}


def test_a_corrupt_decisions_file_is_ignored_not_fatal(w, packet):
    (packet / "review-decisions.json").write_text("{not json")
    edit(packet / "first-draft.md", "changed")
    w.scan_all()
    assert kinds(packet, "first-draft") == ["draft"]


def test_a_new_item_is_a_baseline_and_a_removed_item_is_forgotten(w, packet):
    edit(packet / "review.toml", MANIFEST + '\n[[items]]\nid = "third"\ntitle = "Third"\nfile = "third.md"\n')
    (packet / "third.md").write_text("x")
    w.scan_all()
    assert changes.read(packet) == {} and "draft:third" in changes._read_file(packet)["seen"]
    edit(packet / "review.toml", MANIFEST)
    w.scan_all()
    assert not any(k.endswith(":third") for k in changes._read_file(packet)["seen"])


def test_changes_made_while_the_desk_was_down_are_found_on_the_next_start(registry, packet):
    registry.add(packet)
    watcher(registry).scan_all()
    edit(packet / "first-draft.md", "edited while stopped")
    restarted = watcher(registry)  # fresh caches, as after a restart
    restarted.scan_all()
    assert kinds(packet, "first-draft") == ["draft"]
    watcher(registry).scan_all()
    assert kinds(packet, "first-draft") == ["draft"]  # and not again


def test_entries_are_capped_per_item(w, packet):
    for i in range(changes.MAX_ENTRIES + 5):
        edit(packet / "first-draft.md", f"text {i}")
        w.scan_all()
    assert len(entries(packet, "first-draft")) == changes.MAX_ENTRIES


def test_a_broken_packet_does_not_stop_the_others(registry, packet, tmp_path):
    bad = make_packet(tmp_path / "bad")
    registry.add(bad)
    registry.add(packet)
    wt = watcher(registry)
    wt.scan_all()
    (bad / "review.toml").write_text("this is not toml [")
    edit(packet / "first-draft.md", "changed")
    wt.scan_all()
    assert kinds(packet, "first-draft") == ["draft"]


def test_a_corrupt_changes_file_is_started_afresh(w, packet):
    changes.path_for(packet).write_text("{nope")
    assert changes.read(packet) == {}
    edit(packet / "first-draft.md", "changed")
    w.scan_all()  # the baseline was lost, so this pass only re-baselines
    edit(packet / "first-draft.md", "changed again")
    w.scan_all()
    assert kinds(packet, "first-draft") == ["draft"]


def test_the_background_thread_scans_an_open_packet_on_its_own(registry, packet):
    entry = registry.add(packet)
    wt = Watcher(registry, open_interval=0.02, settle=0.05)  # settling is on: the edit must still come out as one entry
    wt.scan_all()
    wt.touch(entry)  # a page has it open
    wt.start()
    try:
        edit(packet / "first-draft.md", "changed with nobody typing")
        for _ in range(100):
            if changes.read(packet):
                break
            time.sleep(0.03)
    finally:
        wt.stop()
    assert kinds(packet, "first-draft") == ["draft"]


def test_serve_starts_the_servers_watcher(monkeypatch, state_dir):
    from types import SimpleNamespace
    from review_desk import cli, server
    started = []

    class Stop:
        watcher = SimpleNamespace(start=lambda: started.append(1))
        def serve_forever(self): pass
        def server_close(self): pass
    monkeypatch.setattr(server, "make_server", lambda port, reg, share=False, mdns=False: Stop())
    monkeypatch.setattr(cli, "probe", lambda port: "none")
    assert cli.main(["serve", "--port", "1"]) == 0
    assert started == [1]


# ---- writes through the API ---------------------------------------------------------------------------

def agent_put(desk, item, body, **kw):
    return desk.put_item(desk.pid, item, body, **kw)


def page_put(desk, item, body):
    return desk.put_item(desk.pid, item, body, headers={"X-Review-Page": "1"})


@pytest.fixture
def d(desk):
    desk.srv.watcher.settle = 0
    wt = watcher(desk.registry)
    wt.scan_all()
    desk.watcher = wt
    return desk


def test_a_write_from_the_page_is_not_listed(d):
    assert page_put(d, "first-draft", {"decision": "approve", "note": "ok"})[0] == 200
    assert changes.read(d.packet) == {}
    d.watcher.scan_all()
    assert changes.read(d.packet) == {}  # and the scan doesn't mistake it for a hand edit of the file


def test_a_decision_written_without_the_page_is_listed_with_what_changed(d):
    assert agent_put(d, "first-draft", {"decision": "approve"})[0] == 200
    got = entries(d.packet, "first-draft")
    assert [(e["kind"], e["detail"]) for e in got] == [("decision", "not reviewed → approve")]
    d.watcher.scan_all()
    assert kinds(d.packet, "first-draft") == ["decision"]  # the scan adds nothing


def test_a_note_written_without_the_page_is_listed(d):
    agent_put(d, "first-draft", {"note": "shorter"})
    assert [(e["kind"], e["detail"]) for e in entries(d.packet, "first-draft")] == [("note", "note changed")]


def test_an_agent_clearing_a_revise_lists_both_changes(d):
    page_put(d, "first-draft", {"decision": "revise", "note": "shorter title"})
    st, _, _ = agent_put(d, "first-draft", {"decision": "", "note": "", "ifDecision": "revise", "ifNote": "shorter title"})
    assert st == 200
    got = {e["kind"]: e["detail"] for e in entries(d.packet, "first-draft")}
    assert got == {"decision": "revise → not reviewed", "note": "note cleared"}


def test_a_write_that_changes_nothing_lists_nothing(d):
    page_put(d, "first-draft", {"decision": "approve"})
    agent_put(d, "first-draft", {"decision": "approve"})
    assert changes.read(d.packet) == {}


def test_a_conflicting_write_lists_nothing(d):
    page_put(d, "first-draft", {"decision": "approve"})
    assert agent_put(d, "first-draft", {"decision": "", "ifDecision": "revise"})[0] == 409
    assert changes.read(d.packet) == {}


def test_a_change_log_failure_never_fails_the_decision(d, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(changes, "record_write", boom)
    assert agent_put(d, "first-draft", {"decision": "approve"})[0] == 200
    assert json.loads((d.packet / "review-decisions.json").read_text())["first-draft"]["decision"] == "approve"


# ---- the route and the page -----------------------------------------------------------------------------

def test_changes_route_serves_the_items_map(d):
    agent_put(d, "first-draft", {"decision": "drop"})
    st, h, body = d.get(f"/api/p/{d.pid}/changes")
    assert st == 200 and h["Content-Type"] == "application/json"
    assert json.loads(body)["first-draft"][0]["kind"] == "decision"
    assert "seen" not in json.loads(body)


def test_changes_route_is_empty_for_a_quiet_packet(d):
    assert json.loads(d.get(f"/api/p/{d.pid}/changes")[2]) == {}


def test_changes_route_only_serves_registered_packets_with_a_good_host(d):
    assert d.get("/api/p/nope-000000/changes")[0] == 404
    assert d.get(f"/api/p/{d.pid}/changes", host="evil.example")[0] == 403
    assert d.get(f"/api/p/{d.pid}/changes/")[0] == 404
    assert d.get("/api/p/..%2f/changes")[0] == 404


def test_each_card_has_a_hidden_changed_line_with_a_scrollable_tooltip(d):
    body = d.get(f"/p/{d.pid}/")[2].decode()
    assert body.count("data-changed hidden") == 2
    assert 'role="tooltip"' in body and 'data-changed-list tabindex="0"' in body and 'aria-describedby="first-draft-changes"' in body
    css = d.get("/static/page.css")[2].decode()
    assert ".change-tip ul { list-style: none; margin: 0; padding: 6px 12px; max-height: 12em; overflow-y: auto;" in css
    assert ".changed:hover .change-tip, .changed:focus-within .change-tip { display: block; }" in css


def test_layout_version_ignores_the_change_log(d):
    before = d.get(f"/api/p/{d.pid}/drafts")[1]["X-Layout-Version"]
    agent_put(d, "first-draft", {"decision": "approve"})
    edit(d.packet / "first-draft.md", "changed")
    d.watcher.scan_all()
    assert d.get(f"/api/p/{d.pid}/drafts")[1]["X-Layout-Version"] == before


def test_page_script_marks_its_writes_and_builds_the_list_safely(d):
    js = d.get("/static/page.js")[2].decode()
    assert '"X-Review-Page": "1"' in js and "refreshChanges()" in js
    block = js.split("async function refreshChanges", 1)[1].split("function refresh(first)", 1)[0]
    assert "innerHTML" not in block and "textContent" in block and "reverse()" in block
    assert "Decision set by an agent" in js and "Decisions file edited" in js
    assert "en.coarse" in block and "possibly several changes" in block


# ---- the lock ---------------------------------------------------------------------------------------------

def _in_thread(fn):
    import threading
    out = []
    t = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    t.start()
    return t, out


def test_a_decision_write_waits_for_a_scan_in_progress(d):
    changes.LOCK.acquire()
    try:
        t, out = _in_thread(lambda: agent_put(d, "first-draft", {"decision": "approve"}))
        t.join(0.3)
        assert t.is_alive() and not (d.packet / "review-decisions.json").exists()
    finally:
        changes.LOCK.release()
    t.join(3)
    assert out[0][0] == 200


def test_a_scan_waits_for_a_decision_write_in_progress(d):
    edit(d.packet / "first-draft.md", "changed")
    changes.LOCK.acquire()
    try:
        t, _ = _in_thread(d.watcher.scan_all)
        t.join(0.3)
        assert t.is_alive() and changes.read(d.packet) == {}
    finally:
        changes.LOCK.release()
    t.join(3)
    assert kinds(d.packet, "first-draft") == ["draft"]


# ---- when packets are scanned, and what a late scan can say ------------------------------------------------

class Counting(Watcher):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.scans = 0

    def _scan(self, entry):
        self.scans += 1
        return super()._scan(entry)


@pytest.fixture
def cw(registry, packet):
    entry = registry.add(packet)
    clock = Clock()
    return Counting(registry, clock=clock, wall=Wall(), settle=0), clock, entry


def test_a_closed_packet_is_scanned_at_first_then_only_every_ten_minutes(cw):
    wt, clock, _ = cw
    wt.tick()
    assert wt.scans == 1
    for _ in range(5):
        clock.t += 100
        wt.tick()
    assert wt.scans == 1  # 500 s on
    clock.t += 100
    wt.tick()
    assert wt.scans == 2


def test_an_open_packet_is_scanned_every_second(cw):
    wt, clock, entry = cw
    wt.touch(entry)
    n = wt.scans
    for _ in range(4):
        clock.t += 1
        wt.touch(entry)  # the page keeps polling
        wt.tick()
    assert wt.scans == n + 4


def test_the_first_poll_of_a_closed_packet_scans_at_once_and_later_polls_dont(cw):
    wt, clock, entry = cw
    wt.touch(entry)
    assert wt.scans == 1
    clock.t += 0.5
    wt.touch(entry)
    assert wt.scans == 1  # already open: the tick does the scanning


def test_a_packet_stops_being_open_when_the_page_stops_polling(cw):
    wt, clock, entry = cw
    wt.touch(entry)
    clock.t += changes.OPEN_TTL + 1
    wt.tick()  # due once more (it was open), then closed again
    n = wt.scans
    clock.t += 5
    wt.tick()
    assert wt.scans == n


def test_polling_again_after_it_closed_catches_up_at_once(cw):
    wt, clock, entry = cw
    wt.touch(entry)
    clock.t += changes.OPEN_TTL + 100
    n = wt.scans
    wt.touch(entry)
    assert wt.scans == n + 1


def test_a_packet_that_leaves_the_registry_is_forgotten(cw, registry, packet):
    wt, clock, entry = cw
    wt.touch(entry)
    registry.remove(packet)
    wt.tick()
    assert not wt._last and not wt._open and not wt._packets


def test_scanning_never_looks_at_unregistered_packets(registry, packet, tmp_path):
    other = make_packet(tmp_path / "other")
    registry.add(packet)
    wt = Counting(registry, clock=Clock(), wall=Wall(), settle=0)
    wt.tick()
    edit(other / "first-draft.md", "x")
    assert wt.scans == 1 and not changes.path_for(other).exists()


def test_a_scan_that_looked_recently_is_not_marked_coarse(registry, packet):
    registry.add(packet)
    clock = Clock()
    wt = Watcher(registry, clock=clock, wall=Wall(), settle=0)
    wt.scan_all()
    clock.t += 1
    edit(packet / "first-draft.md", "changed")
    wt.scan_all()
    assert "coarse" not in entries(packet, "first-draft")[0]


def test_a_scan_after_a_long_gap_is_marked_coarse_and_the_page_says_so(registry, packet):
    registry.add(packet)
    clock = Clock()
    wt = Watcher(registry, clock=clock, wall=Wall(), settle=0)
    wt.scan_all()
    edit(packet / "first-draft.md", "changed")
    edit(packet / "review.toml", MANIFEST.replace('title = "Second draft"', 'title = "Second, renamed"'))
    clock.t += changes.COARSE_AFTER + 1
    wt.scan_all()
    assert entries(packet, "first-draft")[0]["coarse"] is True and entries(packet, "second-draft")[0]["coarse"] is True


def test_a_restart_marks_what_it_finds_coarse(registry, packet):
    registry.add(packet)
    watcher(registry).scan_all()
    edit(packet / "first-draft.md", "changed while stopped")
    watcher(registry).scan_all()
    assert entries(packet, "first-draft")[0]["coarse"] is True


def test_a_late_scan_stamps_the_change_with_the_files_own_time(registry, packet):
    registry.add(packet)
    clock = Clock()
    stamps = iter(["2000-01-01T00:00:00.000Z", "2099-01-01T00:00:00.000Z"])  # the previous scan, then now
    wt = Watcher(registry, clock=clock, wall=lambda: next(stamps), settle=0)
    wt.scan_all()
    edit(packet / "first-draft.md", "changed")
    mtime = changes.iso_from_epoch((packet / "first-draft.md").stat().st_mtime_ns / 1e9)
    clock.t += 600
    wt.scan_all()
    assert entries(packet, "first-draft")[0]["at"] == mtime


def test_a_late_scan_ignores_an_mtime_in_the_future(registry, packet):
    registry.add(packet)
    clock = Clock()
    stamps = iter(["2000-01-01T00:00:00.000Z", "2001-01-01T00:00:00.000Z"])  # the file's mtime (now) is after both
    wt = Watcher(registry, clock=clock, wall=lambda: next(stamps), settle=0)
    wt.scan_all()
    edit(packet / "first-draft.md", "changed")
    clock.t += 600
    wt.scan_all()
    assert entries(packet, "first-draft")[0]["at"] == "2001-01-01T00:00:00.000Z"


def test_a_late_scan_ignores_an_mtime_no_newer_than_the_previous_scan(registry, packet):
    registry.add(packet)
    clock = Clock()
    stamps = iter(["2000-01-01T00:00:00.000Z", "2090-01-01T00:00:00.000Z", "2099-06-01T00:00:00.000Z"])
    wt = Watcher(registry, clock=clock, wall=lambda: next(stamps), settle=0)
    wt.scan_all()
    edit(packet / "first-draft.md", "changed")
    clock.t += 600
    wt.scan_all()  # mtime is between the two stamps: used
    edit(packet / "first-draft.md", "changed again!", keep_mtime=True)  # a tool that kept the old mtime, older than the last scan
    clock.t += changes.SWEEP_SECONDS + 600
    wt.scan_all()
    assert entries(packet, "first-draft")[1]["at"] == "2099-06-01T00:00:00.000Z"


def test_api_writes_are_never_coarse(d):
    agent_put(d, "first-draft", {"decision": "approve"})
    assert "coarse" not in entries(d.packet, "first-draft")[0]


def test_a_direct_edit_of_the_decisions_file_is_not_hidden_by_a_page_write_that_follows(d):
    page_put(d, "first-draft", {"note": "mine"})
    (d.packet / "review-decisions.json").write_text(json.dumps(
        {"first-draft": {"decision": "approve", "note": "mine", "updatedAt": "x"}}))  # an agent edits the file directly
    page_put(d, "first-draft", {"note": "mine, more"})  # the page writes before any scan has looked
    got = entries(d.packet, "first-draft")
    assert [e["kind"] for e in got] == ["decisions-file"] and got[0]["coarse"] is True


def test_the_changes_route_catches_up_when_a_page_first_polls_it(d):
    d.get(f"/api/p/{d.pid}/changes")  # first poll: baseline
    edit(d.packet / "first-draft.md", "edited while nobody had the page open")
    d.srv.watcher._open.clear()  # the page closed
    got = json.loads(d.get(f"/api/p/{d.pid}/changes")[2])
    assert [e["kind"] for e in got["first-draft"]] == ["draft"]


def test_a_second_poll_within_the_open_window_leaves_the_scanning_to_the_tick(d):
    clock = Clock()
    d.srv.watcher._clock = clock
    d.get(f"/api/p/{d.pid}/changes")
    edit(d.packet / "first-draft.md", "edited with the page open")
    clock.t += 1
    assert json.loads(d.get(f"/api/p/{d.pid}/changes")[2]) == {}  # the poll only marks the packet open
    d.srv.watcher.tick()
    assert [e["kind"] for e in json.loads(d.get(f"/api/p/{d.pid}/changes")[2])["first-draft"]] == ["draft"]


# ---- hostile files ---------------------------------------------------------------------------------------------

def _finishes(fn, timeout=3):
    import threading
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    t.join(timeout)
    return not t.is_alive()


def test_a_fifo_draft_does_not_block_the_scan(w, packet):
    (packet / "first-draft.md").unlink()
    os.mkfifo(packet / "first-draft.md")
    assert _finishes(w.scan_all)
    assert kinds(packet, "first-draft") == ["draft"]  # a draft that became "special" is a change, not a hang
    assert changes._read_file(packet)["seen"]["draft:first-draft"] == "special"


def test_a_fifo_review_changes_file_neither_blocks_the_lock_nor_the_scan(w, packet):
    changes.path_for(packet).unlink()
    os.mkfifo(changes.path_for(packet))
    assert _finishes(lambda: changes.read(packet))
    assert _finishes(w.scan_all)
    assert changes.LOCK.acquire(timeout=1)
    changes.LOCK.release()


def test_a_fifo_decisions_file_neither_blocks_the_scan_nor_a_write(d):
    os.mkfifo(d.packet / "review-decisions.json")
    assert _finishes(d.watcher.scan_all)
    out = []
    assert _finishes(lambda: out.append(agent_put(d, "first-draft", {"decision": "approve"})))
    assert out[0][0] == 500  # refused, as for any unreadable decisions file
    assert changes.LOCK.acquire(timeout=1)
    changes.LOCK.release()


def test_a_symlink_to_a_device_is_not_read(w, packet):
    changes.path_for(packet).unlink()
    changes.path_for(packet).symlink_to("/dev/zero")
    assert _finishes(lambda: changes.read(packet)) and changes.read(packet) == {}


def test_an_oversize_review_changes_file_is_ignored(w, packet, monkeypatch):
    monkeypatch.setattr(changes, "MAX_LOG_BYTES", 10)
    assert changes.read(packet) == {}


def test_a_corrupt_seen_key_does_not_stop_tracking(w, packet):
    changes.path_for(packet).write_text(json.dumps({"seen": {"junk": "x", "draft:first-draft": 5}, "items": {"first-draft": ["x", 1]}}))
    w.scan_all()  # re-baselines
    edit(packet / "first-draft.md", "one")
    w.scan_all()
    assert kinds(packet, "first-draft") == ["draft"]
    assert "junk" not in changes._read_file(packet)["seen"]


def test_a_scan_failure_is_reported_once_per_problem(registry, packet, capsys):
    entry = registry.add(packet)
    wt = watcher(registry)
    (packet / "review.toml").write_text("not toml [")
    wt.scan_packet(entry)
    wt.scan_packet(entry)
    err = capsys.readouterr().err
    assert err.count("change scan of") == 1


# ---- a check on the path is not enough: the file can change between the check and the open ------------------

def test_read_regular_never_blocks_on_a_fifo(tmp_path):
    from review_desk.fsutil import NotRegularError, read_regular
    os.mkfifo(tmp_path / "f")
    out = []

    def go():
        try:
            read_regular(tmp_path / "f")
        except NotRegularError:
            out.append("refused")
    assert _finishes(go) and out == ["refused"]
    (tmp_path / "r").write_text("hi")
    assert read_regular(tmp_path / "r") == b"hi"
    with pytest.raises(OSError):
        read_regular(tmp_path / "r", max_bytes=1)
    with pytest.raises(NotRegularError):
        read_regular(tmp_path)  # a directory


def test_a_draft_swapped_for_a_fifo_after_the_stat_check_does_not_hang_the_scan(w, packet, monkeypatch):
    real = os.stat(packet / "review.toml")
    p = packet / "first-draft.md"
    p.unlink()
    os.mkfifo(p)
    real_regular = changes._regular
    monkeypatch.setattr(changes, "_regular", lambda path: real if str(path).endswith("first-draft.md") else real_regular(path))
    assert _finishes(w.scan_all)  # the stat said "regular" (a lie, as if swapped just after); the open must not block
    assert changes._read_file(packet)["seen"]["draft:first-draft"] == "special"


def test_rendering_and_serving_a_draft_swapped_for_a_fifo_do_not_hang(d, monkeypatch):
    from pathlib import Path
    p = d.packet / "first-draft.md"
    p.unlink()
    os.mkfifo(p)
    monkeypatch.setattr(Path, "is_file", lambda self: True)  # as if the path check ran before the swap
    out = []
    assert _finishes(lambda: out.append(d.get(f"/api/p/{d.pid}/drafts/first-draft")))
    assert b"Draft file not found" in out[0][2]
    assert _finishes(lambda: out.append(d.get(f"/p/{d.pid}/raw/first-draft")))
    assert out[1][0] == 404


def test_a_manifest_swapped_for_a_fifo_does_not_hang_the_loader(packet, monkeypatch):
    from pathlib import Path
    from review_desk.manifest import ManifestError, load
    (packet / "review.toml").unlink()
    os.mkfifo(packet / "review.toml")
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    out = []

    def go():
        try:
            load(packet)
        except ManifestError as e:
            out.append(str(e))
    assert _finishes(go) and "can't read" in out[0]


def test_a_deeply_nested_changes_file_neither_fails_a_save_nor_stops_tracking(d):
    changes.path_for(d.packet).write_text("[" * 200000)  # fits under the size cap, overflows the JSON parser's recursion
    assert changes.read(d.packet) == {}
    st, _, _ = agent_put(d, "first-draft", {"decision": "approve"})
    assert st == 200
    assert json.loads((d.packet / "review-decisions.json").read_text())["first-draft"]["decision"] == "approve"
    assert d.get(f"/api/p/{d.pid}/changes")[0] == 200
    edit(d.packet / "first-draft.md", "changed")
    d.watcher.scan_all()
    edit(d.packet / "first-draft.md", "changed again")
    d.watcher.scan_all()
    assert "draft" in kinds(d.packet, "first-draft")


def test_a_deeply_nested_decisions_file_is_refused_like_any_unreadable_one(d):
    (d.packet / "review-decisions.json").write_text("[" * 200000)
    st, _, body = agent_put(d, "first-draft", {"decision": "approve"})
    assert st == 500 and b"not valid JSON" in body  # the refusal every unreadable decisions file gets, not "internal error"
    assert (d.packet / "review-decisions.json").read_text() == "[" * 200000  # not overwritten


def test_any_failure_of_the_change_log_leaves_the_saved_decision_alone(d, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("anything")
    monkeypatch.setattr(changes, "record_write", boom)
    assert agent_put(d, "first-draft", {"decision": "approve"})[0] == 200


def test_the_child_interpreters_never_import_from_the_current_directory(monkeypatch, state_dir):
    from review_desk import cli, reload as rl
    calls = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "probe", lambda port, _s=iter(["none", "desk"]): next(_s))
    cli.ensure_running(1234)
    monkeypatch.setattr(rl.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    rl.spawn_desk(1234)
    for cmd in calls:  # -P, before -m: the cwd would otherwise come first on sys.path and beat PYTHONPATH
        assert "-P" in cmd and cmd.index("-P") < cmd.index("-m")


def test_two_scans_of_one_packet_never_overlap(registry, packet):
    import threading
    entry = registry.add(packet)
    wt = watcher(registry)
    live, worst = [0], [0]
    real = wt._scan

    def slow(e):
        live[0] += 1
        worst[0] = max(worst[0], live[0])
        time.sleep(0.05)
        try:
            return real(e)
        finally:
            live[0] -= 1
    wt._scan = slow
    ts = [threading.Thread(target=wt.scan_packet, args=(entry,)) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert worst[0] == 1


def test_a_packet_that_leaves_the_registry_drops_its_cached_file_fingerprints(registry, packet):
    entry = registry.add(packet)
    wt = watcher(registry)
    wt.scan_all()
    assert wt._files
    registry.remove(packet)
    wt.tick()
    assert not wt._files


# ---- a file being written is not a change yet ---------------------------------------------------------------

def _mtime(path):
    return path.stat().st_mtime_ns / 1e9


def test_a_file_modified_a_moment_ago_is_left_for_the_next_pass(registry, packet):
    registry.add(packet)
    now = [0.0]
    wt = Watcher(registry, clock=Clock(), wall=Wall(), settle=0.5, epoch=lambda: now[0])
    now[0] = _mtime(packet / "first-draft.md") + 100
    wt.scan_all()  # baseline
    edit(packet / "first-draft.md", "new text")
    now[0] = _mtime(packet / "first-draft.md") + 0.1  # a tenth of a second after the write
    wt.scan_all()
    assert changes.read(packet) == {}
    now[0] = _mtime(packet / "first-draft.md") + 0.6
    wt.scan_all()
    assert kinds(packet, "first-draft") == ["draft"]
    wt.scan_all()
    assert kinds(packet, "first-draft") == ["draft"]


def test_a_save_that_truncates_then_writes_is_one_change_not_two(registry, packet):
    """An editor or agent that opens the draft for writing (emptying it) and then writes the text, with a scan in between."""
    registry.add(packet)
    now = [0.0]
    wt = Watcher(registry, clock=Clock(), wall=Wall(), settle=0.5, epoch=lambda: now[0])
    p = packet / "first-draft.md"
    now[0] = _mtime(p) + 100
    wt.scan_all()
    p.write_text("")  # truncated, the real text not written yet
    now[0] = _mtime(p) + 0.05
    wt.scan_all()  # sees an empty file, just modified
    p.write_text("# First\n\nthe finished revision")
    now[0] = _mtime(p) + 0.05
    wt.scan_all()
    assert changes.read(packet) == {}
    now[0] = _mtime(p) + 1
    wt.scan_all()
    assert kinds(packet, "first-draft") == ["draft"]


def test_settling_applies_to_the_manifest_and_decisions_file_too(registry, packet):
    registry.add(packet)
    now = [0.0]
    wt = Watcher(registry, clock=Clock(), wall=Wall(), settle=0.5, epoch=lambda: now[0])
    now[0] = _mtime(packet / "review.toml") + 100
    wt.scan_all()
    edit(packet / "review.toml", MANIFEST.replace('title = "First draft"', 'title = "First, renamed"'))
    (packet / "review-decisions.json").write_text(json.dumps({"second-draft": {"decision": "drop", "note": "", "updatedAt": "x"}}))
    now[0] = max(_mtime(packet / "review.toml"), _mtime(packet / "review-decisions.json")) + 0.1
    wt.scan_all()
    assert changes.read(packet) == {}
    now[0] += 1
    wt.scan_all()
    assert kinds(packet, "first-draft") == ["manifest"] and kinds(packet, "second-draft") == ["decisions-file"]


def test_a_first_look_is_a_baseline_even_for_a_file_written_a_moment_ago(registry, packet):
    """Otherwise an edit made right after registering would be absorbed into the baseline and never listed."""
    registry.add(packet)
    wt = Watcher(registry, clock=Clock(), wall=Wall(), settle=60)
    wt.scan_all()  # every file is fresh (a fixture just wrote them)
    assert changes._read_file(packet)["seen"]
    edit(packet / "first-draft.md", "edited right away")
    wt.settle = 0
    wt.scan_all()
    assert kinds(packet, "first-draft") == ["draft"]


# ---- "(5 min ago)" ------------------------------------------------------------------------------------------

PAGE_JS = __import__("pathlib").Path(changes.__file__).parent / "static" / "page.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node isn't installed")


def _node(body: str):
    """Run the page's own fmt/agoText/stamp definitions in node and return what `body` computes (as JSON)."""
    prog = f"""
const t = require('fs').readFileSync({json.dumps(str(PAGE_JS))}, 'utf8');
const a = t.indexOf('  const fmt = iso =>'), b = t.indexOf('\\n', t.indexOf('  const stamp = '));
const {{ fmt, agoText, stamp }} = new Function(t.slice(a, b) + '; return {{ fmt, agoText, stamp }};')();
const out = (() => {{ {body} }})();
console.log(JSON.stringify(out));"""
    r = subprocess.run([shutil.which("node"), "-e", prog], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_node
def test_ago_wording():
    got = _node("return [0, 9, 9.9, 10, 59, 59.9, 60, 125, 3599, 3600, 7300, 86399, 86400, 172800, 400000, NaN, -5].map(agoText);")
    assert got == ["just now", "just now", "just now", "10 s ago", "59 s ago", "59 s ago", "1 min ago", "2 min ago",
                   "59 min ago", "1 h ago", "2 h ago", "23 h ago", "1 day ago", "2 days ago", "4 days ago", "just now", "just now"]


@needs_node
def test_a_timestamp_is_followed_by_how_long_ago_in_parentheses():
    got = _node("""
      Date.now = () => Date.parse('2026-10-23T12:10:00Z');
      const five = '2026-10-23T12:05:00.000Z';
      return [stamp(five), fmt(five), stamp('2026-10-23T12:11:00.000Z'), stamp('not a date'), stamp('2026-10-20T12:10:00.000Z')];""")
    assert got[0] == f"{got[1]} (5 min ago)"
    assert got[2].endswith("(just now)")  # a clock slightly behind the file's
    assert got[3] == "not a date"  # unparseable: shown as given, no ago
    assert got[4].endswith("(3 days ago)")


def test_the_card_line_and_every_list_entry_carry_it_and_it_keeps_ticking(d):
    js = d.get("/static/page.js")[2].decode()
    block = js.split("async function refreshChanges", 1)[1].split("function refresh(first)", 1)[0]
    assert "stamp(entries[0].at)" in block and "stamp(en.at)" in block and "fmt(en.at)" not in block
    assert "line.dataset.at" in block and "t.dataset.at" in block
    tick = block.split("function tickAgo", 1)[1]
    assert "setInterval(tickAgo, 5000)" in tick and "stamp(line.dataset.at)" in tick and "stamp(t.dataset.at)" in tick
    assert "line.dataset.label" in tick  # the kind of change (and "possibly several") survives a tick
