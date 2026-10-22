import json
import os
import threading

import pytest

from review_desk import fsutil
from review_desk.decisions import MAX_HISTORY, ConflictError, DecisionsError, merge_item, read, read_history


def test_read_missing_is_empty(packet):
    assert read(packet) == {}


def test_merge_sets_fields_and_timestamp(packet):
    rec = merge_item(packet, "first-draft", {"decision": "approve"})
    assert rec["decision"] == "approve" and rec["updatedAt"].endswith("Z")
    rec2 = merge_item(packet, "first-draft", {"note": "tweak title"})
    assert rec2["decision"] == "approve" and rec2["note"] == "tweak title"
    on_disk = json.loads((packet / "review-decisions.json").read_text())
    assert on_disk["first-draft"] == rec2


def test_merge_keeps_other_items_and_unknown_keys(packet):
    (packet / "review-decisions.json").write_text(json.dumps(
        {"second-draft": {"decision": "drop", "note": "n", "updatedAt": "2026-01-01T00:00:00Z"},
         "retired-item": {"decision": "approve", "note": "", "updatedAt": "2026-01-01T00:00:00Z"}}))
    merge_item(packet, "first-draft", {"decision": "revise"})
    d = read(packet)
    assert d["second-draft"]["decision"] == "drop" and "retired-item" in d and d["first-draft"]["decision"] == "revise"


def test_clear_decision(packet):
    merge_item(packet, "first-draft", {"decision": "approve"})
    assert merge_item(packet, "first-draft", {"decision": ""})["decision"] == ""


@pytest.mark.parametrize("patch", [
    {"decision": "yes"}, {"note": 5}, {"decision": None}, {}, {"other": "x"}, {"note": "x" * 20001},
])
def test_rejects_bad_patch(packet, patch):
    with pytest.raises(DecisionsError):
        merge_item(packet, "first-draft", patch)
    assert not (packet / "review-decisions.json").exists()


def test_corrupt_file_is_not_overwritten(packet):
    (packet / "review-decisions.json").write_text("{broken")
    with pytest.raises(DecisionsError):
        merge_item(packet, "first-draft", {"decision": "approve"})
    assert (packet / "review-decisions.json").read_text() == "{broken"


def test_atomic_write_leaves_original_on_failure(packet, monkeypatch):
    merge_item(packet, "first-draft", {"decision": "approve"})
    before = (packet / "review-decisions.json").read_text()

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(fsutil.os, "replace", boom)
    with pytest.raises(OSError):
        merge_item(packet, "second-draft", {"decision": "drop"})
    assert (packet / "review-decisions.json").read_text() == before
    assert sorted(os.listdir(packet)) == ["first-draft.md", "review-decisions.json", "review.toml", "sub"]  # no temp left


def test_concurrent_merges_do_not_clobber(packet):
    ids = [f"item-{i}" for i in range(40)]
    ts = [threading.Thread(target=merge_item, args=(packet, i, {"decision": "approve"})) for i in ids]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert set(read(packet)) == set(ids)


# ---- earlier notes: review-history.json -----------------------------------

def _history(packet):
    return json.loads((packet / "review-history.json").read_text())


def test_clearing_a_revise_note_archives_it(packet):
    merge_item(packet, "first-draft", {"decision": "revise", "note": "shorter title"})
    rec = merge_item(packet, "first-draft", {"decision": "", "note": ""})
    assert set(rec) == {"decision", "note", "updatedAt"} and rec["note"] == ""  # the decisions shape is unchanged
    (entry,) = _history(packet)["first-draft"]
    assert entry["note"] == "shorter title" and entry["decision"] == "revise" and entry["at"].endswith("Z")
    assert read_history(packet) == _history(packet)


@pytest.mark.parametrize("before, patch", [
    ({"decision": "revise", "note": "a"}, {"note": "ab"}),                       # editing, not clearing
    ({"decision": "revise", "note": "a"}, {"decision": ""}),                     # the note stays
    ({"decision": "approve", "note": "fine"}, {"note": ""}),                     # not a revision request
    ({"decision": "revise", "note": ""}, {"note": ""}),                          # nothing to keep
    ({"decision": "revise", "note": "  \n"}, {"note": ""}),                      # whitespace only
    ({"decision": "revise", "note": "mine"}, {"note": ""}),                      # the human deleting their own note
    ({"decision": "revise", "note": "mine"}, {"decision": "revise", "note": ""}),  # still on revise: not an agent clear
])
def test_other_changes_do_not_archive(packet, before, patch):
    merge_item(packet, "first-draft", before)
    merge_item(packet, "first-draft", patch)
    assert not (packet / "review-history.json").exists()


