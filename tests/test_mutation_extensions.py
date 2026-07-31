from __future__ import annotations

import importlib.util
import inspect
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers.extension import Extension
from usr.plugins.sem_review_loop.helpers import services


ROOT = Path(__file__).resolve().parents[1]


class FakeCoordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def schedule_for_agent(
        self,
        agent: object,
        trigger: str,
        path_hints: list[str],
    ) -> None:
        self.calls.append(("agent", agent, trigger, path_hints))

    def schedule_for_path_hints(
        self,
        trigger: str,
        path_hints: list[str],
    ) -> None:
        self.calls.append(("paths", trigger, path_hints))

    def unregister_context(self, context_id: str) -> None:
        self.calls.append(("unregister", context_id))


class SchedulingFailureCoordinator(FakeCoordinator):
    def _raise(self) -> None:
        raise RuntimeError("schedule unavailable " + ("x" * 1000))

    def schedule_for_agent(
        self,
        agent: object,
        trigger: str,
        path_hints: list[str],
    ) -> None:
        del agent, trigger, path_hints
        self._raise()

    def schedule_for_path_hints(
        self,
        trigger: str,
        path_hints: list[str],
    ) -> None:
        del trigger, path_hints
        self._raise()


class SchedulingStopped(BaseException):
    pass


class BaseFailureCoordinator(SchedulingFailureCoordinator):
    def _raise(self) -> None:
        raise SchedulingStopped


@pytest.fixture
def coordinator(monkeypatch: pytest.MonkeyPatch) -> FakeCoordinator:
    fake = FakeCoordinator()
    monkeypatch.setattr(services, "get_coordinator", lambda: fake)
    return fake


@pytest.fixture
def fake_agent() -> SimpleNamespace:
    tool = SimpleNamespace(args={"runtime": "terminal"})
    return SimpleNamespace(loop_data=SimpleNamespace(current_tool=tool))


def load_extension(
    point: str,
    agent: object | None,
) -> tuple[Extension, type[Extension]]:
    return load_extension_path(
        Path(point) / "_50_sem_review_refresh.py",
        agent,
    )


