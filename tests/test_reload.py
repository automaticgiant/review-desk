import pytest

from review_desk import cli
from review_desk.reload import snapshot, supervise


class FakeChild:
    def __init__(self, log, n):
        self.log, self.n, self.alive, self.killed = log, n, True, False

    def poll(self):
        return None if self.alive else 3

    def terminate(self):
        self.log.append(f"terminate {self.n}")
        self.alive = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.log.append(f"wait {self.n}")


def run(snapshots, crash_after=None):
    """Drive supervise() with scripted snapshots (one per read; the last repeats) until a Ctrl-C after 40 ticks.
    crash_after=N makes the running desk die at tick N. Returns (event log, children spawned)."""
    log, kids, last = [], [], [None]
    reads = iter(snapshots)

    def spawn():
        kids.append(FakeChild(log, len(kids) + 1))
        log.append(f"spawn {len(kids)}")
        return kids[-1]

    def snap():
        last[0] = next(reads, last[0])
        return last[0]

    def sleep(_s):
        ticks = log.count("tick")
        if ticks >= 40:
            raise KeyboardInterrupt
        if crash_after is not None and ticks == crash_after:
            kids[-1].alive = False
        log.append("tick")

    supervise(spawn, snap, sleep=sleep, log=lambda m: log.append("log: " + m))
    return log, kids


def test_a_change_restarts_the_desk_after_stopping_the_old_one():
    log, kids = run([{"a": 1}, {"a": 1}, {"a": 2}, {"a": 2}, {"a": 2}])
    assert log[0] == "spawn 1"
    i = log.index("terminate 1")
    assert log.index("wait 1") > i and log.index("spawn 2") > log.index("wait 1")  # one at a time: the port is free
    assert any("restarting" in l for l in log) and len(kids) == 2


def test_no_change_no_restart():
    log, kids = run([{"a": 1}] * 6)
    assert len(kids) == 1 and not any("restarting" in l for l in log)


def test_a_multi_file_save_restarts_once():
    """The settle pause re-reads the snapshot, so a save that touches several files still restarts exactly once."""
    log, kids = run([{"a": 1}, {"a": 1}, {"a": 2}, {"a": 2, "b": 2}, {"a": 2, "b": 2}])
    assert len(kids) == 2


def test_a_crashed_desk_waits_for_the_next_change_instead_of_respawning():
    log, kids = run([{"a": 1}] * 8, crash_after=2)
    assert len(kids) == 1 and sum("waiting for a change" in l for l in log) == 1  # said once, no restart loop
    log, kids = run([{"a": 1}] * 3 + [{"a": 2}] * 6, crash_after=2)
    assert len(kids) == 2  # a fix is picked up


def test_ctrl_c_stops_the_desk():
    log, kids = run([{"a": 1}] * 3)
    assert kids[-1].alive is False and f"terminate {len(kids)}" in log


def test_a_child_that_ignores_terminate_is_killed():
    import subprocess

    class Stubborn(FakeChild):
        def wait(self, timeout=None):
            if not self.killed:
                raise subprocess.TimeoutExpired("x", timeout)

    log = []
    kid = Stubborn(log, 1)
    supervise(lambda: kid, lambda: {}, sleep=lambda s: (_ for _ in ()).throw(KeyboardInterrupt), log=lambda m: None)
    assert kid.killed


def test_snapshot_sees_python_edits_only(tmp_path):
    (tmp_path / "a.py").write_text("1")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("1")
    (tmp_path / "page.js").write_text("1")  # static files hot-swap in the page; they must not restart the desk
    s0 = snapshot(tmp_path)
    assert set(map(str, s0)) == {str(tmp_path / "a.py"), str(tmp_path / "sub" / "b.py")}
    (tmp_path / "page.js").write_text("22")
    assert snapshot(tmp_path) == s0
    (tmp_path / "sub" / "b.py").write_text("22222")
    assert snapshot(tmp_path) != s0


def test_snapshot_tolerates_a_file_vanishing_mid_scan(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("1")
    (tmp_path / "b.py").write_text("1")
    from pathlib import Path
    real = Path.stat
    def stat(self, *a, **k):
        if self.name == "b.py":
            raise FileNotFoundError(self)  # an editor's atomic save replaced it between the glob and the stat
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "stat", stat)
    assert set(map(lambda p: p.name, map(Path, snapshot(tmp_path)))) == {"a.py"}


