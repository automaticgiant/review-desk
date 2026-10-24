import json

import pytest

from conftest import make_packet
from review_desk.cli import main
from review_desk.decisions import merge_item


def run(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_status_json(capsys, packet, state_dir):
    merge_item(packet, "first-draft", {"decision": "revise", "note": "shorter title"})
    code, out, _ = run(capsys, "status", str(packet), "--json")
    assert code == 0
    s = json.loads(out)
    assert s["title"] == "Demo packet" and s["path"] == str(packet.resolve())
    assert s["counts"] == {"approve": 0, "revise": 1, "drop": 0, "pending": 1, "total": 2}
    assert s["undecided"] == ["second-draft"]
    first = s["items"][0]
    assert first["id"] == "first-draft" and first["decision"] == "revise" and first["note"] == "shorter title"
    assert first["file"] == "first-draft.md" and first["updatedAt"]
    assert s["items"][1]["decision"] is None
    assert s["orphans"] == []
    assert s["lastTouched"] == first["updatedAt"]


def test_status_reports_orphans(capsys, packet, state_dir):
    merge_item(packet, "gone-item", {"decision": "approve"})
    s = json.loads(run(capsys, "status", str(packet), "--json")[1])
    assert s["orphans"] == ["gone-item"] and s["counts"]["pending"] == 2


def test_status_cleared_decision_counts_as_pending(capsys, packet, state_dir):
    merge_item(packet, "first-draft", {"decision": "approve"})
    merge_item(packet, "first-draft", {"decision": ""})
    s = json.loads(run(capsys, "status", str(packet), "--json")[1])
    assert s["counts"]["pending"] == 2 and s["undecided"] == ["first-draft", "second-draft"]


def test_status_text(capsys, packet, state_dir):
    merge_item(packet, "first-draft", {"decision": "approve", "note": "ship it"})
    code, out, _ = run(capsys, "status", str(packet))
    assert code == 0
    assert "1 approve" in out and "1 pending" in out
    assert "first-draft" in out and "ship it" in out
    assert "undecided: second-draft" in out


def test_status_all_registered(capsys, packet, state_dir):
    run(capsys, "add", str(packet))
    code, out, _ = run(capsys, "status", "--json")
    rows = json.loads(out)["packets"]
    assert code == 0 and rows[0]["counts"]["pending"] == 2 and rows[0]["id"]


def test_status_bad_manifest_exits_nonzero(capsys, tmp_path, state_dir):
    code, _, err = run(capsys, "status", str(tmp_path))
    assert code == 1 and "review.toml" in err


def test_add_list_remove(capsys, packet, state_dir):
    code, out, _ = run(capsys, "add", str(packet))
    assert code == 0 and "registered" in out
    code, out, _ = run(capsys, "list")
    assert "Demo packet" in out and str(packet.resolve()) in out and "2 pending" in out
    code, out, _ = run(capsys, "list", "--json")
    assert json.loads(out)["packets"][0]["title"] == "Demo packet"
    assert run(capsys, "remove", str(packet))[0] == 0
    assert run(capsys, "remove", str(packet))[0] == 1


def test_new_scaffolds_valid_manifest(capsys, tmp_path, state_dir):
    d = tmp_path / "fresh"
    code, out, _ = run(capsys, "new", str(d), "--title", "Fresh drafts")
    assert code == 0
    from review_desk.manifest import load
    p = load(d)
    assert p.title == "Fresh drafts" and p.items
    assert run(capsys, "new", str(d))[0] == 1  # refuses to overwrite


@pytest.fixture
def no_real_browser(monkeypatch):
    """Record every launch attempt; never start a desk or a browser."""
    from review_desk import cli
    monkeypatch.delenv("REVIEW_DESK_BROWSER", raising=False)
    monkeypatch.setattr(cli, "ensure_running", lambda port: None)
    calls = {"run": [], "open": [], "get": []}

    class FakeBrowser:
        def __init__(self, name):
            self.name = name

        def open(self, url, new=0, autoraise=True):
            calls["get"].append((self.name, url, new))
            return True

    def fake_get(name=None):
        if name == "no-such-browser":
            raise cli.webbrowser.Error("could not locate runnable browser")
        return FakeBrowser(name)

    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **k: calls["run"].append(cmd))
    monkeypatch.setattr(cli.webbrowser, "open", lambda url, new=0, autoraise=True: calls["open"].append((url, new)) or True)
    monkeypatch.setattr(cli.webbrowser, "get", fake_get)
    return calls


def test_open_no_browser_prints_url(capsys, packet, state_dir, no_real_browser, monkeypatch):
    from review_desk import cli
    monkeypatch.setattr(cli, "PLATFORM", "darwin")
    monkeypatch.setenv("REVIEW_DESK_BROWSER", "Some Browser")
    code, out, _ = run(capsys, "open", str(packet), "--port", "9999", "--no-browser")
    assert code == 0 and out.strip().startswith("http://127.0.0.1:9999/p/")
    assert no_real_browser == {"run": [], "open": [], "get": []}


def test_open_macos_default_browser(capsys, state_dir, no_real_browser, monkeypatch):
    from review_desk import cli
    monkeypatch.setattr(cli, "PLATFORM", "darwin")
    code, out, _ = run(capsys, "open", "--port", "9999")
    assert code == 0 and out.strip() == "http://127.0.0.1:9999/"
    assert no_real_browser["run"] == [["open", "http://127.0.0.1:9999/"]]
    assert no_real_browser["open"] == [] and no_real_browser["get"] == []