def load_extension_path(
    relative_path: Path,
    agent: object | None,
) -> tuple[Extension, type[Extension]]:
    path = ROOT / "extensions/python" / relative_path
    assert path.is_file()
    spec = importlib.util.spec_from_file_location(
        "test_sem_review_" + "_".join(relative_path.parts),
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    classes = [
        value
        for _name, value in inspect.getmembers(module, inspect.isclass)
        if issubclass(value, Extension) and value is not Extension
    ]
    assert len(classes) == 1
    return classes[0](agent=agent), classes[0]


@pytest.mark.parametrize(
    ("point", "parameter_names"),
    [
        ("text_editor_write_after", ("self", "data", "kwargs")),
        ("text_editor_patch_after", ("self", "data", "kwargs")),
        ("workdir_file_mutation_after", ("self", "data", "kwargs")),
        (
            "tool_execute_after",
            ("self", "tool_name", "response", "kwargs"),
        ),
    ],
)
def test_extension_signatures_match_upstream_hook_contract(
    point: str,
    parameter_names: tuple[str, ...],
) -> None:
    _extension, extension_type = load_extension(point, None)
    signature = inspect.signature(extension_type.execute)
    assert tuple(signature.parameters) == parameter_names


@pytest.mark.asyncio
async def test_text_write_schedules_changed_path(
    coordinator: FakeCoordinator,
    fake_agent: SimpleNamespace,
) -> None:
    extension, _type = load_extension(
        "text_editor_write_after",
        fake_agent,
    )
    await extension.execute(
        data={"path": "/project/src/app.py", "total_lines": 4}
    )
    assert coordinator.calls == [
        (
            "agent",
            fake_agent,
            "text_editor_write",
            ["/project/src/app.py"],
        )
    ]


@pytest.mark.asyncio
async def test_text_patch_schedules_changed_path(
    coordinator: FakeCoordinator,
    fake_agent: SimpleNamespace,
) -> None:
    extension, _type = load_extension(
        "text_editor_patch_after",
        fake_agent,
    )
    await extension.execute(data={"path": "/project/src/app.py"})
    assert coordinator.calls == [
        (
            "agent",
            fake_agent,
            "text_editor_patch",
            ["/project/src/app.py"],
        )
    ]


@pytest.mark.asyncio
async def test_text_event_without_agent_is_ignored(
    coordinator: FakeCoordinator,
) -> None:
    extension, _type = load_extension("text_editor_write_after", None)
    await extension.execute(data={"path": "/project/src/app.py"})
    assert coordinator.calls == []


@pytest.mark.asyncio
async def test_workdir_mutation_without_agent_matches_all_scopes(
    coordinator: FakeCoordinator,
) -> None:
    extension, _type = load_extension(
        "workdir_file_mutation_after",
        None,
    )
    await extension.execute(
        data={"action": "upload", "paths": ["/project/src/app.py"]}
    )
    assert coordinator.calls == [
        ("paths", "file_browser_upload", ["/project/src/app.py"])
    ]


@pytest.mark.asyncio
async def test_non_code_execution_tool_is_ignored(
    coordinator: FakeCoordinator,
    fake_agent: SimpleNamespace,
) -> None:
    extension, _type = load_extension(
        "tool_execute_after",
        fake_agent,
    )
    await extension.execute(tool_name="response", response=object())
    assert coordinator.calls == []


@pytest.mark.asyncio
async def test_code_execution_output_poll_is_ignored(
    coordinator: FakeCoordinator,
    fake_agent: SimpleNamespace,
) -> None:
    fake_agent.loop_data.current_tool.args = {"runtime": "output"}
    extension, _type = load_extension(
        "tool_execute_after",
        fake_agent,
    )
    await extension.execute(
        tool_name="code_execution_tool",
        response=object(),
    )
    assert coordinator.calls == []


@pytest.mark.asyncio
async def test_terminal_code_execution_schedules_refresh(
    coordinator: FakeCoordinator,
    fake_agent: SimpleNamespace,
) -> None:
    extension, _type = load_extension(
        "tool_execute_after",
        fake_agent,
    )
    await extension.execute(
        tool_name="code_execution_tool",
        response=object(),
    )
    assert coordinator.calls == [
        ("agent", fake_agent, "code_execution", [])
    ]


def failure_call(
    point: str,
    fake_agent: SimpleNamespace,
) -> tuple[Extension, dict[str, object]]:
    extension, _type = load_extension(point, fake_agent)
    if point == "tool_execute_after":
        return extension, {
            "tool_name": "code_execution_tool",
            "response": object(),
        }
    if point == "workdir_file_mutation_after":
        return extension, {
            "data": {
                "action": "upload",
                "paths": ["/project/src/app.py"],
            }
        }
    return extension, {"data": {"path": "/project/src/app.py"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "point",
    [
        "text_editor_write_after",
        "text_editor_patch_after",
        "workdir_file_mutation_after",
        "tool_execute_after",
    ],
)
async def test_post_mutation_scheduling_failure_is_bounded_and_nonfatal(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fake_agent: SimpleNamespace,
    point: str,
) -> None:
    monkeypatch.setattr(
        services,
        "get_coordinator",
        lambda: SchedulingFailureCoordinator(),
    )
    extension, call = failure_call(point, fake_agent)

    with caplog.at_level(logging.WARNING):
        await extension.execute(**call)

    assert "scheduling failed" in caplog.text.lower()
    assert len(caplog.records[-1].getMessage()) < 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "point",
    [
        "text_editor_write_after",
        "text_editor_patch_after",
        "workdir_file_mutation_after",
        "tool_execute_after",
    ],
)
async def test_post_mutation_hooks_preserve_base_exception(
    monkeypatch: pytest.MonkeyPatch,
    fake_agent: SimpleNamespace,
    point: str,
) -> None:
    monkeypatch.setattr(
        services,
        "get_coordinator",
        lambda: BaseFailureCoordinator(),
    )
    extension, call = failure_call(point, fake_agent)

    with pytest.raises(SchedulingStopped):
        await extension.execute(**call)


def test_agent_context_remove_cleanup_unregisters_context(
    coordinator: FakeCoordinator,
) -> None:
    extension, extension_type = load_extension_path(
        Path(
            "_functions/agent/AgentContext/remove/end/"
            "_50_sem_review_cleanup.py"
        ),
        None,
    )
    assert inspect.iscoroutinefunction(extension_type.execute) is False

    extension.execute(
        data={
            "args": ("ctx-removed",),
            "result": SimpleNamespace(id="ctx-removed"),
        }
    )

    assert coordinator.calls == [("unregister", "ctx-removed")]