def test_history_is_per_item_and_capped_keeping_the_newest(packet):
    for i in range(MAX_HISTORY + 3):
        merge_item(packet, "first-draft", {"decision": "revise", "note": f"n{i}"})
        merge_item(packet, "first-draft", {"decision": "", "note": ""})
    merge_item(packet, "second-draft", {"decision": "revise", "note": "other"})
    merge_item(packet, "second-draft", {"decision": "", "note": ""})
    h = read_history(packet)
    assert [e["note"] for e in h["first-draft"]] == [f"n{i}" for i in range(3, MAX_HISTORY + 3)]
    assert [e["note"] for e in h["second-draft"]] == ["other"]


def test_corrupt_history_is_not_overwritten_and_blocks_the_clear(packet):
    merge_item(packet, "first-draft", {"decision": "revise", "note": "keep me"})
    (packet / "review-history.json").write_text("{broken")
    with pytest.raises(DecisionsError):
        merge_item(packet, "first-draft", {"decision": "", "note": ""})
    assert (packet / "review-history.json").read_text() == "{broken"
    assert read(packet)["first-draft"]["note"] == "keep me"  # the note is not lost


def test_history_write_failure_leaves_the_decision_untouched(packet, monkeypatch):
    """Only the history write fails, so this holds only if the history is written before the decisions file."""
    merge_item(packet, "first-draft", {"decision": "revise", "note": "keep me"})
    before = (packet / "review-decisions.json").read_bytes()
    real = fsutil.os.replace
    def replace(src, dst):
        if str(dst).endswith("review-history.json"):
            raise OSError("disk full")
        return real(src, dst)
    monkeypatch.setattr(fsutil.os, "replace", replace)
    with pytest.raises(OSError):
        merge_item(packet, "first-draft", {"decision": "", "note": ""})
    assert (packet / "review-decisions.json").read_bytes() == before


# ---- ifDecision: the agent's clear applies only if the human hasn't moved on ------------------------

CLEAR = {"decision": "", "note": "", "ifDecision": "revise"}


def test_if_decision_applies_when_it_matches_and_is_never_stored(packet):
    merge_item(packet, "first-draft", {"decision": "revise", "note": "shorter"})
    rec = merge_item(packet, "first-draft", CLEAR)
    assert rec["decision"] == "" and rec["note"] == "" and set(rec) == {"decision", "note", "updatedAt"}
    assert set(read(packet)["first-draft"]) == {"decision", "note", "updatedAt"}
    assert [e["note"] for e in read_history(packet)["first-draft"]] == ["shorter"]


def test_if_decision_mismatch_changes_nothing(packet):
    merge_item(packet, "first-draft", {"decision": "revise", "note": "shorter"})
    merge_item(packet, "first-draft", {"decision": "approve", "note": "ok, fix typo"})  # the human moved on
    before = (packet / "review-decisions.json").read_bytes()
    with pytest.raises(ConflictError):
        merge_item(packet, "first-draft", CLEAR)
    assert (packet / "review-decisions.json").read_bytes() == before
    assert not (packet / "review-history.json").exists()


def test_if_decision_matches_an_item_with_no_record_as_pending(packet):
    assert merge_item(packet, "first-draft", {"decision": "approve", "ifDecision": ""})["decision"] == "approve"
    with pytest.raises(ConflictError):
        merge_item(packet, "second-draft", {"decision": "approve", "ifDecision": "revise"})
    assert "second-draft" not in read(packet)


@pytest.mark.parametrize("patch", [{"ifDecision": "revise"}, {"note": "x", "ifDecision": "maybe"}, {"note": "x", "ifDecision": None}])
def test_if_decision_must_come_with_a_change_and_be_valid(packet, patch):
    with pytest.raises(DecisionsError):
        merge_item(packet, "first-draft", patch)
    assert not (packet / "review-decisions.json").exists()


def test_history_missing_is_empty(packet):
    assert read_history(packet) == {}
