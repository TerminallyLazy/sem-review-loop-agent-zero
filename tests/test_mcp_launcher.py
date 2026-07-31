from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from usr.plugins.sem_review_loop.helpers import mcp_launcher
from usr.plugins.sem_review_loop.helpers.mcp_launcher import (
    CUSTOM_BINARY_ENV,
    PROJECT_ID_ENV,
    PROJECT_ROOT_ENV,
    child_environment,
    launch_sem_mcp,
)


class FakeProcess:
    pid = 4242

    def __init__(self) -> None:
        self.polls = 0
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        self.polls += 1
        return None if self.polls == 1 else 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class StubbornProcess:
    pid = 4343

    def __init__(self) -> None:
        self.alive = True
        self.terminated = False
        self.killed = False
        self.polls_after_kill = 0

    def poll(self) -> int | None:
        if self.killed:
            self.polls_after_kill += 1
            if self.polls_after_kill >= 4:
                self.alive = False
        return None if self.alive else -9

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class WindowsTreeProcess:
    pid = 4444

    def __init__(self) -> None:
        self.signals: list[int] = []
        self.killed = False

    def poll(self) -> int | None:
        return None

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)

    def terminate(self) -> None:
        raise AssertionError("direct-child terminate is not tree-safe")

    def kill(self) -> None:
        self.killed = True


def test_child_environment_excludes_poisoned_parent_values(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid")
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-copy")
    monkeypatch.setenv("HOME", "/tmp/poisoned-home")
    environment = child_environment(
        project_root=tmp_path,
        project_id="project-123",
    )
    assert "HTTPS_PROXY" not in environment
    assert "HTTP_PROXY" not in environment
    assert "ALL_PROXY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert environment["HOME"] != "/tmp/poisoned-home"
    assert environment["SEM_NO_NETWORK"] == "1"
    assert environment["SEM_NO_TELEMETRY"] == "1"
    assert Path(environment["HOME"]).is_relative_to(
        Path(environment["SEM_CACHE_DIR"]).parents[3]
    )


def test_launcher_holds_binary_lease_for_full_child_lifetime(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    active = {"lease": False}
    process = FakeProcess()

    @contextmanager
    def lease(custom: str) -> Iterator[Path]:
        assert custom == ""
        active["lease"] = True
        events.append("lease-enter")
        try:
            yield Path("/plugin/.data/bin/.sem-lease-test")
        finally:
            assert process.polls >= 2
            active["lease"] = False
            events.append("lease-exit")

    def popen(command: list[str], **kwargs: Any) -> FakeProcess:
        assert active["lease"] is True
        assert command == [
            "/plugin/.data/bin/.sem-lease-test",
            "mcp",
        ]
        environment = kwargs["env"]
        assert "HTTPS_PROXY" not in environment
        assert "SECRET_TOKEN" not in environment
        assert kwargs["cwd"] == str(tmp_path.resolve())
        events.append("child-start")
        return process

    result = launch_sem_mcp(
        environ={
            PROJECT_ROOT_ENV: str(tmp_path),
            PROJECT_ID_ENV: "project-lease",
            CUSTOM_BINARY_ENV: "",
            "HTTPS_PROXY": "http://attacker.invalid",
            "SECRET_TOKEN": "do-not-copy",
        },
        lease=lease,
        popen=popen,
    )
    assert result == 0
    assert active["lease"] is False
    assert events == ["lease-enter", "child-start", "lease-exit"]


def test_launcher_reaps_stubborn_child_before_releasing_lease(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    active = {"lease": False}
    process = StubbornProcess()

    @contextmanager
    def lease(_custom: str) -> Iterator[Path]:
        active["lease"] = True
        try:
            yield Path("/plugin/.data/bin/.sem-lease-test")
        finally:
            assert process.alive is False
            active["lease"] = False

    def popen(_command: list[str], **_kwargs: Any) -> StubbornProcess:
        assert active["lease"] is True
        return process

    def fail_wait(_process: StubbornProcess) -> int:
        raise RuntimeError("launcher wait interrupted")

    monkeypatch.setattr(mcp_launcher, "_wait_for_exit", fail_wait)
    monkeypatch.setattr(
        mcp_launcher,
        "_signal_process_tree",
        lambda child, _signal: child.terminate(),
    )
    monkeypatch.setattr(
        mcp_launcher,
        "_force_process_tree",
        lambda child: child.kill(),
    )
    monkeypatch.setattr(mcp_launcher, "TERMINATION_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(mcp_launcher, "POLL_INTERVAL_SECONDS", 0.0)

    with pytest.raises(RuntimeError, match="wait interrupted"):
        launch_sem_mcp(
            environ={
                PROJECT_ROOT_ENV: str(tmp_path),
                PROJECT_ID_ENV: "project-stubborn",
                CUSTOM_BINARY_ENV: "",
            },
            lease=lease,
            popen=popen,
        )
    assert process.terminated is True
    assert process.killed is True
    assert process.polls_after_kill >= 4
    assert active["lease"] is False


def test_windows_launcher_signals_group_and_force_kills_process_tree(
    monkeypatch: Any,
) -> None:
    process = WindowsTreeProcess()
    commands: list[list[str]] = []
    ctrl_break = 1234
    monkeypatch.setattr(mcp_launcher.os, "name", "nt")
    monkeypatch.setattr(
        mcp_launcher.signal,
        "CTRL_BREAK_EVENT",
        ctrl_break,
        raising=False,
    )

    def run(command: list[str], **kwargs: Any) -> object:
        commands.append(command)
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == mcp_launcher.TERMINATION_GRACE_SECONDS
        return object()

    monkeypatch.setattr(mcp_launcher.subprocess, "run", run)
    mcp_launcher._signal_process_tree(process, mcp_launcher.signal.SIGTERM)
    mcp_launcher._force_process_tree(process)

    assert process.signals == [ctrl_break]
    assert commands == [
        ["taskkill", "/PID", str(process.pid), "/T", "/F"]
    ]
    assert process.killed is True
