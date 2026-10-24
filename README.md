# review-desk

An asynchronous approval queue for agent drafts, fully local.

Coding agents produce things a human has to approve: ticket drafts, PR replies, plans, notes. Approving them inside a
chat that jumps between topics is lossy. The desk takes the review out of the conversation:

- an agent groups its drafts into a **review packet** (a directory with a `review.toml` manifest);
- you review the packet in a browser at your own pace, marking each item **approve**, **revise** or **drop**, with a note;
- every click and note saves to disk at once (`review-decisions.json`, beside the manifest);
- any agent, in any later session, reads the decisions back with `review-desk status` and acts on them.

The review lifecycle is decoupled from the rest of the conversation.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/packet-dark.png">
  <img src="docs/screenshots/packet-light.png" width="720"
       alt="A packet page: a tally bar (1 approved, 1 to revise, 1 dropped, 2 not reviewed) above cards for each draft, each with Approve, Revise, Drop and Clear buttons and a notes box; one card is approved, one marked revise with a note, one pending, one dropped.">
</picture>

*A packet page, in your system's light or dark theme ([light](docs/screenshots/packet-light.png),
[dark](docs/screenshots/packet-dark.png)). The bar keeps the tally; each card holds one decision and its note.*

Nothing leaves the machine. The server binds 127.0.0.1 only (unless you opt in to [sharing on your local network](#sharing-on-your-local-network)), and the one runtime dependency is `markdown-it-py`.

## Install

Needs [uv](https://docs.astral.sh/uv/) and Python 3.11 or later.

```sh
git clone https://github.com/ddl-h-morgan/review-desk.git review-desk
cd review-desk && uv sync
uv run --script bin/review-desk --help                  # once, with network access: builds the launcher's cached environment
ln -s "$PWD/bin/review-desk" ~/.local/bin/review-desk   # or: alias review-desk="$PWD/bin/review-desk"
```

`bin/review-desk` is a [PEP 723](https://peps.python.org/pep-0723/) script: `uv run --script` builds and caches its own
environment from the dependency block at the top of the file (no project install), and it imports the package from the
checkout's `src/`, so a `git pull` is all an update needs. The launcher runs `--offline`, which is why that environment
is built once up front (`uv sync` doesn't build it; without it the first run fails to resolve `markdown-it-py`). It works
through a symlink on macOS and Linux, and also as `uv run --script bin/review-desk ...`. The examples below assume
`review-desk` is on your `PATH`.

**Platforms:** macOS and Linux (POSIX; not Windows). The test suite passes on macOS, on Debian (glibc) and on Alpine
(musl, busybox), with Python 3.11 and newer. Notes for [sharing](#sharing-on-your-local-network) on Linux: the Share
tooltip's addresses come from `ip -o -4 addr show`, else `ifconfig`, else the one address the OS would route out of; the
`.local` name (`--share-mdns`) is only available when avahi is running (it uses the machine's host name, so set avahi's
`host-name` to match if you changed it); and a host firewall (ufw, firewalld, macOS "allow incoming connections" for
Python) may need to allow the desk's port before another device can reach it.

## Quick start

```sh
review-desk new ~/work/drafts --title "Release follow-ups"   # scaffold review.toml, then edit it
review-desk add ~/work/drafts
review-desk open ~/work/drafts                               # starts the desk if needed, opens the page
review-desk status ~/work/drafts                             # later, from any session
```

The desk serves every registered packet from one process, on port **8791** by default (`--port`, or `$REVIEW_DESK_PORT`).
The index at <http://127.0.0.1:8791/> lists packets with approve, revise, drop and pending counts, and when each was last
touched. Each packet page is rendered from its manifest and draft files on every request, so an edited draft shows with no rebuild. An
open packet page also picks up a changed draft by itself within a few seconds (only that draft's text is swapped, so
notes you're typing, open drafts and your scroll position stay put). A changed manifest (items, titles, facts) shows a
"Reload" prompt instead, so a reload never costs you open drafts. The bar's "Hide reviewed" checkbox hides approved and
dropped items (revise stays visible); it is per browser, and a card you just decided stays until you toggle.

<img src="docs/screenshots/index.png" width="720"
     alt="The desk's index page listing three packets, Release 2.4 follow-ups, Docs cleanup and Onboarding guide edits, each with approve, revise, drop and pending counts, its path, and when it was last touched.">

*The index: every registered packet, with its counts.*

The page checks the desk every few seconds. If the desk stops, a red banner says nothing is being saved and the controls
are disabled until it's back, so no click looks saved that wasn't.

<img src="docs/screenshots/offline.png" width="720"
     alt="A packet page with a red banner reading 'Not connected to the review desk: not saving', the status 'Not connected' in the bar, and the card's buttons and notes box greyed out.">

*The desk stopped while the page was open.*

## Browser

`review-desk open` picks the browser from `--browser NAME`, else `$REVIEW_DESK_BROWSER`, else the system default:

- **macOS:** a named browser is an application name, opened in a new window:
  `open -na "<NAME>" --args --new-window <url>`. Unset, it runs `open <url>` (the default browser).
- **Elsewhere:** Python's `webbrowser` (`webbrowser.get(NAME)` when one is named).

For example, to always get a new window of Brave on macOS, put this in your shell profile:

```sh
export REVIEW_DESK_BROWSER="Brave Browser"
```

`--no-browser` skips the browser and the desk start, and only prints the URL.

## Commands

All commands are non-interactive and exit 0 on success, 1 on error (message on stderr).

| Command | What it does |
|---|---|
| `new <dir> [--title T]` | Writes a commented `review.toml` (and `example-draft.md`) into `<dir>`. Refuses to overwrite. |
| `add <dir>` | Validates the manifest and registers the packet by absolute path. Idempotent. |
| `remove <dir>` | Unregisters the packet. Its files, decisions included, are untouched. |
| `list [--json]` | Registered packets, with counts and last-touched time. |
| `status <dir> [--json]` | Agent-readable summary: counts, each item's decision and note, `undecided` ids, `orphans` (decided ids no longer in the manifest). Reads the files directly, so the desk needn't be running. |
| `status [--json]` | Without a dir: one line per registered packet, plus how many have pending items. |
| `serve [--port N] [--background] [--reload] [--share]` | Runs the desk in the foreground, or with `--background` starts it detached (if not already up), prints the URL and exits. `--share` allows [sharing on your local network](#sharing-on-your-local-network), `--share-mdns` (with it) also accepts this machine's `.local` name. Log: `~/.local/state/review-desk/desk-<port>.log`. |
| `share [status\|on\|off] [--json] [--port N]` | Needs a desk running with `--share`. `status` (the default) shows whether the network window is open, its time left and URLs; `on` opens a 4-hour window, or resets a running one to a full 4 hours; `off` closes it. |
| `open [<dir>] [--port N] [--browser NAME] [--no-browser]` | Opens the index, or a packet, in a browser (see [Browser](#browser)), starting the desk first if needed. Registers `<dir>` if it isn't yet. `--no-browser` only prints the URL. |

The registry lives at `~/.local/state/review-desk/packets.json` (`$REVIEW_DESK_STATE` overrides the directory).

## Packet format

A packet is any directory holding `review.toml`:

```toml
title = "Release follow-ups"            # required
intro = """                             # optional, markdown, shown above the cards
What this batch is and what to decide.
"""
links = [{label = "Issue 1234", url = "https://example.com/issues/1234"}]   # optional
appendix = """                          # optional, markdown, shown below the cards (evidence tables etc.)
"""

[[items]]
id = "fix-the-foo"                  # required, stable slug: [a-z0-9][a-z0-9-]{0,79}; decisions are keyed by it
title = "Fix the foo"                   # required (inline markdown)
kind = "new Task"                       # optional (inline markdown)
recommendation = "**File** it."         # optional (inline markdown)
file = "fix-the-foo.md"             # optional: .md/.markdown/.txt, relative to the packet, must stay inside it
links = ["https://example.com", {label = "PR #1", url = "https://github.com/o/r/pull/1"}]   # optional
[items.facts]                           # optional extra rows on the card: label = inline markdown
Overlaps = "[issue 1](https://example.com/issues/1)"
```

Each item becomes a card. The card shows the recommendation, facts and links; **Read the draft** expands the item's
file, rendered as GitHub-flavoured markdown (CommonMark, plus tables, strikethrough and bare-URL links), so a draft
looks as it will on GitHub: a list can follow a line of text directly, and two-space indents nest. Long code lines
scroll sideways; tick **Wrap long lines** in the top bar to wrap them instead (a per-browser preference, kept in
`localStorage`):

<img src="docs/screenshots/draft-expanded.png" width="600"
     alt="One card, approved, with its draft expanded: a heading, a proposal as a bulleted list with a nested list, a code block whose long line wraps, and numbered acceptance criteria.">

**Working on the page itself:** every desk swaps changed `static/page.css` and `static/page.js` into open pages within a
few seconds, without a reload, so notes you're typing, open drafts and your scroll position survive. Python changes need
the desk restarted; `review-desk serve --reload` does that for you when a `.py` file in the package changes (set
`REVIEW_DESK_RELOAD=1` to have the desk that `open` starts do the same). It restarts the desk, not the supervisor, so
edits to `cli.py` or `reload.py` need `serve --reload` restarted by hand. A desk that is already running is never
replaced: stop it first, or `--reload` is ignored (the command says so).

## Changes made outside the page

Each card shows **Last changed <time> (5 min ago) · <what>** when something other than your own clicks has changed that
item; the "ago" part moves on by itself (just now, seconds, minutes, hours, days). Hover or focus it (Tab) for the full
list, newest first, in a scrollable box, each entry with its own "ago". Each entry says what kind of change it was:

| Label | What happened |
|---|---|
| Draft edited | The item's draft file changed on disk (compared by content: a save with the same text doesn't count). |
| Item details edited | The item's entry in `review.toml` changed (title, kind, recommendation, file, links, facts). Edits to the packet's intro or appendix aren't tied to an item and aren't listed. |
| Decision set by an agent / Note changed by an agent | A decision or note was written through the API without the page, which is how an agent clears a revise mark. The entry says what changed (`revise → not reviewed`, `note cleared`), never the note text. |
| Decisions file edited | `review-decisions.json` was edited on disk for that item. |

The desk records these in `review-changes.json` beside the manifest, so the list survives restarts. It compares content
hashes rather than watching file events (a hash never misses a net change, and an editor's save-by-rename is no
problem), and it only looks hard at packets you have open:

- **Open in a page:** the page's poll marks the packet open, and the desk scans it about once a second. The first poll
  after it was closed scans at once, so the page starts current.
- **Not open:** every 10 minutes. A change found by a scan that hadn't looked for a while (more than 5 seconds, including
  the first scan after a restart) is marked **possibly several changes**, since it may stand for more than one edit. Its
  time is the file's own modified time, not the later moment the scan noticed. A page that opens later still sees it.
- A packet's first scan is only a baseline and lists nothing. At most 50 entries are kept per item.
- A file modified in the last half second is left for the next pass, so a save that truncates and then writes is one
  change, not two. (A packet's first look is never delayed.)
- Packet files are opened without blocking and checked on the open file, so a FIFO or device in a packet, even one
  swapped in just before the read, can't hang a scan, a save or a page; a draft that turns into one shows as a draft change.

Your own writes are not listed: the page marks them with an `X-Review-Page: 1` header, and a write without it counts as
an agent's. That's bookkeeping, not security: anyone who can write can also send the header, and anyone who can write
the packet directory can rewrite or forge `review-changes.json` itself, so don't treat the log or the "by an agent"
labels as proof of who changed what. The scan is cheap (about 0.1 ms of CPU per packet scanned, so a pass over 200 packets of 20
drafts is about 20 ms), and a closed packet costs one such pass per 10 minutes. `review-changes.json` is derived data;
delete it and the desk starts a new baseline. Run one desk per state directory: the lock that keeps writes and scans from
interfering is per process.

## Sharing on your local network

By default only this machine can reach the desk. To review from a phone or another computer on the same network, start
the desk in share mode: `review-desk serve --share` (or `--background --share`, or `REVIEW_DESK_SHARE=1` for the desk
that `open` starts). Share mode alone exposes nothing: the desk listens on all interfaces but hangs up on every other
machine, without a response, until you open a **share window**, which lasts 4 hours:

- **In the page:** with share mode on, the top bar (and the index) shows a **Share on this network (4h)** button. Click
  it to start the window; it then shows a countdown (`Sharing 3:58:12`). While more than a minute has gone, a click
  resets the timer to 4:00:00; right after a start or reset (3:59:00 or more left), a click ends sharing. Hover the
  button while sharing to see the URLs to open from another device: one per network interface, plus the machine's
  `<name>.local` (mDNS) name if you started the desk with `--share-mdns` (see below) and the machine has one.
- **From a terminal or an agent:** `review-desk share on` (start, or reset to 4 hours), `review-desk share off`,
  `review-desk share status`. These call `POST /api/share` on localhost, which is the route behind the button:
  `curl -s -X POST http://127.0.0.1:8791/api/share -H 'X-Review-Desk: 1' -H 'Content-Type: application/json' -d '{"action": "on"}'`
  (`action` is `on`, `off` or `toggle`, the button's rule).

The window lives in the desk's memory: restarting the desk ends it, and nothing is shared again until someone opens a
new one. It also ends if the machine sleeps, and it only admits connections to the addresses this machine had when the
window opened, so closing the lid and opening it on another network doesn't carry the sharing along. Only the machine running the desk can open, reset or end a window; a device on the network sees the
countdown but its button is disabled. People on the network can read the packets and record decisions and notes, so
share only on a network you trust, and end the window when you're done. Use a URL from the tooltip;
other host names are refused.

**mDNS name (opt-in):** `serve --share --share-mdns` (or `REVIEW_DESK_SHARE_MDNS=1` together with sharing) also accepts
this machine's `<name>.local` name and lists it first in the tooltip: `scutil --get LocalHostName` on macOS, the host name
on Linux when avahi is running. It's off by default because anyone on the network can claim a `.local` name (macOS
renames itself to `<name>-2` after a conflict, but the desk looks its name up once at startup and doesn't follow), so
that name may not reach this machine. `--share-mdns` without `--share` is an error; the env switch without sharing is
ignored.

**Decisions** live beside the manifest in `review-decisions.json`:

```json
{"fix-the-foo": {"decision": "revise", "note": "shorter title", "updatedAt": "2026-10-20T18:55:27.184Z"}}
```

`decision` is `approve`, `revise`, `drop`, or `""` (cleared, counts as pending). This shape is a stable contract, so
existing decisions files keep working. Ids that leave the manifest keep their records and show up as `orphans` in `status`.

**Don't rename an item id once review has started**: the old decision becomes an orphan and the item shows as pending.

## Agent workflow

1. **Create the packet.** Put the drafts in one directory and write `review.toml` (`review-desk new <dir>` gives a
   template). One item per thing the reviewer must decide. Put your recommendation on the card, and anything that
   changes the decision (overlapping work, risks) in `facts`.
2. **`review-desk add <dir>`**, then **`review-desk open <dir>`**, and tell the reviewer it's waiting (the URL is
   printed). Don't ask for the decisions in chat; move on.
3. **Later, in any session: `review-desk status <dir> --json`.** Act only on what's there:
   - `approve`: do it (the click is the approval for that item, but follow your project's own rules for logging it);
   - `revise`: edit the draft per the `note`. The open page swaps the new text in within a few seconds, without a reload. Then clear the mark and note with `PUT {"decision": "", "note": "", "ifDecision": "revise", "ifNote": "<the note you acted on>"}` (see the skill; a 409 means the reviewer decided again or added to the note, so leave it). The desk keeps the old note in `review-history.json` and shows it on the card under "Earlier notes". Say which items changed; they get re-decided;
   - `drop`: don't do it; leave the file;
   - `undecided`: leave it alone; it's still waiting.
   To wait for the reviewer in the same session, run `bin/poll-packet <dir>` as a background command: it is read-only,
   and exits with `CHANGED` when any decision or note changes (backoff from 15 s to 10 min; `POLL_START`/`POLL_MAX`
   override), so the agent is re-invoked, then reads `status --json` as above.
4. Never write `review-decisions.json` yourself. Decisions are the reviewer's. (The desk's PUT API exists for the page.)

## Install the skill

`skills/review-desk/SKILL.md` is a [Claude Code skill](https://docs.claude.com/en/docs/claude-code/skills) that teaches an
agent the workflow above. Install it for all your projects, or for one:

```sh
ln -s "$PWD/skills/review-desk" ~/.claude/skills/review-desk          # every project (or cp -R)
ln -s "$PWD/skills/review-desk" /path/to/project/.claude/skills/review-desk   # one project
```

## Security

A local web server is reachable from every web page you open, so the desk treats the browser as hostile. Each rule has a
test (in `tests/test_server.py`, `tests/test_share.py` and `tests/test_changes.py`), and `bin/mutation-check` breaks each rule in turn to prove the tests catch it.

- Binds 127.0.0.1 only. With `--share` it binds all interfaces, but a peer that isn't this machine is dropped at accept
  time (no thread, no response) unless a share window is open, its address is in an RFC 1918 range (10/8, 172.16/12,
  192.168/16), and it reached an address this machine had when the window opened. That is a private-address check, not a
  subnet check: a VPN or routed private network that reaches the same addresses passes it too.
- Concurrent connections are capped in two separate pools, 256 from this machine and 16 from the network, and every
  connection is hung up after 30 seconds. So no peer on the network can exhaust the desk's threads, by opening thousands
  of connections or by trickling a request, or crowd out the owner. A process on this machine can still fill the
  owner's own pool for up to 30 seconds. Handler threads get an 8 MB stack, because musl's 128 KB default lets deeply
  nested JSON in a packet file crash the process.
- Every request needs `Host` to be exactly `127.0.0.1:<port>` or `localhost:<port>`, which defeats DNS rebinding. In
  share mode a peer on the network may also use the address the connection arrived on, and with `--share-mdns` this
  machine's own mDNS name, never other names. A loopback peer may use neither: a `.local` name can be claimed by anyone
  on the network, which is why `--share-mdns` is off by default.
- Opening, resetting or ending a share window works only from a loopback peer with a loopback `Host`, plus the write
  rules below; a device on the network can't extend its own access.
- Writes need `X-Review-Desk: 1` (a cross-origin form or no-cors fetch can't send a custom header), `Content-Type:
  application/json`, a same-origin `Origin` when one is sent, and no cross-site `Sec-Fetch-Site`.
- Only registered packets and item ids from the manifest are reachable. URL segments must be plain slugs (no decoding,
  no dots), and item files are served only through the manifest mapping, never by filename. The manifest rejects
  absolute paths, `..`, and symlinks that resolve outside the packet.
- Request bodies are capped at 64 KiB, notes at 20,000 characters.
- The change scan only reads and hashes files the manifest already vouches for; it never serves them, and
  `/api/p/<id>/changes` returns just timestamps, kinds and short labels (not draft text or notes).
- Untrusted text is escaped: markdown renders with raw HTML disabled, and only http(s), mailto and relative links (and image sources) survive.
  Pages also carry a strict CSP (`default-src 'none'`, script and style from the desk only), `nosniff`, `DENY` framing,
  and `no-referrer`.
- Writes merge one item at a time under a lock and replace the file atomically (temp file, fsync, rename). A corrupt
  decisions file is never overwritten.

Not covered: other processes running as your user can read and write the files directly. That's the same trust boundary
as the files themselves.

## Optional: run at login on macOS (example only)

`~/Library/LaunchAgents/local.review-desk.plist`, with the `/path/to/...` placeholders replaced by absolute paths (launchd doesn't expand `~`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.review-desk</string>
  <key>ProgramArguments</key><array>
    <string>/path/to/review-desk/bin/review-desk</string><string>serve</string><string>--port</string><string>8791</string>
  </array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/path/to/home/.local/state/review-desk/launchd.log</string>
</dict></plist>
```

Load it with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.review-desk.plist`.

## Development

`uv run --offline pytest -q` runs the tests; `bin/mutation-check` breaks each security and merge rule in turn and
checks the suite catches it. `CLAUDE.md` has the working notes. `docs/screenshots/make.py` regenerates the screenshots
from dummy packets on a scratch desk (port 8794); see its header for how to run it.

## License

Apache License 2.0. See [LICENSE](LICENSE).
