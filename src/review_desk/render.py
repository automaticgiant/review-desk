"""HTML for the index and packet pages. Everything from a manifest or draft is untrusted text:

- plain fields go through html.escape;
- markdown goes through Python-Markdown with raw HTML disabled (block and inline), so tags render as text;
- href/src values survive only if they are http(s), mailto, fragment or scheme-less relative URLs.
The server also sends a CSP that allows only its own script and stylesheet, as a second layer.
"""
from __future__ import annotations

import html
import re
from pathlib import Path
from xml.etree.ElementTree import Element

import markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor

from .manifest import Item, Link, Packet

_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")
_SAFE_SCHEMES = {"http", "https", "mailto"}


def safe_url(url: str) -> bool:
    cleaned = re.sub(r"[\x00-\x20\x7f]+", "", url or "")
    m = _SCHEME_RE.match(cleaned)
    return m is None or m.group(1).lower() in _SAFE_SCHEMES


class _UrlFilter(Treeprocessor):
    def run(self, root: Element):
        for el in root.iter():
            for attr in ("href", "src"):
                v = el.get(attr)
                if v is not None and not safe_url(v):
                    del el.attrib[attr]
            if el.tag == "a" and el.get("href", "").startswith(("http://", "https://")):
                el.set("rel", "noopener noreferrer")
                el.set("target", "_blank")


class _NoRawHtml(Extension):
    def extendMarkdown(self, md):
        md.preprocessors.deregister("html_block")
        md.inlinePatterns.deregister("html")
        md.treeprocessors.register(_UrlFilter(md), "url_filter", 0)


def md(text: str) -> str:
    return markdown.markdown(text or "", extensions=["tables", "fenced_code", "sane_lists", _NoRawHtml()])


def md_inline(text: str) -> str:
    h = md(text).strip()
    m = re.fullmatch(r"<p>(.*)</p>", h, flags=re.S)
    return m.group(1) if m else h


e = html.escape


def display_path(path) -> str:
    """A path for display: under the home directory it starts with "~", so a
    screenshot or screen share doesn't show the username."""
    p = Path(path)
    try:
        return "~/" + str(p.relative_to(Path.home()))
    except ValueError:
        return str(p)


def _links(links: list[Link]) -> str:
    return ", ".join(f'<a href="{e(l.url)}" rel="noopener noreferrer" target="_blank">{e(l.label)}</a>' for l in links)


