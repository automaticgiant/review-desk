---
name: review-desk
description: Use when a batch of drafts needs a human's approval (ticket drafts, PR replies, comments, plans, messages); put them in a review packet instead of asking in chat. The human decides each item (approve, revise, drop) in a local browser page, and you read the decisions back later with `review-desk status --json`.
---

# review-desk

review-desk is a local, loopback-only approval desk. You write drafts into a **review packet** (a directory with a
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
   - `revise`: treat the note as instructions. Edit the draft file; the page re-renders it on reload. Tell the human
     which items changed so they can decide again. Don't act on a revised item until it's approved.
   - `drop`: don't do it. Leave the file.
   - pending (`null`): don't act; it's still waiting.

## Packet format

See the review-desk README, "Packet format", for every manifest field (`title`, `intro`, `links`, `appendix`, and per
item `id`, `title`, `kind`, `recommendation`, `file`, `links`, `facts`). `review-desk new` writes a commented template.

Don't rename an item's `id` once review has started: its decision becomes an orphan and the item shows as pending.

## Don't

- Don't edit or create `review-decisions.json`. Decisions are the human's.
- Don't PUT to the desk's API (`/api/p/.../items/...`) on the human's behalf. It exists for the page.
- Don't treat a pending, dropped or revise-marked item as approved.
- Don't move or rename a registered packet directory; run `review-desk remove <dir>` and `add` it again if you must.
