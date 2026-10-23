---
name: review-desk
description: Use when a batch of drafts needs a human's approval (ticket drafts, PR replies, comments, plans, messages); put them in a review packet instead of asking in chat. The human decides each item (approve, revise, drop) in a local browser page, and you read the decisions back later with `review-desk status --json`.
---

# review-desk

review-desk is a local approval desk, loopback-only unless the human opts in to sharing on their network (below). You write drafts into a **review packet** (a directory with a
`review.toml` manifest and markdown files), the human marks each item **approve**, **revise** or **drop** with a note in
the browser, and the decisions are saved to `review-decisions.json` beside the manifest. You read them back, in this
session or a later one, and act on them.

Use it instead of asking "should I send these?" in chat when there are several things to approve, or when the review
can happen later.

## Install (once)

Needs [uv](https://docs.astral.sh/uv/) and Python 3.11 or later.

```sh
git clone https://github.com/ddl-h-morgan/review-desk.git review-desk
cd review-desk && uv sync
uv run --script bin/review-desk --help                    # once, with network: builds the launcher's cached environment
ln -s "$PWD/bin/review-desk" ~/.local/bin/review-desk     # or: alias review-desk="$PWD/bin/review-desk"
```

Optional settings (environment variables):

- `REVIEW_DESK_BROWSER`: the browser `open` uses. On macOS, an app name opened in a new window, e.g.
  `REVIEW_DESK_BROWSER="Firefox"`. Unset: the system default. `--browser NAME` overrides it per call.
- `REVIEW_DESK_PORT`: the desk port (default 8791). `--port N` overrides it per call.
- `REVIEW_DESK_STATE`: where the packet registry and desk logs live (default `~/.local/state/review-desk`).

Check it works: `review-desk --help`.

## Is the desk running?

```sh
curl -s -H 'Host: 127.0.0.1:8791' http://127.0.0.1:8791/api/health    # {"app": "review-desk", ...} if it is
review-desk serve --background                                          # starts it if it isn't; prints the URL
```

`review-desk open` also starts the desk if needed. `review-desk status` reads the files directly and works with the desk
down.

## Workflow

1. **Scaffold the packet:** `review-desk new <dir> --title "<what this batch is>"`. It writes a commented `review.toml`
   and `example-draft.md`, and refuses to overwrite an existing manifest.
2. **Write the drafts and fill `review.toml`:** one `[[items]]` entry per thing the human must decide, each with a stable
   `id` (a slug: `[a-z0-9][a-z0-9-]{0,79}`), a `title`, and usually a `file` (a `.md`, `.markdown` or `.txt` path inside
   the packet). Put your recommendation in `recommendation`, and anything that changes the decision (overlaps, risks)
   in `[items.facts]`. Delete `example-draft.md` and its item if you don't use them.
3. **Register it:** `review-desk add <dir>`. This validates the manifest; fix any error it prints and run it again.
4. **Open it:** `review-desk open <dir>`. It prints the packet URL (`http://127.0.0.1:<port>/p/<id>/`). Use
   `--no-browser` if you only want the URL.
5. **Tell the human** the URL and what the packet holds, then move on. Don't ask for the decisions in chat.
6. **Later, read the decisions:** `review-desk status <dir> --json`. Each item has `decision` (`approve`, `revise`,
   `drop`, or `null` for pending) and `note`; `undecided` lists the pending ids, and `orphans` lists decided ids no longer
   in the manifest.
7. **Act only on what's decided:**
   - `approve`: do it, as drafted (plus any small change the note asks for).
   - `revise`: treat the note as instructions. Edit the draft file; the open page swaps the new text in within a few seconds (no reload, so the human's notes and open drafts are undisturbed). Then clear the revise
     mark and the note so the item goes back to pending (see "Clear a revise mark and its note" below), and tell the human which items changed so
     they can decide again. Don't act on a revised item until it's approved.
   - `drop`: don't do it. Leave the file.
   - pending (`null`): don't act; it's still waiting.

## Clear a revise mark and its note

After you have edited a revised item's draft, put it back to pending and clear its note. This is the one write you make to the desk's API,
and only for `revise` items you have just revised (never to approve, drop or decide anything):

```sh
curl -s -X PUT http://127.0.0.1:8791/api/p/<packet-id>/items/<item-id> \
  -H 'X-Review-Desk: 1' -H 'Content-Type: application/json' \
  -d '{"decision": "", "note": "", "ifDecision": "revise", "ifNote": "<the note exactly as status returned it>"}'
```

`ifDecision` and `ifNote` make the clear conditional: it applies only while the item is still on `revise` and its note is
still the text you acted on (copy it from `status --json`, character for character). If the human has decided again since
(say they approved the revised draft with a note) or added to the note, the desk answers 409, changes nothing, and you
leave the item alone: it needs a fresh look.

`<packet-id>` is the segment after `/p/` in the packet URL that `review-desk open` prints. Use your `--port` in the URL if the desk
isn't on 8791. The desk archives the old note itself before clearing it (`review-history.json` beside the manifest) and shows it on
the card under "Earlier notes", so the human still sees what they asked for. Don't write that file. Check with `review-desk status <dir> --json`
that the item now shows `decision: null` and an empty `note`.

## Sharing with the local network (only if the human asks)

The desk is loopback-only. If the human wants to review from another device (phone, laptop), the desk must have been
started with `--share` (or `REVIEW_DESK_SHARE=1`). A desk that is already running is never switched over: say so, and ask
the human to stop it (find it with `lsof -nP -iTCP:8791 -sTCP:LISTEN`, then `kill <pid>`; use the desk's port) before you
start one with `review-desk serve --background --share`. Share mode alone exposes
nothing; the network can reach the desk only while a 4-hour share window is open:

```sh
review-desk share status --json   # enabled? active? remaining seconds, and the URLs to give the human
review-desk share on              # open a 4-hour window, or reset a running one to a full 4 hours (bump)
review-desk share off             # close it
```

Only do this when the human asked for it, and close the window when they say they're done (it also ends by itself after
4 hours or when the desk restarts). `share on` and `share off` fail with a message if the desk isn't in share mode.
Pass on the URLs from `status` (IP URLs, plus a `.local` one only if the desk was started with `--share-mdns`); the human can also hover the page's Share button to see them.
Anyone on the network can then read the packets and record decisions, so mention that.

## What the human sees of your changes

The desk records changes made outside the page and shows the newest on each card ("Last changed ..."), with the full list
in a tooltip: when you edit a draft file or the item's entry in `review.toml`, and when you write a decision or note
through the API (your revise-clear is shown as "Decision set by an agent" and "Note changed by an agent", without the
note text). This is automatic, so there is nothing for you to call. It does mean the human can see exactly when you
revised something, so edit the draft once, deliberately, rather than saving it repeatedly. The page marks its own writes
with `X-Review-Page: 1`; never send that header, so your writes stay labelled as yours.

## Packet format

See the review-desk README, "Packet format", for every manifest field (`title`, `intro`, `links`, `appendix`, and per
item `id`, `title`, `kind`, `recommendation`, `file`, `links`, `facts`). `review-desk new` writes a commented template.

Don't rename an item's `id` once review has started: its decision becomes an orphan and the item shows as pending.

## Don't

- Don't edit or create `review-decisions.json`, `review-history.json` or `review-changes.json`. Decisions are the human's.
- Don't PUT to the desk's API (`/api/p/.../items/...`) on the human's behalf, except `{"decision": "", "note": "", "ifDecision": "revise", "ifNote": "..."}` to clear a revise
  mark and its note after you have revised that item (above). It exists for the page.
- Don't treat a pending, dropped or revise-marked item as approved.
- Don't open a share window on your own initiative, and don't leave one open after the human is done.
- Don't move or rename a registered packet directory; run `review-desk remove <dir>` and `add` it again if you must.
