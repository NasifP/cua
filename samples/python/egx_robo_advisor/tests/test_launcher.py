"""One command for three processes, without weakening what the split was for."""

from __future__ import annotations

from egx_advisor import launcher as launcher_module
from egx_advisor.launcher import Launcher, ProcessSpec, build_specs


class FakeProcess:
    def __init__(self, log: list, name: str, exit_code=None) -> None:
        self.log, self.name, self.code = log, name, exit_code

    def poll(self):
        return self.code

    def terminate(self):
        self.log.append(("terminate", self.name))
        self.code = 0

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.log.append(("kill", self.name))


def make(tmp_path, monkeypatch, probe=lambda port, path: "free"):
    monkeypatch.setattr(launcher_module, "LOG_DIR", tmp_path / "logs")
    events: list = []

    def popen(argv, **kwargs):
        name = next(s.name for s in specs if s.argv == argv)
        events.append(("start", name))
        return FakeProcess(events, name)

    specs = build_specs({}, python="py")
    runner = Launcher(
        specs=specs, env={}, popen=popen, probe=probe,
        halt=lambda reason: events.append(("halt", reason)), say=lambda _: None,
    )
    return runner, events


def test_all_three_start_in_order(tmp_path, monkeypatch) -> None:
    runner, events = make(tmp_path, monkeypatch)
    runner.start_all()
    assert [e for e in events if e[0] == "start"] == [
        ("start", "computer-server"), ("start", "dashboard"), ("start", "agent"),
    ]


def test_a_server_that_is_already_running_is_reused_not_doubled(tmp_path, monkeypatch) -> None:
    """A second copy used to fail with 'only one usage of each socket address'."""
    runner, events = make(
        tmp_path, monkeypatch,
        probe=lambda port, path: "ours" if port == 8000 else "free",
    )
    runner.start_all()
    started = [name for kind, name in events if kind == "start"]
    assert "computer-server" not in started
    assert runner.reused == ["computer-server"]


def test_stopping_halts_the_bot_before_anything_is_terminated(tmp_path, monkeypatch) -> None:
    runner, events = make(tmp_path, monkeypatch)
    runner.start_all()
    runner.stop_all()
    stop_events = [e for e in events if e[0] in ("halt", "terminate")]
    assert stop_events[0][0] == "halt", "the bus must be halted first"
    assert stop_events[1] == ("terminate", "agent"), "then the agent, before the rest"


def test_a_crashed_child_is_restarted_a_bounded_number_of_times(tmp_path, monkeypatch) -> None:
    runner, events = make(tmp_path, monkeypatch)
    runner.start_all()
    for _ in range(launcher_module.MAX_RESTARTS + 2):
        runner.children[1].process.code = 1
        runner.supervise_once()
    starts = [name for kind, name in events if kind == "start" and name == "dashboard"]
    assert len(starts) == 1 + launcher_module.MAX_RESTARTS


def test_the_launcher_never_arms_the_bot() -> None:
    source = (launcher_module.PROJECT_ROOT / "egx_advisor" / "launcher.py").read_text(
        encoding="utf-8"
    )
    assert ".resume(" not in source
    assert "/api/control/resume" not in source


def test_the_agent_always_runs_as_a_dry_run() -> None:
    agent = next(s for s in build_specs({}, python="py") if s.name == "agent")
    assert "--dry-run" in agent.argv


def test_specs_are_plain_data() -> None:
    assert all(isinstance(s, ProcessSpec) for s in build_specs({}))
