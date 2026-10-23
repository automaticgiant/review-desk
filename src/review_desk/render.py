"""HTML for the index and packet pages. Everything from a manifest or draft is untrusted text:

- plain fields go through html.escape;
- markdown is GitHub-style (CommonMark plus tables, strikethrough and bare-URL links, via markdown-it-py), with raw
  HTML disabled, so tags render as text;
- href/src values survive only if they are http(s), mailto, fragment or scheme-less relative URLs (checked on the
  final, entity-decoded value markdown-it puts in the attribute).
The server also sends a CSP that allows only its own script and stylesheet, as a second layer.
"""
from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path
from urllib.parse import unquote

from markdown_it import MarkdownIt

from .fsutil import read_regular
from .manifest import Item, Link, Packet

_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")
_SAFE_SCHEMES = {"http", "https", "mailto"}


def safe_url(url: str) -> bool:
    """True for http(s), mailto, fragment and scheme-less relative URLs. Percent-decoding first and dropping
    control characters and spaces (which browsers ignore inside a scheme) means "java%09script:" is refused too."""
    cleaned = re.sub(r"[\x00-\x20\x7f]+", "", unquote(url or ""))
    m = _SCHEME_RE.match(cleaned)
    return m is None or m.group(1).lower() in _SAFE_SCHEMES


def _url_filter(state) -> None:
    """Core rule, run last: drop any unsafe href/src (the link text or alt stays), and open web links in a new tab."""
    stack = list(state.tokens)
    while stack:
        tok = stack.pop()
        if tok.children:
            stack.extend(tok.children)
        for attr in ("href", "src"):
            v = tok.attrGet(attr)
            if v is not None and not safe_url(str(v)):
                del tok.attrs[attr]
        if tok.type == "link_open" and str(tok.attrGet("href") or "").startswith(("http://", "https://")):
            tok.attrSet("rel", "noopener noreferrer")
            tok.attrSet("target", "_blank")


_MD = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable(["table", "strikethrough", "linkify"])
_MD.validateLink = lambda url: True  # every URL is checked by _url_filter, which keeps the text of a refused link
_MD.core.ruler.push("url_filter", _url_filter)


def md(text: str) -> str:
    return _MD.render(text or "")


def md_inline(text: str) -> str:
    return _MD.renderInline(text or "").strip()


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
        n_done = sum(1 for r in rows if not r.get("error") and r["counts"]["pending"] == 0)
        if n_done:
            listing = (f'<label class="done-toggle"><input type="checkbox" id="done-toggle"> '
                       f'Show packets with nothing pending ({n_done})</label>\n') + listing
    else:
        listing = '<p class="intro">No packets registered. Run <code>review-desk add &lt;dir&gt;</code>.</p>'
    body = f'''<main class="wrap">
  <h1>Review desk</h1>
  <p class="intro">Packets waiting for review. Decisions save beside each packet's <code>review.toml</code> as you click.</p>
  <div class="share" id="share" hidden></div>
  {listing}
</main>
<script src="/static/page.js"></script>'''
    return _shell("Review desk", body, ' data-page="index"')


def _card(n: int, item: Item, draft_html_: str, pid: str) -> str:
    sid = e(item.id)
    facts = [("Recommendation", md_inline(item.recommendation))] if item.recommendation else []
    facts += [(e(k), md_inline(v)) for k, v in item.facts.items()]
    if item.links:
        facts.append(("Links", _links(item.links)))
    if item.file:
        facts.append(("Draft", f'<code>{e(item.file)}</code> · <a href="/p/{e(pid)}/raw/{sid}">raw</a>'))
    dl = "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in facts)
    return f'''
<article class="card" data-id="{sid}" id="{sid}" data-draft-version="{draft_version(draft_html_)}">
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
  <div class="note-slot"><div class="note-box">
    <label class="note-label" for="{sid}-note">Notes for the agent</label>
    <textarea id="{sid}-note" data-note rows="2" placeholder="What to change, who to ask, anything else"></textarea>
    <p class="saved" data-saved aria-live="polite"></p>
  </div></div>
  <details class="earlier" data-earlier hidden><summary>Earlier notes (<span data-earlier-count>0</span>)</summary><ol data-earlier-list></ol></details>
  <p class="saved" data-draft-updated aria-live="polite"></p>
  <div class="changed" data-changed hidden>
    <button type="button" class="changed-btn" data-changed-text aria-describedby="{sid}-changes"></button>
    <div class="change-tip" id="{sid}-changes" role="tooltip"><ul data-changed-list tabindex="0" aria-label="Changes to this item, newest first"></ul></div>
  </div>
  <details class="draft"><summary>Read the draft</summary><div class="draft-body">{draft_html_}</div></details>
</article>'''


