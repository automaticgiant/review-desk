import json
import os
import threading

import pytest

from review_desk import fsutil
from review_desk.decisions import DecisionsError, merge_item, read


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