def test_serve_reload_runs_the_supervisor_with_a_plain_child(monkeypatch, state_dir):
    from review_desk import reload as rl
    seen = {}
    monkeypatch.setattr(rl, "supervise", lambda spawn, snap, **k: seen.update(spawn=spawn, snap=snap) or 0)
    monkeypatch.setattr(cli, "probe", lambda port: "none")
    popen = []
    monkeypatch.setattr(rl.subprocess, "Popen", lambda cmd, **k: popen.append(cmd) or object())
    assert cli.main(["serve", "--reload", "--port", "1234"]) == 0
    seen["spawn"]()
    assert popen[0][-3:] == ["serve", "--port", "1234"] and "--reload" not in popen[0]  # the child is a plain desk


def test_serve_reload_refuses_a_busy_port(monkeypatch, state_dir, capsys):
    monkeypatch.setattr(cli, "probe", lambda port: "desk")
    assert cli.main(["serve", "--reload", "--port", "1234"]) != 0
    assert "already in use" in capsys.readouterr().err


def test_background_reload_and_the_env_switch_start_the_detached_desk_with_reload(monkeypatch, state_dir):
    calls = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(cli, "probe", lambda port, _s=iter(["none", "desk"]): next(_s))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    cli.ensure_running(1234, reload=True)
    assert "--reload" in calls[0]
    monkeypatch.setattr(cli, "probe", lambda port, _s=iter(["none", "desk"]): next(_s))
    monkeypatch.setenv("REVIEW_DESK_RELOAD", "1")
    cli.ensure_running(1234)
    assert "--reload" in calls[1]
    monkeypatch.setattr(cli, "probe", lambda port, _s=iter(["none", "desk"]): next(_s))
    monkeypatch.delenv("REVIEW_DESK_RELOAD")
    cli.ensure_running(1234)
    assert "--reload" not in calls[2]


def test_serve_background_passes_reload_to_the_launcher(monkeypatch, state_dir, capsys):
    seen = []
    monkeypatch.setattr(cli, "ensure_running", lambda port, reload=False, share=False, mdns=False: seen.append(reload))
    assert cli.main(["serve", "--background", "--reload", "--port", "1234"]) == 0
    assert cli.main(["serve", "--background", "--port", "1234"]) == 0
    assert seen == [True, False]


def test_supervisor_messages_are_flushed_so_a_background_log_shows_them(monkeypatch):
    from review_desk import reload as rl
    flushes = []
    class Out:
        def write(self, s): pass
        def flush(self): flushes.append(1)
    monkeypatch.setattr(rl.sys, "stdout", Out())
    rl._say("source changed")
    assert flushes


def test_sigterm_stops_the_desk_like_ctrl_c(monkeypatch):
    """Killing the supervisor must not orphan the desk it runs: SIGTERM becomes the same exit path as Ctrl-C."""
    import signal
    from review_desk import reload as rl
    handlers = {}
    monkeypatch.setattr(rl.signal, "signal", lambda sig, h: handlers.update({sig: h}))
    rl.stop_on_sigterm()
    with pytest.raises(KeyboardInterrupt):
        handlers[signal.SIGTERM](signal.SIGTERM, None)


def test_serve_reload_installs_the_sigterm_handler(monkeypatch, state_dir):
    from review_desk import reload as rl
    calls = []
    monkeypatch.setattr(rl, "stop_on_sigterm", lambda: calls.append(1))
    monkeypatch.setattr(rl, "supervise", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "probe", lambda port: "none")
    cli.main(["serve", "--reload", "--port", "1234"])
    assert calls == [1]


def test_reload_on_an_already_running_desk_says_it_did_nothing(monkeypatch, state_dir, capsys):
    monkeypatch.setattr(cli, "probe", lambda port: "desk")
    cli.ensure_running(1234, reload=True)
    assert "already running" in capsys.readouterr().err
    cli.ensure_running(1234)  # a plain start of a running desk stays silent
    assert capsys.readouterr().err == ""


def test_mutation_check_runs_in_a_copy_without_the_venv():
    """bin/mutation-check must never mutate the checkout a reloading desk may be watching."""
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "bin" / "mutation-check").read_text()
    assert "shutil.copytree(SRC, R" in text and '".venv"' in text and "R = _tmp" in text
    assert "R = pathlib.Path(__file__)" not in text


def test_mutation_check_can_show_progress_in_a_bar_and_filter_by_name():
    """A long run has to be watchable in a 10-line shell view: --bar replaces the per-mutant lines with one progress line."""
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "bin" / "mutation-check").read_text()
    assert '"--bar" in sys.argv' in text and '"--only" in sys.argv' in text
    assert "elapsed, ~" in text and "survivors {len(survivors)}" in text and 'print(f"SURVIVED  {name}", flush=True)' in text
