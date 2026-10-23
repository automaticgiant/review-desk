"""Decisions file beside the manifest: review-decisions.json = {item_id: {decision, note, updatedAt}}.

The shape is a stable contract: agents and existing packets rely on it, so it doesn't change. Writes merge one item
at a time under a process-wide lock and replace the file atomically, so two browser tabs editing different items can't clobber each other. Keys for items no longer in the
manifest are kept (status reports them as orphans). A corrupt file is never overwritten.

Earlier notes live in a second file, review-history.json = {item_id: [{note, decision, at}, ...]} (oldest first, at most
MAX_HISTORY per item). When one write moves an item off "revise" and clears its note (what an agent does after
revising), the old note is archived there first, so the page can show what prompted the revision. Editing or deleting a
note on its own never archives.

A patch may carry guards, "ifDecision" and/or "ifNote": the write applies only while the item's decision (note) is
still that value, otherwise ConflictError and nothing changes. Guards are never stored. The agent's clear uses both, so
it can't wipe a decision the human made, or a note the human added to, after the agent read the item. An archived
entry's "at" is when that note was written (the old record's updatedAt), i.e. when the revision was requested.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from .fsutil import NotRegularError, atomic_write_json, read_regular

DECISIONS = "review-decisions.json"
HISTORY = "review-history.json"
VALID = ("approve", "revise", "drop", "")
MAX_NOTE = 20000
MAX_HISTORY = 20
_lock = threading.Lock()


class DecisionsError(ValueError):
    pass


class ConflictError(DecisionsError):
    """An ifDecision guard didn't match the item's current decision."""


def path_for(packet_dir) -> Path:
    return Path(packet_dir) / DECISIONS


def history_path_for(packet_dir) -> Path:
    return Path(packet_dir) / HISTORY


def read(packet_dir) -> dict:
    return _read_object(path_for(packet_dir))


def read_history(packet_dir) -> dict:
    return _read_object(history_path_for(packet_dir))


def _read_object(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        data = json.loads(read_regular(p).decode("utf-8"))  # never blocks on a FIFO or device, even one swapped in just now
    except NotRegularError:
        raise DecisionsError(f"{p} is not a regular file; not touching it") from None
    except OSError as e:
        raise DecisionsError(f"{p} can't be read ({e.strerror or e}); not touching it") from None
    except (ValueError, RecursionError) as e:
        raise DecisionsError(f"{p} is not valid JSON ({e}); not touching it") from None
    if not isinstance(data, dict):
        raise DecisionsError(f"{p} must hold a JSON object")
    return data


def validate_patch(patch) -> dict:
    if not isinstance(patch, dict) or not patch:
        raise DecisionsError("expected a JSON object with 'decision' and/or 'note'")
    extra = set(patch) - {"decision", "note", "ifDecision", "ifNote"}
    if extra:
        raise DecisionsError(f"unknown field(s): {', '.join(sorted(extra))}")
    if not ({"decision", "note"} & set(patch)):
        raise DecisionsError("expected 'decision' and/or 'note' (ifDecision only guards one of them)")
    if "ifDecision" in patch and patch["ifDecision"] not in VALID:
        raise DecisionsError("ifDecision must be one of approve, revise, drop or ''")
    if "ifNote" in patch and not isinstance(patch["ifNote"], str):
        raise DecisionsError("ifNote must be a string")
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
        old = state.get(item_id) or {}
        for key, field in (("ifDecision", "decision"), ("ifNote", "note")):
            if key in patch and (old.get(field) or "") != patch[key]:
                raise ConflictError(f"{field} is no longer {patch[key]!r}; nothing changed")
        rec = dict(old)
        rec.setdefault("decision", "")
        rec.setdefault("note", "")
        rec.update({k: v for k, v in patch.items() if not k.startswith("if")})
        rec["updatedAt"] = now_iso()
        left_revise = "decision" in patch and rec["decision"] != "revise"
        if left_revise and old.get("decision") == "revise" and str(old.get("note") or "").strip() and not rec["note"].strip():
            _archive(packet_dir, item_id, old["note"], old.get("updatedAt") or rec["updatedAt"])
        state[item_id] = rec
        atomic_write_json(path_for(packet_dir), state)
        return rec


def _archive(packet_dir, item_id: str, note: str, at: str) -> None:
    """Called under the lock, before the decisions write: a failure here leaves the note where it was."""
    history = read_history(packet_dir)
    entries = history.get(item_id)
    entries = (entries if isinstance(entries, list) else []) + [{"note": note, "decision": "revise", "at": at}]
    history[item_id] = entries[-MAX_HISTORY:]
    atomic_write_json(history_path_for(packet_dir), history)
