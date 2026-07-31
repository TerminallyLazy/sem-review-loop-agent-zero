from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from usr.plugins.sem_review_loop.helpers.config import parse_config
from usr.plugins.sem_review_loop.helpers.coordinator import (
    RefreshCoordinator,
    RefreshTimeoutError,
)
from usr.plugins.sem_review_loop.helpers.project_scope import ProjectScope
from usr.plugins.sem_review_loop.helpers.registry import (
    ReviewCheckpoint,
    ReviewRegistry,
)
from usr.plugins.sem_review_loop.helpers.sem_types import (
    DiffRequest,
    DiffSnapshot,
    DiffSummary,
)


SCOPE = ProjectScope(
    "ctx",
    "project",
    "project-id",
    Path("/project"),
    Path("/project/src"),
    "src",
)


def empty_snapshot(
    fingerprint: str,
    *,
    request: DiffRequest = DiffRequest("working"),
) -> DiffSnapshot:
    return DiffSnapshot(
        request=request,
        fingerprint=fingerprint,
        revision=0,
        summary=DiffSummary(0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        changes=(),
        details=MappingProxyType({}),
        sem_version="0.21.0",
        completed_at="2026-07-29T00:00:00+00:00",
    )


class FakeRunner:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def diff(
        self,
        scope: ProjectScope,
        request: DiffRequest,
        fingerprint: str,
    ) -> DiffSnapshot:
        del scope
        self.calls.append(fingerprint)
        if self.error:
            raise self.error
        return empty_snapshot(fingerprint, request=request)

    def manual_diff(
        self,
        scope: ProjectScope,
        files: object,
        fingerprint: str,
    ) -> DiffSnapshot:
        del scope, files
        self.calls.append(fingerprint)
        return empty_snapshot(
            fingerprint,
            request=DiffRequest("stdin"),
        )


class BlockingConcurrencyRunner:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self.first_entered = threading.Event()
        self.two_entered = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()
        self._guard = threading.Lock()

    def _execute(
        self,
        fingerprint: str,
        request: DiffRequest,
    ) -> DiffSnapshot:
        with self._guard:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.first_entered.set()
            if self.active >= 2:
                self.two_entered.set()
        try:
            assert self.release.wait(2), "test runner release timed out"
            return empty_snapshot(fingerprint, request=request)
        finally:
            with self._guard:
                self.active -= 1
                if self.active == 0:
                    self.exited.set()

    def diff(
        self,
        scope: ProjectScope,
        request: DiffRequest,
        fingerprint: str,
    ) -> DiffSnapshot:
        del scope
        return self._execute(fingerprint, request)

    def manual_diff(
        self,
        scope: ProjectScope,
        files: object,
        fingerprint: str,
    ) -> DiffSnapshot:
        del scope, files
        return self._execute(fingerprint, DiffRequest("stdin"))


class FakeTask:
    instances: list["FakeTask"] = []

    def __init__(self) -> None:
        self.killed = False
        self.awaited = False
        self.call: tuple[Any, tuple[Any, ...]] | None = None
        self.__class__.instances.append(self)

    def start_task(self, func: Any, *args: Any) -> "FakeTask":
        self.call = (func, args)
        return self

    def kill(self, terminate_thread: bool = False) -> None:
        del terminate_thread
        self.killed = True

    async def result(self, timeout: float | None = None) -> object:
        del timeout
        self.awaited = True
        if self.killed:
            raise concurrent.futures.CancelledError()
        assert self.call is not None
        func, args = self.call
        return await func(*args)

    async def run(self) -> object:
        assert self.call is not None
        func, args = self.call
        return await func(*args)


class TimeoutTask(FakeTask):
    async def result(self, timeout: float | None = None) -> object:
        del timeout
        self.awaited = True
        raise TimeoutError("pending")


class BaseFailureTask(FakeTask):
    def start_task(self, func: Any, *args: Any) -> "FakeTask":
        del func, args
        raise KeyboardInterrupt("debounce startup interrupted")


class QueuedGenerationRunner:
    def __init__(self, blocking_project_id: str) -> None:
        self.blocking_project_id = blocking_project_id
        self.blocking_entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[str] = []
        self._guard = threading.Lock()

    def diff(
        self,
        scope: ProjectScope,
        request: DiffRequest,
        fingerprint: str,
    ) -> DiffSnapshot:
        with self._guard:
            self.calls.append(scope.project_id)
        if scope.project_id == self.blocking_project_id:
            self.blocking_entered.set()
            assert self.release.wait(2), "test runner release timed out"
        return empty_snapshot(fingerprint, request=request)


async def wait_until(
    predicate: Any,
    *,
    timeout: float = 1.0,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.001)


def coordinator_for_test(
    *,
    fingerprints: list[str] | None = None,
    runner: FakeRunner | None = None,
    events: list[dict[str, object]] | None = None,
    emit_error: Exception | None = None,
) -> tuple[ReviewRegistry, RefreshCoordinator, FakeRunner]:
    values = iter(fingerprints or ["same"] * 30)
    registry = ReviewRegistry()
    selected_runner = runner or FakeRunner()

    async def emit(
        scope: ProjectScope,
        revision: int,
    ) -> None:
        if emit_error is not None:
            raise emit_error
        if events is not None:
            events.append(
                {
                    "context_id": scope.context_id,
                    "project_id": scope.project_id,
                    "revision": revision,
                }
            )

    async def no_sleep(_seconds: float) -> None:
        return None

    coordinator = RefreshCoordinator(
        registry=registry,
        runner_for=lambda _scope, _config: selected_runner,
        fingerprint=lambda *_args: next(values),
        emit=emit,
        sleep=no_sleep,
        task_factory=FakeTask,
    )
    coordinator.register_scope(SCOPE, parse_config({}))
    return registry, coordinator, selected_runner


@pytest.mark.asyncio
async def test_changed_fingerprint_during_sem_run_is_never_published() -> None:
    registry, coordinator, _runner = coordinator_for_test(
        fingerprints=["before", "after", "after", "after"],
    )
    result = await coordinator.refresh_now(
        SCOPE,
        DiffRequest("working"),
    )
    assert result.fingerprint == "after"
    current = registry.current_working(SCOPE)
    assert current is not None and current.fingerprint == "after"


def test_new_schedule_cancels_old_debounce_generation() -> None:
    FakeTask.instances.clear()
    registry, coordinator, _runner = coordinator_for_test()
    first = coordinator.schedule(
        SCOPE,
        trigger="write",
        path_hints=["src/a.py"],
    )
    second = coordinator.schedule(
        SCOPE,
        trigger="patch",
        path_hints=["src/a.py"],
    )
    assert second > first
    assert FakeTask.instances[-2].killed is True
    assert registry.pending_generation(SCOPE) == second


@pytest.mark.asyncio
async def test_manual_working_refresh_replaces_matching_debounce() -> None:
    FakeTask.instances.clear()
    registry, coordinator, _runner = coordinator_for_test()
    coordinator.schedule(
        SCOPE,
        trigger="write",
        path_hints=["src/a.py"],
    )
    debounce = FakeTask.instances[-1]
    result = await coordinator.refresh_now(
        SCOPE,
        DiffRequest("working"),
    )
    assert debounce.killed is True
    assert result.fingerprint == "same"
    assert registry.pending_generation(SCOPE) == 0


@pytest.mark.asyncio
async def test_revision_event_contains_exact_metadata_only_contract() -> None:
    events: list[dict[str, object]] = []
    _registry, coordinator, _runner = coordinator_for_test(events=events)
    await coordinator.refresh_now(SCOPE, DiffRequest("working"))
    await coordinator.drain_notifications(SCOPE)
    assert events == [
        {
            "context_id": "ctx",
            "project_id": SCOPE.project_id,
            "revision": 1,
        }
    ]
    assert set(events[0]) == {"context_id", "project_id", "revision"}


@pytest.mark.asyncio
async def test_emit_failure_does_not_corrupt_good_snapshot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry, coordinator, _runner = coordinator_for_test(
        emit_error=RuntimeError("socket unavailable"),
    )
    with caplog.at_level(logging.WARNING):
        result = await coordinator.refresh_now(
            SCOPE,
            DiffRequest("working"),
        )
        await coordinator.drain_notifications(SCOPE)
    current = registry.current_working(SCOPE)
    assert result.fingerprint == "same"
    assert current is not None
    assert current.stale is False
    assert current.error == ""
    assert registry.public_status(SCOPE)["error"] == ""
    assert "revision event failed" in caplog.text.lower()


@pytest.mark.asyncio
async def test_hung_revision_emitter_never_blocks_publication_or_settlement(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = ReviewRegistry()
    runner = FakeRunner()
    emitter_started = asyncio.Event()
    never_finish = asyncio.Event()

    async def hung_emit(
        _scope: ProjectScope,
        _revision: int,
    ) -> None:
        emitter_started.set()
        await never_finish.wait()

    coordinator = RefreshCoordinator(
        registry=registry,
        runner_for=lambda _scope, _config: runner,
        fingerprint=lambda *_args: "hung-emitter",
        emit=hung_emit,
        emit_timeout_seconds=0.01,
    )
    coordinator.register_scope(SCOPE, parse_config({}))

    with caplog.at_level(logging.WARNING):
        result = await coordinator.refresh_now(
            SCOPE,
            DiffRequest("working"),
        )
        assert result.fingerprint == "hung-emitter"
        assert registry.pending_generation(SCOPE) == 0
        assert registry.generation_is_settled(SCOPE)
        await coordinator.drain_notifications(SCOPE)

    assert emitter_started.is_set()
    assert "revision event timed out" in caplog.text.lower()
    assert coordinator._emit_tasks == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_action", ["unregister", "cancel_all"])
async def test_cleanup_cancels_and_forgets_tracked_revision_emitters(
    cleanup_action: str,
) -> None:
    registry = ReviewRegistry()
    runner = FakeRunner()
    emitter_started = asyncio.Event()
    emitter_cancelled = asyncio.Event()
    never_finish = asyncio.Event()

    async def hung_emit(
        _scope: ProjectScope,
        _revision: int,
    ) -> None:
        emitter_started.set()
        try:
            await never_finish.wait()
        finally:
            emitter_cancelled.set()

    coordinator = RefreshCoordinator(
        registry=registry,
        runner_for=lambda _scope, _config: runner,
        fingerprint=lambda *_args: "cleanup-emitter",
        emit=hung_emit,
        emit_timeout_seconds=1,
    )
    coordinator.register_scope(SCOPE, parse_config({}))
    await coordinator.refresh_now(SCOPE, DiffRequest("working"))
    await asyncio.wait_for(emitter_started.wait(), timeout=0.5)

    if cleanup_action == "unregister":
        coordinator.unregister_context(SCOPE.context_id)
    else:
        coordinator.cancel_all()

    await asyncio.wait_for(emitter_cancelled.wait(), timeout=0.5)
    await wait_until(lambda: not coordinator._emit_tasks)


def test_path_hints_match_registered_scope_but_ignore_a0proj() -> None:
    registry, coordinator, _runner = coordinator_for_test()
    coordinator.schedule_for_path_hints(
        "upload",
        ["/src/app.py"],
    )
    matched_generation = registry.pending_generation(SCOPE)
    assert matched_generation > 0
    coordinator.schedule_for_path_hints(
        "metadata",
        ["/.a0proj/mcp_servers.json"],
    )
    assert registry.pending_generation(SCOPE) == matched_generation


@pytest.mark.asyncio
async def test_unchanged_debounced_fingerprint_skips_sem() -> None:
    registry, coordinator, runner = coordinator_for_test()
    generation = registry.reserve_generation(SCOPE, lane="working")
    assert registry.publish_working(
        SCOPE,
        empty_snapshot("same"),
        generation,
    )
    coordinator.schedule(
        SCOPE,
        trigger="code_execution",
        path_hints=[],
    )
    task = FakeTask.instances[-1]
    result = await task.run()
    assert isinstance(result, DiffSnapshot)
    assert result.fingerprint == "same"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_refresh_error_keeps_last_good_snapshot_as_stale() -> None:
    registry, coordinator, _runner = coordinator_for_test()
    await coordinator.refresh_now(SCOPE, DiffRequest("working"))
    coordinator.runner_for = lambda _scope, _config: FakeRunner(
        RuntimeError("sem unavailable")
    )
    with pytest.raises(RuntimeError, match="unavailable"):
        await coordinator.refresh_now(SCOPE, DiffRequest("working"))
    current = registry.current_working(SCOPE)
    assert current is not None
    assert current.fingerprint == "same"
    assert current.stale is True
    assert current.error == "sem unavailable"


@pytest.mark.asyncio
async def test_ensure_current_never_awaits_wrong_generation() -> None:
    registry, coordinator, _runner = coordinator_for_test()
    coordinator.schedule(SCOPE, trigger="write", path_hints=["src/a.py"])
    old_task = FakeTask.instances[-1]
    newer = registry.reserve_generation(SCOPE, lane="working")
    registry.mark_pending(SCOPE, newer, lane="working")
    result = await coordinator.ensure_current(SCOPE, timeout_seconds=1)
    assert result.fingerprint == "same"
    assert old_task.awaited is False
    assert old_task.killed is True


@pytest.mark.asyncio
async def test_ensure_current_settles_matching_cancelled_debounce() -> None:
    registry, coordinator, _runner = coordinator_for_test()
    generation = coordinator.schedule(
        SCOPE,
        trigger="write",
        path_hints=["src/a.py"],
    )
    cancelled = FakeTask.instances[-1]
    cancelled.kill()

    result = await coordinator.ensure_current(SCOPE, timeout_seconds=1)

    assert result.fingerprint == "same"
    assert generation == 1
    assert cancelled.awaited
    assert registry.pending_generation(SCOPE) == 0
    assert registry.generation_is_settled(SCOPE)


@pytest.mark.asyncio
async def test_ensure_current_times_out_closed_on_pending_generation() -> None:
    registry, coordinator, _runner = coordinator_for_test()
    coordinator.task_factory = TimeoutTask
    coordinator.schedule(
        SCOPE,
        trigger="write",
        path_hints=["src/a.py"],
    )
    with pytest.raises(RefreshTimeoutError, match="did not finish"):
        await coordinator.ensure_current(SCOPE, timeout_seconds=0.01)


@pytest.mark.asyncio
async def test_ensure_current_never_returns_fingerprint_after_close() -> None:
    registry = ReviewRegistry()
    fingerprint_entered = threading.Event()
    release_fingerprint = threading.Event()

    def blocking_fingerprint(*_args: object) -> str:
        fingerprint_entered.set()
        assert release_fingerprint.wait(2), (
            "test fingerprint release timed out"
        )
        return "same"

    async def no_emit(
        _scope: ProjectScope,
        _revision: int,
    ) -> None:
        return None

    coordinator = RefreshCoordinator(
        registry=registry,
        runner_for=lambda _scope, _config: FakeRunner(),
        fingerprint=blocking_fingerprint,
        emit=no_emit,
    )
    coordinator.register_scope(SCOPE, parse_config({}))
    generation = registry.reserve_generation(SCOPE, lane="working")
    assert registry.publish_working(
        SCOPE,
        empty_snapshot("same"),
        generation,
    )
    ensure = asyncio.create_task(
        coordinator.ensure_current(SCOPE, timeout_seconds=1)
    )
    assert await asyncio.to_thread(fingerprint_entered.wait, 1)

    coordinator.close()
    release_fingerprint.set()

    with pytest.raises(RuntimeError, match="closed"):
        await asyncio.wait_for(ensure, timeout=1)
    assert coordinator._worker_executor._shutdown
    assert not coordinator._execution_locks
    assert not coordinator._evict_projects
    assert not coordinator._workers


@pytest.mark.asyncio
async def test_view_refresh_does_not_replace_working_pending_state() -> None:
    registry, coordinator, _runner = coordinator_for_test()
    working_generation = registry.reserve_generation(SCOPE, lane="working")
    registry.mark_pending(SCOPE, working_generation, lane="working")
    view = await coordinator.refresh_manual(
        SCOPE,
        [
            {
                "filePath": "src/app.py",
                "status": "modified",
                "beforeContent": "a",
                "afterContent": "b",
            }
        ],
    )
    assert view.request.mode == "stdin"
    assert registry.pending_generation(SCOPE, lane="working") == (
        working_generation
    )


@pytest.mark.asyncio
async def test_same_project_working_and_manual_sem_calls_never_overlap() -> None:
    runner = BlockingConcurrencyRunner()
    registry = ReviewRegistry()

    async def no_emit(
        _scope: ProjectScope,
        _revision: int,
    ) -> None:
        return None

    coordinator = RefreshCoordinator(
        registry=registry,
        runner_for=lambda _scope, _config: runner,
        fingerprint=lambda *_args: "same-project",
        emit=no_emit,
    )
    coordinator.register_scope(SCOPE, parse_config({}))

    working = asyncio.create_task(
        coordinator.refresh_now(SCOPE, DiffRequest("working"))
    )
    assert await asyncio.to_thread(runner.first_entered.wait, 1)
    manual = asyncio.create_task(
        coordinator.refresh_manual(
            SCOPE,
            [
                {
                    "filePath": "src/app.py",
                    "status": "modified",
                    "beforeContent": "before",
                    "afterContent": "after",
                }
            ],
        )
    )
    overlapped = await asyncio.to_thread(runner.two_entered.wait, 0.5)
    runner.release.set()
    await asyncio.gather(working, manual)

    assert overlapped is False
    assert runner.calls == 2
    assert runner.max_active == 1


@pytest.mark.asyncio
async def test_different_projects_can_execute_sem_in_parallel() -> None:
    runner = BlockingConcurrencyRunner()
    registry = ReviewRegistry()
    other_scope = ProjectScope(
        "ctx-other",
        "other",
        "other-project-id",
        Path("/other"),
        Path("/other/src"),
        "src",
    )

    async def no_emit(
        _scope: ProjectScope,
        _revision: int,
    ) -> None:
        return None

    coordinator = RefreshCoordinator(
        registry=registry,
        runner_for=lambda _scope, _config: runner,
        fingerprint=lambda *_args: "parallel-projects",
        emit=no_emit,
    )
    coordinator.register_scope(SCOPE, parse_config({}))
    coordinator.register_scope(other_scope, parse_config({}))

    first = asyncio.create_task(
        coordinator.refresh_now(SCOPE, DiffRequest("working"))
    )
    second = asyncio.create_task(
        coordinator.refresh_now(other_scope, DiffRequest("working"))
    )
    overlapped = await asyncio.to_thread(runner.two_entered.wait, 1)
    runner.release.set()
    await asyncio.gather(first, second)

    assert overlapped is True
    assert runner.calls == 2
    assert runner.max_active == 2


@pytest.mark.asyncio
async def test_cancelled_refresh_settles_generation_until_real_worker_exits() -> None:
    runner = BlockingConcurrencyRunner()
    registry = ReviewRegistry()

    async def no_emit(
        _scope: ProjectScope,
        _revision: int,
    ) -> None:
        return None

    coordinator = RefreshCoordinator(
        registry=registry,
        runner_for=lambda _scope, _config: runner,
        fingerprint=lambda *_args: "current",
        emit=no_emit,
    )
    coordinator.register_scope(SCOPE, parse_config({}))
    initial = registry.reserve_generation(SCOPE, lane="working")
    assert registry.publish_working(
        SCOPE,
        empty_snapshot("current"),
        initial,
    )
    refresh = asyncio.create_task(
        coordinator.refresh_now(SCOPE, DiffRequest("working"))
    )
    try:
        assert await asyncio.to_thread(runner.first_entered.wait, 1)
        refresh.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(refresh, timeout=0.5)

        assert registry.pending_generation(SCOPE) == 0
        assert registry.generation_is_settled(SCOPE)
        assert SCOPE.project_id in coordinator._workers
        current = await coordinator.ensure_current(
            SCOPE,
            timeout_seconds=0.5,
        )
        assert current.fingerprint == "current"
        assert registry.record_checkpoint_if_current(
            SCOPE,
            ReviewCheckpoint(
                project_id=SCOPE.project_id,
                fingerprint="current",
                outcome="pass",
                structural_entities=(),
                findings=(),
            ),
        )
    finally:
        runner.release.set()

    assert await asyncio.to_thread(runner.exited.wait, 1)
    await wait_until(
        lambda: SCOPE.project_id not in coordinator._workers
    )
    current = registry.current_working(SCOPE)
    assert current is not None and current.revision == 1


@pytest.mark.asyncio
async def test_obsolete_executor_queued_generation_never_runs_sem() -> None:
    queued_scope = ProjectScope(
        "ctx-queued",
        "queued",
        "queued-project-id",
        Path("/queued"),
        Path("/queued/src"),
        "src",
    )
    runner = QueuedGenerationRunner(SCOPE.project_id)
    registry = ReviewRegistry()

    async def no_emit(
        _scope: ProjectScope,
        _revision: int,
    ) -> None:
        return None

    coordinator = RefreshCoordinator(
        registry=registry,
        runner_for=lambda _scope, _config: runner,
        fingerprint=lambda root, *_args: str(root),
        emit=no_emit,
        worker_threads=1,
    )
    coordinator.register_scope(SCOPE, parse_config({}))
    coordinator.register_scope(queued_scope, parse_config({}))
    blocker = asyncio.create_task(
        coordinator.refresh_now(SCOPE, DiffRequest("working"))
    )
    stale: asyncio.Task[DiffSnapshot] | None = None
    latest: asyncio.Task[DiffSnapshot] | None = None
    try:
        assert await asyncio.to_thread(
            runner.blocking_entered.wait,
            1,
        )
        stale = asyncio.create_task(
            coordinator.refresh_now(
                queued_scope,
                DiffRequest("working"),
            )
        )
        await wait_until(
            lambda: registry.pending_generation(queued_scope) > 0
        )
        assert queued_scope.project_id not in coordinator._workers
        latest = asyncio.create_task(
            coordinator.refresh_now(
                queued_scope,
                DiffRequest("working"),
            )
        )
        await wait_until(
            lambda: registry.pending_generation(queued_scope) >= 2
        )
    finally:
        runner.release.set()

    await asyncio.wait_for(blocker, timeout=1)
    assert stale is not None and latest is not None
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stale, timeout=1)
    newest = await asyncio.wait_for(latest, timeout=1)
    await coordinator.drain_notifications()

    assert newest.fingerprint == str(queued_scope.project_root)
    assert runner.calls.count(queued_scope.project_id) == 1
    assert registry.generation_is_settled(queued_scope)


def test_unregister_context_ref_counts_lock_and_cancels_debounce() -> None:
    FakeTask.instances.clear()
    registry, coordinator, _runner = coordinator_for_test()
    sibling = ProjectScope(
        "ctx-sibling",
        SCOPE.project_name,
        SCOPE.project_id,
        SCOPE.project_root,
        SCOPE.watched_root,
        SCOPE.watched_relative,
    )
    coordinator.register_scope(sibling, parse_config({}))
    project_lock = coordinator._execution_lock(SCOPE)
    coordinator.schedule(
        SCOPE,
        trigger="write",
        path_hints=["src/a.py"],
    )
    debounce = FakeTask.instances[-1]

    assert coordinator.unregister_context(SCOPE.context_id) == (SCOPE,)
    assert debounce.killed
    assert not registry.has_scope(SCOPE)
    assert registry.has_scope(sibling)
    assert SCOPE.project_id in coordinator._execution_locks

    assert coordinator.unregister_context(sibling.context_id) == (sibling,)
    assert SCOPE.project_id not in coordinator._execution_locks
    assert not project_lock.locked()
    assert not coordinator._configs


@pytest.mark.asyncio
async def test_unregister_context_evicts_lock_after_running_worker_exits() -> None:
    runner = BlockingConcurrencyRunner()
    registry = ReviewRegistry()

    async def no_emit(
        _scope: ProjectScope,
        _revision: int,
    ) -> None:
        return None

    coordinator = RefreshCoordinator(
        registry=registry,
        runner_for=lambda _scope, _config: runner,
        fingerprint=lambda *_args: "cleanup",
        emit=no_emit,
    )
    coordinator.register_scope(SCOPE, parse_config({}))
    refresh = asyncio.create_task(
        coordinator.refresh_now(SCOPE, DiffRequest("working"))
    )
    try:
        assert await asyncio.to_thread(runner.first_entered.wait, 1)
        assert coordinator.unregister_context(SCOPE.context_id) == (
            SCOPE,
        )
        assert not registry.has_scope(SCOPE)
        assert not coordinator._configs
        assert SCOPE.project_id in coordinator._execution_locks
        assert SCOPE.project_id in coordinator._workers
    finally:
        runner.release.set()

    with pytest.raises(asyncio.CancelledError):
        await refresh
    await wait_until(
        lambda: SCOPE.project_id not in coordinator._workers
        and SCOPE.project_id not in coordinator._execution_locks
    )


def test_debounce_baseexception_settles_reserved_generation() -> None:
    registry, coordinator, _runner = coordinator_for_test()
    coordinator.task_factory = BaseFailureTask

    with pytest.raises(
        KeyboardInterrupt,
        match="debounce startup interrupted",
    ):
        coordinator.schedule(
            SCOPE,
            trigger="write",
            path_hints=["src/a.py"],
        )

    assert registry.pending_generation(SCOPE) == 0
    assert registry.generation_is_settled(SCOPE)
    assert coordinator._tasks == {}


@pytest.mark.asyncio
async def test_global_worker_admission_stays_bounded_when_waiters_cancel() -> None:
    runner = QueuedGenerationRunner(SCOPE.project_id)
    registry = ReviewRegistry()

    async def no_emit(
        _scope: ProjectScope,
        _revision: int,
    ) -> None:
        return None

    coordinator = RefreshCoordinator(
        registry=registry,
        runner_for=lambda _scope, _config: runner,
        fingerprint=lambda root, *_args: str(root),
        emit=no_emit,
        worker_threads=1,
    )
    coordinator.register_scope(SCOPE, parse_config({}))
    waiting_scopes = tuple(
        ProjectScope(
            f"ctx-waiting-{index}",
            f"waiting-{index}",
            f"waiting-project-{index}",
            Path(f"/waiting-{index}"),
            Path(f"/waiting-{index}/src"),
            "src",
        )
        for index in range(12)
    )
    for scope in waiting_scopes:
        coordinator.register_scope(scope, parse_config({}))

    blocker = asyncio.create_task(
        coordinator.refresh_now(SCOPE, DiffRequest("working"))
    )
    waiters: list[asyncio.Task[DiffSnapshot]] = []
    try:
        assert await asyncio.to_thread(
            runner.blocking_entered.wait,
            1,
        )
        waiters = [
            asyncio.create_task(
                coordinator.refresh_now(
                    scope,
                    DiffRequest("working"),
                )
            )
            for scope in waiting_scopes
        ]
        await wait_until(
            lambda: all(
                registry.pending_generation(scope) > 0
                for scope in waiting_scopes
            )
        )
        await asyncio.sleep(0.05)

        assert coordinator._worker_executor._work_queue.qsize() <= 1

        for waiter in waiters:
            waiter.cancel()
        results = await asyncio.gather(
            *waiters,
            return_exceptions=True,
        )
        assert all(
            isinstance(result, asyncio.CancelledError)
            for result in results
        )
        assert coordinator._worker_executor._work_queue.qsize() <= 1
        assert all(
            registry.generation_is_settled(scope)
            for scope in waiting_scopes
        )
        assert all(
            not coordinator._execution_lock(scope).locked()
            for scope in waiting_scopes
        )
    finally:
        runner.release.set()

    await asyncio.wait_for(blocker, timeout=1)
    assert runner.calls == [SCOPE.project_id]
    coordinator.close()


@pytest.mark.asyncio
async def test_terminal_close_settles_worker_and_blocks_late_publish_emit() -> None:
    runner = BlockingConcurrencyRunner()
    registry = ReviewRegistry()
    events: list[int] = []
    waiting_scope = ProjectScope(
        "ctx-waiting",
        "waiting",
        "waiting-project",
        Path("/waiting"),
        Path("/waiting/src"),
        "src",
    )

    async def emit(
        _scope: ProjectScope,
        revision: int,
    ) -> None:
        events.append(revision)

    coordinator = RefreshCoordinator(
        registry=registry,
        runner_for=lambda _scope, _config: runner,
        fingerprint=lambda *_args: "late-result",
        emit=emit,
        worker_threads=1,
    )
    coordinator.register_scope(SCOPE, parse_config({}))
    coordinator.register_scope(waiting_scope, parse_config({}))
    refresh = asyncio.create_task(
        coordinator.refresh_now(SCOPE, DiffRequest("working"))
    )
    assert await asyncio.to_thread(runner.first_entered.wait, 1)
    waiting_refresh = asyncio.create_task(
        coordinator.refresh_now(
            waiting_scope,
            DiffRequest("working"),
        )
    )
    await wait_until(
        lambda: registry.pending_generation(waiting_scope) > 0
    )

    started = asyncio.get_running_loop().time()
    coordinator.close()
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.1
    assert registry.pending_generation(SCOPE) == 0
    assert registry.generation_is_settled(SCOPE)
    assert registry.pending_generation(waiting_scope) == 0
    assert registry.generation_is_settled(waiting_scope)
    assert coordinator._worker_executor._shutdown
    with pytest.raises(RuntimeError, match="closed"):
        coordinator.schedule(
            SCOPE,
            trigger="write",
            path_hints=["src/a.py"],
        )

    runner.release.set()
    results = await asyncio.wait_for(
        asyncio.gather(
            refresh,
            waiting_refresh,
            return_exceptions=True,
        ),
        timeout=1,
    )
    assert all(
        isinstance(result, asyncio.CancelledError)
        for result in results
    )
    await coordinator.drain_notifications()

    assert registry.current_working(SCOPE) is None
    assert events == []


@pytest.mark.asyncio
async def test_close_before_execution_lock_creation_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, coordinator, _runner = coordinator_for_test()
    original_execution_lock = coordinator._execution_lock

    def close_before_lock(scope: ProjectScope) -> threading.Lock:
        coordinator.close()
        return original_execution_lock(scope)

    monkeypatch.setattr(
        coordinator,
        "_execution_lock",
        close_before_lock,
    )

    with pytest.raises(RuntimeError, match="closed"):
        await coordinator.refresh_now(SCOPE, DiffRequest("working"))

    assert registry.pending_generation(SCOPE) == 0
    assert registry.generation_is_settled(SCOPE)
    assert coordinator._worker_executor._shutdown
    assert not coordinator._execution_locks
    assert not coordinator._evict_projects
    assert not coordinator._workers
    assert not coordinator._tasks


@pytest.mark.asyncio
async def test_close_after_unregistered_execution_lock_creation_evicts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ReviewRegistry()

    async def no_emit(
        _scope: ProjectScope,
        _revision: int,
    ) -> None:
        return None

    coordinator = RefreshCoordinator(
        registry=registry,
        runner_for=lambda _scope, _config: FakeRunner(),
        fingerprint=lambda *_args: "never-run",
        emit=no_emit,
    )
    original_execution_lock = coordinator._execution_lock

    def close_after_lock(scope: ProjectScope) -> threading.Lock:
        project_lock = original_execution_lock(scope)
        coordinator.close()
        return project_lock

    monkeypatch.setattr(
        coordinator,
        "_execution_lock",
        close_after_lock,
    )

    with pytest.raises(asyncio.CancelledError):
        await coordinator.refresh_now(SCOPE, DiffRequest("working"))

    assert registry.pending_generation(SCOPE) == 0
    assert registry.generation_is_settled(SCOPE)
    assert coordinator._worker_executor._shutdown
    assert not coordinator._execution_locks
    assert not coordinator._evict_projects
    assert not coordinator._workers
    assert not coordinator._tasks


@pytest.mark.asyncio
async def test_stubborn_emitter_is_bounded_and_tracked_until_completion() -> None:
    registry = ReviewRegistry()
    runner = FakeRunner()
    emitter_started = asyncio.Event()
    emitter_cancelled = asyncio.Event()
    release_emitter = asyncio.Event()

    async def stubborn_emit(
        _scope: ProjectScope,
        _revision: int,
    ) -> None:
        emitter_started.set()
        while not release_emitter.is_set():
            try:
                await release_emitter.wait()
            except asyncio.CancelledError:
                emitter_cancelled.set()

    coordinator = RefreshCoordinator(
        registry=registry,
        runner_for=lambda _scope, _config: runner,
        fingerprint=lambda *_args: "stubborn-emitter",
        emit=stubborn_emit,
        emit_timeout_seconds=0.01,
    )
    coordinator.register_scope(SCOPE, parse_config({}))
    await coordinator.refresh_now(SCOPE, DiffRequest("working"))
    await asyncio.wait_for(emitter_started.wait(), timeout=0.5)
    await asyncio.wait_for(emitter_cancelled.wait(), timeout=0.5)

    drain = asyncio.create_task(coordinator.drain_notifications(SCOPE))
    done, _pending = await asyncio.wait({drain}, timeout=0.1)
    assert drain in done
    assert coordinator._emit_tasks
    assert all(
        not binding.task.done()
        for binding in coordinator._emit_tasks.values()
    )

    started = asyncio.get_running_loop().time()
    coordinator.close()
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.1
    assert coordinator._emit_tasks

    release_emitter.set()
    await wait_until(lambda: not coordinator._emit_tasks)