def test_open_macos_named_browser_from_env(capsys, state_dir, no_real_browser, monkeypatch):
    from review_desk import cli
    monkeypatch.setattr(cli, "PLATFORM", "darwin")
    monkeypatch.setenv("REVIEW_DESK_BROWSER", "Some Browser")
    assert run(capsys, "open", "--port", "9999")[0] == 0
    assert no_real_browser["run"] == [["open", "-na", "Some Browser", "--args", "--new-window", "http://127.0.0.1:9999/"]]


def test_open_browser_flag_overrides_env(capsys, state_dir, no_real_browser, monkeypatch):
    from review_desk import cli
    monkeypatch.setattr(cli, "PLATFORM", "darwin")
    monkeypatch.setenv("REVIEW_DESK_BROWSER", "Some Browser")
    assert run(capsys, "open", "--port", "9999", "--browser", "Other Browser")[0] == 0
    assert no_real_browser["run"] == [["open", "-na", "Other Browser", "--args", "--new-window", "http://127.0.0.1:9999/"]]


def test_open_empty_env_means_unset(capsys, state_dir, no_real_browser, monkeypatch):
    from review_desk import cli
    monkeypatch.setattr(cli, "PLATFORM", "darwin")
    monkeypatch.setenv("REVIEW_DESK_BROWSER", "")
    assert run(capsys, "open", "--port", "9999")[0] == 0
    assert no_real_browser["run"] == [["open", "http://127.0.0.1:9999/"]]


def test_open_other_platform_uses_webbrowser(capsys, state_dir, no_real_browser, monkeypatch):
    from review_desk import cli
    monkeypatch.setattr(cli, "PLATFORM", "linux")
    assert run(capsys, "open", "--port", "9999")[0] == 0
    assert no_real_browser["open"] == [("http://127.0.0.1:9999/", 1)]
    assert no_real_browser["run"] == [] and no_real_browser["get"] == []


def test_open_other_platform_named_browser(capsys, state_dir, no_real_browser, monkeypatch):
    from review_desk import cli
    monkeypatch.setattr(cli, "PLATFORM", "linux")
    monkeypatch.setenv("REVIEW_DESK_BROWSER", "firefox")
    assert run(capsys, "open", "--port", "9999")[0] == 0
    assert no_real_browser["get"] == [("firefox", "http://127.0.0.1:9999/", 1)]
    assert no_real_browser["run"] == [] and no_real_browser["open"] == []


def test_open_other_platform_unknown_browser_fails(capsys, state_dir, no_real_browser, monkeypatch):
    from review_desk import cli
    monkeypatch.setattr(cli, "PLATFORM", "linux")
    code, _, err = run(capsys, "open", "--port", "9999", "--browser", "no-such-browser")
    assert code == 1 and "no-such-browser" in err


def test_wait_returns_when_a_decision_changes(capsys, packet, state_dir, monkeypatch):
    sleeps = []

    def sleep(n):
        sleeps.append(n)
        assert len(sleeps) < 20, "never noticed the change"
        if len(sleeps) == 3:
            merge_item(packet, "first-draft", {"decision": "approve", "note": ""})
    monkeypatch.setattr("review_desk.cli.time.sleep", sleep)
    code, out, _ = run(capsys, "wait", str(packet), "--start", "1", "--max", "2")
    assert code == 0
    assert sleeps == [1, 2, 2]  # doubles, then caps
    assert out.splitlines()[0] == "CHANGED"
    assert ["first-draft", "approve", ""] in json.loads(out.splitlines()[1])


def test_wait_notices_a_changed_note_and_a_new_item(capsys, packet, state_dir, monkeypatch):
    monkeypatch.setattr("review_desk.cli.time.sleep",
                        lambda n: merge_item(packet, "second-draft", {"decision": "", "note": "hm"}))
    assert run(capsys, "wait", str(packet), "--start", "1")[0] == 0


def test_wait_rejects_bad_delays_and_bad_packets(capsys, packet, tmp_path, state_dir, monkeypatch):
    monkeypatch.setattr("review_desk.cli.time.sleep", lambda n: pytest.fail("must not sleep"))
    for bad in ("0", "-1"):
        assert run(capsys, "wait", str(packet), "--start", bad)[0] == 1
    assert run(capsys, "wait", str(packet), "--max", "0")[0] == 1
    assert run(capsys, "wait", str(tmp_path))[0] == 1  # no review.toml


def test_wait_gives_up_after_three_failed_polls_but_survives_one(capsys, packet, state_dir, monkeypatch):
    mf = packet / "review.toml"
    good = mf.read_text()
    n = []

    def sleep(_):
        n.append(1)
        assert len(n) < 20, "never gave up"
        mf.write_text(good if len(n) == 2 else "not [valid")
    # poll 1 fails, poll 2 is fine (resets the count), polls 3-5 fail: 3 in a row
    monkeypatch.setattr("review_desk.cli.time.sleep", sleep)
    code, _, err = run(capsys, "wait", str(packet), "--start", "1")
    assert code == 1 and "gave up after 3 failed polls" in err and len(n) == 5