def draft_html(packet: Packet, item: Item) -> str:
    p = packet.file_path(item)
    if p is None:
        return "<p>No draft file.</p>"
    if not p.is_file():
        return f'<p class="perror">Draft file not found: <code>{e(item.file)}</code></p>'
    try:
        text = read_regular(p).decode("utf-8", errors="replace")
    except OSError:  # became a FIFO or vanished since the check
        return f'<p class="perror">Draft file not found: <code>{e(item.file)}</code></p>'
    if p.suffix.lower() == ".txt":
        return f"<pre>{e(text)}</pre>"
    return md(text)


HELP_DIALOG = """<dialog id="help-dialog" class="help" aria-labelledby="help-title">
  <h2 id="help-title">How this works</h2>
  <p>An agent drafted these items and is waiting on you. Decide each one; nothing is sent or acted on until you approve it.</p>
  <dl>
    <dt>Approve</dt><dd>The draft is good as written. The agent may use it.</dd>
    <dt>Revise</dt><dd>Close, but change it. Say what in <em>Notes for the agent</em>. The agent edits the draft file and
      tells you which items changed. The new text appears here on its own within a few seconds, without disturbing
      your notes or the drafts you have open. The item stays on Revise until you decide again, or the agent has revised it and sends it back to Not reviewed. Once the agent has revised, your note is cleared and kept
      under <em>Earlier notes</em> on the card.</dd>
    <dt>Drop</dt><dd>Don't use it.</dd>
    <dt>Clear</dt><dd>Back to not reviewed.</dd>
  </dl>
  <p>Notes save as you type. Decisions save as you click, to <code>review-decisions.json</code> beside the packet's
    <code>review.toml</code>. The agent reads them with <code>review-desk status</code> the next time it checks, so
    tell it to look once you're done. The page only talks to this machine.</p>
  <form method="dialog"><button id="help-close" type="submit">Close</button></form>
</dialog>"""


def draft_version(rendered: str) -> str:
    """A short fingerprint of a rendered draft; the page compares it to see whether an agent changed the draft."""
    return hashlib.sha1(rendered.encode("utf-8")).hexdigest()[:12]


def layout_version(pid: str, packet: Packet) -> str:
    """Fingerprint of everything on the packet page except the draft bodies (those are swapped in place)."""
    return draft_version(_packet_html(pid, packet, False, ""))


def packet_page(pid: str, packet: Packet) -> str:
    return _packet_html(pid, packet, True, layout_version(pid, packet))


def _packet_html(pid: str, packet: Packet, drafts: bool, layout: str) -> str:
    n = len(packet.items)
    cards = "\n".join(_card(i, it, draft_html(packet, it) if drafts else "", pid) for i, it in enumerate(packet.items, 1))
    links = f'<p class="plinks">{_links(packet.links)}</p>' if packet.links else ""
    body = f'''<div class="offline" role="alert" id="offline" hidden>
  <strong>Not connected to the review desk: not saving.</strong>
  Clicks and notes on this page won't be kept until it reconnects. Start the desk with <code>review-desk serve</code>
  (or <code>review-desk open</code>); this page reconnects on its own.
</div>
<div class="notice" id="layout-changed" role="status" hidden>
  The packet's manifest changed (items, titles or facts). <a href="">Reload</a> to see it; your notes are already saved.
</div>
<div class="bar" id="bar">
  <a class="back" href="/">← All packets</a>
  <strong>{n} items</strong>
  <span class="pill approve" id="n-approve">0 approved</span>
  <span class="pill revise" id="n-revise">0 to revise</span>
  <span class="pill drop" id="n-drop">0 dropped</span>
  <span class="pill" id="n-open">{n} not reviewed</span>
  <label class="wrap-toggle"><input type="checkbox" id="wrap-toggle"> Wrap long lines</label>
  <label class="wrap-toggle"><input type="checkbox" id="hide-reviewed-toggle"> <span id="hide-reviewed-label">Hide reviewed</span></label>
  <span class="share" id="share" hidden></span>
  <button type="button" id="help-toggle" class="help-toggle" aria-haspopup="dialog" aria-controls="help-dialog" aria-expanded="false">How this works</button>
  <span class="conn" id="conn">Connecting…</span>
</div>
{HELP_DIALOG}
<main class="wrap">
  <h1>{e(packet.title)}</h1>
  <section class="intro">{md(packet.intro)}{links}
    <p>Decisions and notes save to <code>{e(display_path(packet.dir / "review-decisions.json"))}</code> as you go.</p>
  </section>
  {cards}
  <section class="evidence">{md(packet.appendix) if packet.appendix else ""}</section>
</main>
<script src="/static/page.js"></script>'''
    return _shell(packet.title, body, f' data-page="packet" data-pid="{e(pid)}" data-layout-version="{e(layout)}"')


def error_page(title: str, message: str) -> str:
    return _shell(title, f'<main class="wrap"><p><a href="/">← All packets</a></p><h1>{e(title)}</h1>'
                         f'<p class="perror">{e(message)}</p></main>')
