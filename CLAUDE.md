# review-desk

A local, loopback-only approval desk: agents put drafts in a review packet (`review.toml`), a human decides each item in
the browser, and decisions land in `review-decisions.json` beside the manifest. README.md covers usage, the packet format
and the agent workflow; `skills/review-desk/SKILL.md` is the agent-facing version. This file is for working on the tool
itself.

## Working rules

- Keep it small and local: stdlib plus `markdown-it-py` (with linkify). No network beyond 127.0.0.1, and no coupling to any hosted service.
- Don't change the decisions file shape (`{item_id: {decision, note, updatedAt}}`): agents and existing packets rely on it.
- Don't change `slugify`: packet ids (and so their URLs) derive from it.
- Every security rule in `server.py`'s docstring has a test (`tests/test_server.py`, `tests/test_share.py`, `tests/test_changes.py`). A new rule needs a test, and a mutant in `bin/mutation-check`.
- Test first, then check the tests can fail: `bin/mutation-check` (green baseline required; it restores every file).
- Tests never launch a real browser or desk: stub `cli.subprocess.run`, `cli.webbrowser` and `cli.ensure_running`.
- The default desk port is 8791. Run test desks on another port (`--port`), with a scratch `REVIEW_DESK_STATE`.
- When testing against a real packet, use a copy; never PUT into a live packet.

## Running things

- Tests: `uv run --offline pytest -q` (about 1 s). Mutation check: `bin/mutation-check` (a few minutes now; `--bar` for one progress line per mutant with an estimate, `--only TEXT` for one area).
- CLI: `bin/review-desk ...`: a PEP 723 script (`uv run --offline --script`, started through a portable sh/Python polyglot shebang); keep its dependency block in step with `pyproject.toml` (a test checks). It exports `PYTHONPATH=src` so desks it starts can import the package, and they run with `-P` so the current directory is never imported. Its first two lines are an sh/Python polyglot (`env -S` isn't portable); the first run needs network to build its environment (`uv run --script bin/review-desk --help`).
- Live page edits: every desk hot-swaps `static/page.css` and `static/page.js` into open pages within a few seconds, with no reload (notes, open drafts and scroll survive), so there is no flag and no restart for page changes. Python changes: run the desk with `serve --reload` (or `REVIEW_DESK_RELOAD=1`) and it restarts itself on any `.py` change in the package; otherwise restart by hand. The supervisor itself isn't reloaded (edits to `cli.py`/`reload.py` restart only the desk). A `page.js` that throws after loading needs a manual page reload. `bin/mutation-check` runs in a temp copy, so it is safe next to a reloading desk.
- A running desk: `lsof -nP -iTCP:8791 -sTCP:LISTEN`; log `~/.local/state/review-desk/desk-8791.log`. Restart it after
  changing server code: the page and manifest are live, Python code isn't.
- `bin/export-public <dest> "<Name>" <email>`: export the current tree as a fresh, single-commit repository.

## Layout

- `src/review_desk/manifest.py`: parse and validate `review.toml` (ids, path containment, link schemes), and `slugify`.
- `registry.py`: `packets.json` under `$REVIEW_DESK_STATE` (default `~/.local/state/review-desk`); packet ids are
  `<last two path parts>-<sha1[:6]>`.
- `decisions.py`: read, validate and merge one item under a lock; `fsutil.atomic_write_json` does the write.
- `summary.py`: the `status`/`list`/index summaries. `render.py`: HTML; drafts are GitHub-style markdown (markdown-it-py, CommonMark plus tables, strikethrough, linkify), raw HTML off, URL allowlist on every href and src.
- `server.py`: routes and security checks. `static/page.{css,js}`: the page (served as files so the CSP can forbid inline script).
- `cli.py`: commands, and `open_browser`. `reload.py`: the `serve --reload` supervisor. `bin/review-desk`: launcher (PEP 723 script). `bin/mutation-check`: the can-it-fail check.
- `changes.py`: external changes per item (`review-changes.json`): the `Watcher` (started by `serve`; open packets scanned ~1 s, others every 10 min, coarse flag after a gap; the changes route calls `touch`), and `record_write`, called by the PUT handler under `changes.LOCK`. `X-Review-Page: 1` marks the page's own writes.
- `share.py`: share mode (`serve --share`): the in-memory 4-hour window, peer checks, and the interface URLs (and the mDNS name, only with `--share-mdns`) the Share button's tooltip lists. `cli.cmd_share` drives `POST /api/share` for agents.
- `.githooks/`: Linux + systemd user unit only. `post-merge`/`post-rewrite` symlink to `restart-systemd-user-unit`, which restarts `review-desk.service` after a pull (needs `git config core.hooksPath .githooks`); a no-op on macOS.
- `skills/review-desk/`: the Claude Code skill for agents using the desk.

## Gotchas

- `os.replace` is patched in tests through `fsutil.os`; keep the write in `fsutil`.
- Route segments are matched undecoded against `[a-z0-9-]+`. Don't add `unquote` to routing.
- `cli.PLATFORM` (not `sys.platform`) picks the browser strategy, so tests can set it.
- Some browsers hang in headless `--dump-dom`/`--screenshot` mode. For a UI check, Playwright's Chromium is reliable.
