# review-desk

A local, loopback-only approval desk: agents put drafts in a review packet (`review.toml`), a human decides each item in
the browser, and decisions land in `review-decisions.json` beside the manifest. README.md covers usage, the packet format
and the agent workflow; `skills/review-desk/SKILL.md` is the agent-facing version. This file is for working on the tool
itself.

## Working rules

- Keep it small and local: stdlib plus `markdown-it-py` (with linkify). No network beyond 127.0.0.1, and no coupling to any hosted service.
- Don't change the decisions file shape (`{item_id: {decision, note, updatedAt}}`): agents and existing packets rely on it.
- Don't change `slugify`: packet ids (and so their URLs) derive from it.
- Every security rule in `server.py`'s docstring has a test. A new rule needs a test, and a mutant in `bin/mutation-check`.
- Test first, then check the tests can fail: `bin/mutation-check` (green baseline required; it restores every file).
- Tests never launch a real browser or desk: stub `cli.subprocess.run`, `cli.webbrowser` and `cli.ensure_running`.
- The default desk port is 8791. Run test desks on another port (`--port`), with a scratch `REVIEW_DESK_STATE`.
- When testing against a real packet, use a copy; never PUT into a live packet.

## Running things

- Tests: `uv run --offline pytest -q` (about 1 s). Mutation check: `bin/mutation-check` (about 15 s).
- CLI: `bin/review-desk ...` (runs `uv run --offline` against this checkout).
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
- `cli.py`: commands, and `open_browser`. `bin/review-desk`: launcher. `bin/mutation-check`: the can-it-fail check.
- `skills/review-desk/`: the Claude Code skill for agents using the desk.

## Gotchas

- `os.replace` is patched in tests through `fsutil.os`; keep the write in `fsutil`.
- Route segments are matched undecoded against `[a-z0-9-]+`. Don't add `unquote` to routing.
- `cli.PLATFORM` (not `sys.platform`) picks the browser strategy, so tests can set it.
- Some browsers hang in headless `--dump-dom`/`--screenshot` mode. For a UI check, Playwright's Chromium is reliable.
