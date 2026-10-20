"""Parse and validate a packet manifest (review.toml).

A packet is any directory holding review.toml. Every path an item names must resolve (symlinks included) inside that
directory and end in .md or .txt; the server serves item files only through this validated mapping, never by URL path.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST = "review.toml"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
FILE_SUFFIXES = (".md", ".markdown", ".txt")
SAFE_URL_RE = re.compile(r"^(https?://|mailto:)", re.I)


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class Link:
    label: str
    url: str


@dataclass
class Item:
    id: str
    title: str
    kind: str = ""
    recommendation: str = ""
    file: str | None = None
    links: list[Link] = field(default_factory=list)
    facts: dict[str, str] = field(default_factory=dict)


@dataclass
class Packet:
    dir: Path
    title: str
    intro: str = ""
    links: list[Link] = field(default_factory=list)
    appendix: str = ""
    items: list[Item] = field(default_factory=list)

    def item(self, item_id: str) -> Item | None:
        return next((i for i in self.items if i.id == item_id), None)

    def file_path(self, item: Item) -> Path | None:
        return _resolve_file(self.dir, item.file, item.id) if item.file else None


def slugify(text: str) -> str:
    """Lowercase, runs of non [a-z0-9-] -> '-', trimmed, at most 80 chars. Packet ids derive from it, so changing it
    would change the ids of registered packets (and their URLs)."""
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")[:80]


def _str(d: dict, key: str, where: str, required=False) -> str:
    v = d.get(key, "")
    if not isinstance(v, str):
        raise ManifestError(f"{where}: '{key}' must be a string")
    if required and not v.strip():
        raise ManifestError(f"{where}: '{key}' is required")
    return v


def _links(raw, where: str) -> list[Link]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestError(f"{where}: 'links' must be a list")
    out = []
    for x in raw:
        if isinstance(x, str):
            label, url = x, x
        elif isinstance(x, dict):
            url = x.get("url")
            label = x.get("label") or url
        else:
            raise ManifestError(f"{where}: each link is a URL string or {{label, url}}")
        if not isinstance(url, str) or not isinstance(label, str) or not SAFE_URL_RE.match(url):
            raise ManifestError(f"{where}: link URLs must start with http://, https:// or mailto: (got {url!r})")
        out.append(Link(label, url))
    return out


def _resolve_file(root: Path, rel: str, where: str) -> Path:
    if Path(rel).is_absolute() or rel.startswith("~"):
        raise ManifestError(f"{where}: 'file' must be relative to the packet directory")
    if not rel.lower().endswith(FILE_SUFFIXES):
        raise ManifestError(f"{where}: 'file' must end in .md, .markdown or .txt")
    root = root.resolve()
    p = (root / rel).resolve()
    if not p.is_relative_to(root):
        raise ManifestError(f"{where}: 'file' resolves outside the packet directory")
    return p


def load(directory) -> Packet:
    root = Path(directory).expanduser().resolve()
    mf = root / MANIFEST
    if not mf.is_file():
        raise ManifestError(f"no {MANIFEST} in {root}")
    try:
        data = tomllib.loads(mf.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(f"{mf}: invalid TOML: {e}") from None
    title = _str(data, "title", MANIFEST, required=True)
    intro = _str(data, "intro", MANIFEST)
    appendix = _str(data, "appendix", MANIFEST)
    links = _links(data.get("links"), MANIFEST)
    raw_items = data.get("items", [])
    if not isinstance(raw_items, list) or not all(isinstance(x, dict) for x in raw_items):
        raise ManifestError(f"{MANIFEST}: 'items' must be an array of tables ([[items]])")
    items, seen = [], set()
    for n, r in enumerate(raw_items, 1):
        where = f"{MANIFEST} item {n}"
        iid = r.get("id")
        if not isinstance(iid, str) or not ID_RE.match(iid):
            raise ManifestError(f"{where}: 'id' must be a slug matching {ID_RE.pattern} (got {iid!r})")
        if iid in seen:
            raise ManifestError(f"{where}: duplicate id {iid!r}")
        seen.add(iid)
        where = f"{MANIFEST} item {iid!r}"
        f = r.get("file")
        if f is not None:
            if not isinstance(f, str) or not f:
                raise ManifestError(f"{where}: 'file' must be a non-empty string")
            _resolve_file(root, f, where)
        facts = r.get("facts", {})
        if not isinstance(facts, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in facts.items()):
            raise ManifestError(f"{where}: 'facts' must be a table of label = markdown string")
        items.append(Item(id=iid, title=_str(r, "title", where, required=True), kind=_str(r, "kind", where),
                          recommendation=_str(r, "recommendation", where), file=f,
                          links=_links(r.get("links"), where), facts=dict(facts)))
    return Packet(dir=root, title=title, intro=intro, links=links, appendix=appendix, items=items)


SCAFFOLD = '''# review-desk packet manifest. Format: the review-desk README, "Packet format".
title = {title}
intro = """
What this batch is, and what the reviewer should decide. Markdown.
"""
# links = [{{label = "Tracker", url = "https://example.com/issues/1234"}}]

[[items]]
id = "example-draft"            # stable slug; decisions are keyed by it, so don't rename after review starts
title = "Example draft"
kind = "new Task"
recommendation = "**File** it."
file = "example-draft.md"       # markdown, relative to this directory
# links = ["https://example.com"]
# [items.facts]
# Overlaps = "[issue 1](https://example.com/issues/1)"
'''
