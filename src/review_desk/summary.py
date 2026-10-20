"""Agent-readable summaries of a packet's decisions (used by `status`, `list` and the index page)."""
from __future__ import annotations

from pathlib import Path

from . import decisions as dec
from .manifest import ManifestError, Packet, load
from .registry import Entry


def summarize(packet: Packet, state: dict) -> dict:
    counts = {"approve": 0, "revise": 0, "drop": 0, "pending": 0, "total": len(packet.items)}
    items, undecided = [], []
    for it in packet.items:
        rec = state.get(it.id) or {}
        d = rec.get("decision") or None
        counts[d if d in ("approve", "revise", "drop") else "pending"] += 1
        if d is None:
            undecided.append(it.id)
        fp = packet.file_path(it)
        items.append({"id": it.id, "title": it.title, "kind": it.kind, "file": it.file,
                      "fileExists": bool(fp and fp.is_file()), "decision": d, "note": rec.get("note", ""),
                      "updatedAt": rec.get("updatedAt")})
    ids = {it.id for it in packet.items}
    stamps = [r.get("updatedAt") for r in state.values() if isinstance(r, dict) and r.get("updatedAt")]
    return {"title": packet.title, "path": str(packet.dir), "decisionsFile": str(dec.path_for(packet.dir)),
            "counts": counts, "undecided": undecided, "orphans": sorted(k for k in state if k not in ids),
            "lastTouched": max(stamps) if stamps else None, "items": items}


def summarize_dir(directory) -> dict:
    """Raises ManifestError or DecisionsError."""
    p = load(directory)
    return summarize(p, dec.read(p.dir))


def summarize_entry(entry: Entry) -> dict:
    row = {"id": entry.id, "path": entry.path}
    try:
        if not Path(entry.path).is_dir():
            raise ManifestError(f"directory missing: {entry.path}")
        s = summarize_dir(entry.path)
        row.update(title=s["title"], counts=s["counts"], lastTouched=s["lastTouched"], undecided=s["undecided"])
    except (ManifestError, dec.DecisionsError, OSError) as e:
        row["error"] = str(e)
    return row


def text_report(s: dict) -> str:
    c = s["counts"]
    lines = [f"{s['title']}  ({s['path']})",
             f"{c['total']} items: {c['approve']} approve, {c['revise']} revise, {c['drop']} drop, {c['pending']} pending"
             f"; last touched {s['lastTouched'] or 'never'}", ""]
    for it in s["items"]:
        mark = it["decision"] or "pending"
        missing = "" if it["fileExists"] or not it["file"] else "  [draft file missing]"
        lines.append(f"{mark:<8} {it['id']}  {it['title']}{missing}")
        if it["note"]:
            for i, ln in enumerate(it["note"].splitlines() or [""]):
                lines.append(("         note: " if i == 0 else "               ") + ln)
    lines.append("")
    lines.append("undecided: " + (", ".join(s["undecided"]) or "none"))
    if s["orphans"]:
        lines.append("orphans (decided, not in manifest): " + ", ".join(s["orphans"]))
    return "\n".join(lines)
