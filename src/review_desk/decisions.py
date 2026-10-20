"""Decisions file beside the manifest: review-decisions.json = {item_id: {decision, note, updatedAt}}.

The shape is a stable contract: agents and existing packets rely on it, so it doesn't change. Writes merge one item
at a time under a process-wide lock and replace the file atomically, so two browser tabs editing different items can't clobber each other. Keys for items no longer in the
manifest are kept (status reports them as orphans). A corrupt file is never overwritten.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from .fsutil import atomic_write_json

DECISIONS = "review-decisions.json"
VALID = ("approve", "revise", "drop", "")
MAX_NOTE = 20000
_lock = threading.Lock()


class DecisionsError(ValueError):
    pass


def path_for(packet_dir) -> Path:
    return Path(packet_dir) / DECISIONS


def read(packet_dir) -> dict:
    p = path_for(packet_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise DecisionsError(f"{p} is not valid JSON ({e}); not touching it") from None
    if not isinstance(data, dict):
        raise DecisionsError(f"{p} must hold a JSON object")
    return data


def validate_patch(patch) -> dict:
    if not isinstance(patch, dict) or not patch:
        raise DecisionsError("expected a JSON object with 'decision' and/or 'note'")
    extra = set(patch) - {"decision", "note"}
    if extra:
        raise DecisionsError(f"unknown field(s): {', '.join(sorted(extra))}")
    if "decision" in patch and patch["decision"] not in VALID:
        raise DecisionsError("decision must be one of approve, revise, drop or '' (clear)")
    if "note" in patch and (not isinstance(patch["note"], str) or len(patch["note"]) > MAX_NOTE):
        raise DecisionsError(f"note must be a string of at most {MAX_NOTE} characters")
    return patch


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def merge_item(packet_dir, item_id: str, patch: dict) -> dict:
    validate_patch(patch)
    with _lock:
        state = read(packet_dir)
        rec = dict(state.get(item_id) or {})
        rec.setdefault("decision", "")
        rec.setdefault("note", "")
        rec.update(patch)
        rec["updatedAt"] = now_iso()
        state[item_id] = rec
        atomic_write_json(path_for(packet_dir), state)
        return rec