def _shell(title: str, body: str, body_attrs: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{e(title)} · review desk</title>
<link rel="stylesheet" href="/static/page.css">
</head>
<body{body_attrs}>
{body}
</body>
</html>
"""


COUNT_KEYS = ("approve", "revise", "drop", "pending")


def index_page(rows: list[dict]) -> str:
    """rows: dicts from summary.summarize_entry (id, path, title, counts, lastTouched, error)."""
    if rows:
        lis = []
        for r in rows:
            if r.get("error"):
                lis.append(f'<li class="packet broken"><div class="ptitle">{e(display_path(r["path"]))}</div>'
                           f'<p class="perror">{e(r["error"])}</p></li>')
                continue
            c = r["counts"]
            pills = "".join(f'<span class="pill {k}">{c[k]} {k}</span>' for k in COUNT_KEYS)
            touched = e(r["lastTouched"] or "never")
            lis.append(f'''<li class="packet{' done' if c['pending'] == 0 else ''}">
  <a class="ptitle" href="/p/{e(r["id"])}/">{e(r["title"])}</a>
  <div class="pcounts"><strong>{c["total"]} items</strong> {pills}</div>
  <p class="pmeta"><code>{e(display_path(r["path"]))}</code> · last touched <time data-iso="{touched}">{touched}</time></p>
</li>''')
        listing = '<ul class="packets">' + "\n".join(lis) + "</ul>"
    else:
        listing = '<p class="intro">No packets registered. Run <code>review-desk add &lt;dir&gt;</code>.</p>'
    body = f'''<main class="wrap">
  <h1>Review desk</h1>
  <p class="intro">Packets waiting for review. Decisions save beside each packet's <code>review.toml</code> as you click.</p>
  {listing}
</main>
<script src="/static/page.js"></script>'''
    return _shell("Review desk", body, ' data-page="index"')


def _card(n: int, item: Item, draft_html: str, pid: str) -> str:
    sid = e(item.id)
    facts = [("Recommendation", md_inline(item.recommendation))] if item.recommendation else []
    facts += [(e(k), md_inline(v)) for k, v in item.facts.items()]
    if item.links:
        facts.append(("Links", _links(item.links)))
    if item.file:
        facts.append(("Draft", f'<code>{e(item.file)}</code> · <a href="/p/{e(pid)}/raw/{sid}">raw</a>'))
    dl = "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in facts)
    return f'''
<article class="card" data-id="{sid}" id="{sid}">
  <header class="card-head">
    <span class="num">{n}</span>
    <div class="head-text">
      <h2>{md_inline(item.title)}</h2>
      <p class="kind">{md_inline(item.kind)}</p>
    </div>
    <span class="status" data-status>Not reviewed</span>
  </header>
  <dl class="facts">{dl}</dl>
  <div class="decide" role="group" aria-label="Decision for item {n}">
    <button type="button" data-decision="approve">Approve</button>
    <button type="button" data-decision="revise">Revise</button>
    <button type="button" data-decision="drop">Drop</button>
    <button type="button" data-decision="" class="clear">Clear</button>
  </div>
  <label class="note-label" for="{sid}-note">Notes for the agent</label>
  <textarea id="{sid}-note" data-note rows="2" placeholder="What to change, who to ask, anything else"></textarea>
  <p class="saved" data-saved aria-live="polite"></p>
  <details class="draft"><summary>Read the draft</summary><div class="draft-body">{draft_html}</div></details>
</article>'''


def draft_html(packet: Packet, item: Item) -> str:
    p = packet.file_path(item)
    if p is None:
        return "<p>No draft file.</p>"
    if not p.is_file():
        return f'<p class="perror">Draft file not found: <code>{e(item.file)}</code></p>'
    text = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix.lower() == ".txt":
        return f"<pre>{e(text)}</pre>"
    return md(text)


def packet_page(pid: str, packet: Packet) -> str:
    n = len(packet.items)
    cards = "\n".join(_card(i, it, draft_html(packet, it), pid) for i, it in enumerate(packet.items, 1))
    links = f'<p class="plinks">{_links(packet.links)}</p>' if packet.links else ""
    body = f'''<div class="offline" role="alert" id="offline" hidden>
  <strong>Not connected to the review desk: not saving.</strong>
  Clicks and notes on this page won't be kept until it reconnects. Start the desk with <code>review-desk serve</code>
  (or <code>review-desk open</code>); this page reconnects on its own.
</div>
<div class="bar" id="bar">
  <a class="back" href="/">← All packets</a>
  <strong>{n} items</strong>
  <span class="pill approve" id="n-approve">0 approved</span>
  <span class="pill revise" id="n-revise">0 to revise</span>
  <span class="pill drop" id="n-drop">0 dropped</span>
  <span class="pill" id="n-open">{n} not reviewed</span>
  <span class="conn" id="conn">Connecting…</span>
</div>
<main class="wrap">
  <h1>{e(packet.title)}</h1>
  <section class="intro">{md(packet.intro)}{links}
    <p>Decisions and notes save to <code>{e(display_path(packet.dir / "review-decisions.json"))}</code> as you go.</p>
  </section>
  {cards}
  <section class="evidence">{md(packet.appendix) if packet.appendix else ""}</section>
</main>
<script src="/static/page.js"></script>'''
    return _shell(packet.title, body, f' data-page="packet" data-pid="{e(pid)}"')


def error_page(title: str, message: str) -> str:
    return _shell(title, f'<main class="wrap"><p><a href="/">← All packets</a></p><h1>{e(title)}</h1>'
                         f'<p class="perror">{e(message)}</p></main>')
