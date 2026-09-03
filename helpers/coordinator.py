from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from helpers.defer import DeferredTask
from usr.plugins.sem_review_loop.helpers.config import (
    PluginConfig,
    config_for_agent,
    parse_config,
)
from usr.plugins.sem_review_loop.helpers.fingerprints import diff_fingerprint
from usr.plugins.sem_review_loop.helpers.project_scope import (
    ProjectScope,
    scope_for_agent,
)
from usr.plugins.sem_review_loop.helpers.registry import (
    RefreshLane,
    ReviewRegistry,
)
from usr.plugins.sem_review_loop.helpers.sem_runner import (
    SemRunner,
    canonical_manual_payload,
    normalize_manual_files,
)
from usr.plugins.sem_review_loop.helpers.sem_types import (
    DiffRequest,
    DiffSnapshot,
    EntityRef,
)
from usr.plugins.sem_review_loop.helpers.ws_events import emit_revision


logger = logging.getLogger(__name__)

ScopeKey = tuple[str, str]


class RefreshRaceError(RuntimeError):
    pass


class RefreshTimeoutError(RuntimeError):
    pass


class _ObsoleteGeneration(RuntimeError):
    pass


@dataclass(frozen=True)
class _TaskBinding:
    scope: ProjectScope
    generation: int
    task: Any


@dataclass(frozen=True)
class _ExecutionAttempt:
    snapshot: DiffSnapshot
    stable: bool
    skipped: bool = False


@dataclass(frozen=True)
class _WorkerBinding:
    scope: ProjectScope
    generation: int
    lane: RefreshLane
    future: concurrent.futures.Future[Any]
    project_lock: threading.Lock


@dataclass(frozen=True)
class _EmitBinding:
    scope: ProjectScope
    loop: asyncio.AbstractEventLoop
    task: asyncio.Task[None]
    timeout_handle: asyncio.TimerHandle


class RefreshCoordinator:
    """Coordinate refreshes safely across Agent, Flask, and worker loops."""

    def __init__(
        self,
        *,
        registry: ReviewRegistry,
        runner_for: Callable[
            [ProjectScope, PluginConfig],
            SemRunner,
        ],
        fingerprint: Callable[..., str] = diff_fingerprint,
        emit: Callable[
            [ProjectScope, int],
            Awaitable[None],
        ] = emit_revision,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        task_factory: Callable[[], Any] = DeferredTask,
        emit_timeout_seconds: float = 1.0,
        worker_threads: int = 4,
    ) -> None:
        self.registry = registry
        self.runner_for = runner_for
        self.fingerprint = fingerprint
        self.emit = emit
        self.sleep = sleep
        self.task_factory = task_factory
        self.emit_timeout_seconds = max(float(emit_timeout_seconds), 0.01)
        worker_limit = max(int(worker_threads), 1)
        self._lock = threading.RLock()
        self._closed = False
        self._tasks: dict[ScopeKey, _TaskBinding | Any] = {}
        self._configs: dict[ScopeKey, PluginConfig] = {}
        self._scopes: dict[ScopeKey, ProjectScope] = {}
        self._execution_locks: dict[str, threading.Lock] = {}
        self._workers: dict[str, _WorkerBinding] = {}
        self._emit_tasks: dict[int, _EmitBinding] = {}
        self._evict_projects: set[str] = set()
        self._worker_admission = threading.BoundedSemaphore(worker_limit)
        self._worker_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=worker_limit,
            thread_name_prefix="sem-review",
        )

    @staticmethod
    def _key(scope: ProjectScope) -> ScopeKey:
        return scope.project_id, scope.context_id

    @staticmethod
    def _task_from_entry(entry: _TaskBinding | Any | None) -> Any | None:
        return entry.task if isinstance(entry, _TaskBinding) else entry

    def _is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise RuntimeError("Semantic Review coordinator is closed.")

    def _ensure_open(self) -> None:
        with self._lock:
            self._ensure_open_locked()

    def _matching_task(
        self,
        scope: ProjectScope,
        generation: int,
    ) -> Any | None:
        with self._lock:
            entry = self._tasks.get(self._key(scope))
            if isinstance(entry, _TaskBinding):
                if entry.generation != generation:
                    return None
                return entry.task
            # Preserve compatibility with direct test/integration injection.
            return entry

    def _remove_task_if_current(
        self,
        scope: ProjectScope,
        generation: int,
    ) -> None:
        with self._lock:
            key = self._key(scope)
            entry = self._tasks.get(key)
            if (
                isinstance(entry, _TaskBinding)
                and entry.generation == generation
            ):
                self._tasks.pop(key, None)

    @staticmethod
    def _kill_task(task: Any | None) -> None:
        if task is None:
            return
        try:
            task.kill()
        except Exception as exc:
            logger.warning(
                "Semantic Review debounce cancellation failed: %s",
                RefreshCoordinator._bounded_log_detail(exc),
            )

    @staticmethod
    def _bounded_log_detail(value: object) -> str:
        return " ".join(str(value).split())[:300] or "unknown error"

    def register_scope(
        self,
        scope: ProjectScope,
        config: PluginConfig,
    ) -> None:
        with self._lock:
            self._ensure_open_locked()
            self.registry.register(scope)
            self._configs[self._key(scope)] = config
            self._scopes[self._key(scope)] = scope
            self._evict_projects.discard(scope.project_id)

    def _config(self, scope: ProjectScope) -> PluginConfig:
        with self._lock:
            config = self._configs.get(self._key(scope))
        return config if config is not None else parse_config({})

    def _execution_lock(self, scope: ProjectScope) -> threading.Lock:
        with self._lock:
            self._ensure_open_locked()
            return self._execution_locks.setdefault(
                scope.project_id,
                threading.Lock(),
            )

    def _maybe_evict_project_lock_locked(
        self,
        project_id: str,
    ) -> None:
        if project_id not in self._evict_projects:
            return
        if any(key[0] == project_id for key in self._configs):
            self._evict_projects.discard(project_id)
            return
        if project_id in self._workers:
            return
        project_lock = self._execution_locks.get(project_id)
        if project_lock is not None and project_lock.locked():
            return
        self._execution_locks.pop(project_id, None)
        self._evict_projects.discard(project_id)

    def _release_untracked_project_lock(
        self,
        scope: ProjectScope,
        project_lock: threading.Lock,
    ) -> None:
        project_lock.release()
        with self._lock:
            self._maybe_evict_project_lock_locked(scope.project_id)

    def _worker_done(
        self,
        project_id: str,
        future: concurrent.futures.Future[Any],
        project_lock: threading.Lock,
    ) -> None:
        project_lock.release()
        self._worker_admission.release()
        with self._lock:
            binding = self._workers.get(project_id)
            if binding is not None and binding.future is future:
                self._workers.pop(project_id, None)
            self._maybe_evict_project_lock_locked(project_id)

    def _execute_if_current(
        self,
        scope: ProjectScope,
        generation: int,
        lane: RefreshLane,
        operation: Callable[[], Any],
    ) -> Any:
        if (
            self._is_closed()
            or not self.registry.generation_is_current(
                scope,
                generation,
                lane=lane,
            )
        ):
            raise _ObsoleteGeneration
        return operation()

    def _generation_can_run(
        self,
        scope: ProjectScope,
        generation: int,
        lane: RefreshLane,
    ) -> bool:
        return (
            not self._is_closed()
            and self.registry.generation_is_current(
                scope,
                generation,
                lane=lane,
            )
        )

    def _publish_if_open(
        self,
        publisher: Callable[
            [ProjectScope, DiffSnapshot, int],
            bool,
        ],
        scope: ProjectScope,
        snapshot: DiffSnapshot,
        generation: int,
    ) -> bool:
        with self._lock:
            if self._closed:
                return False
            return publisher(scope, snapshot, generation)

    def _publish_error_if_open(
        self,
        scope: ProjectScope,
        generation: int,
        error: object,
        *,
        lane: RefreshLane,
    ) -> bool:
        with self._lock:
            if self._closed:
                return False
            return self.registry.publish_error(
                scope,
                generation,
                error,
                lane=lane,
            )

    async def _run_project_worker(
        self,
        scope: ProjectScope,
        generation: int,
        lane: RefreshLane,
        operation: Callable[[], Any],
    ) -> Any:
        """Run one latest-generation SEM job with bounded global admission."""

        if not self._generation_can_run(scope, generation, lane):
            raise asyncio.CancelledError

        project_lock = self._execution_lock(scope)
        project_acquired = False
        admission_acquired = False
        try:
            while not project_lock.acquire(blocking=False):
                if not self._generation_can_run(
                    scope,
                    generation,
                    lane,
                ):
                    raise asyncio.CancelledError
                await asyncio.sleep(0.01)
            project_acquired = True

            while not self._worker_admission.acquire(blocking=False):
                if not self._generation_can_run(
                    scope,
                    generation,
                    lane,
                ):
                    raise asyncio.CancelledError
                await asyncio.sleep(0.01)
            admission_acquired = True

            if not self._generation_can_run(scope, generation, lane):
                raise asyncio.CancelledError

            with self._lock:
                if self._closed:
                    raise asyncio.CancelledError
                future = self._worker_executor.submit(
                    self._execute_if_current,
                    scope,
                    generation,
                    lane,
                    operation,
                )
                binding = _WorkerBinding(
                    scope=scope,
                    generation=generation,
                    lane=lane,
                    future=future,
                    project_lock=project_lock,
                )
                self._workers[scope.project_id] = binding
                future.add_done_callback(
                    lambda completed: self._worker_done(
                        scope.project_id,
                        completed,
                        project_lock,
                    )
                )
            project_acquired = False
            admission_acquired = False
        except BaseException:
            if admission_acquired:
                self._worker_admission.release()
            if project_acquired:
                self._release_untracked_project_lock(
                    scope,
                    project_lock,
                )
            raise

        try:
            return await asyncio.wrap_future(future)
        except _ObsoleteGeneration as exc:
            raise asyncio.CancelledError from exc

    @staticmethod
    def _cancel_emit(binding: _EmitBinding) -> None:
        def cancel() -> None:
            binding.timeout_handle.cancel()
            binding.task.cancel()

        try:
            binding.loop.call_soon_threadsafe(cancel)
        except RuntimeError:
            return

    def _emit_timed_out(self, task_id: int) -> None:
        with self._lock:
            binding = self._emit_tasks.get(task_id)
        if binding is None or binding.task.done():
            return
        logger.warning("Semantic Review revision event timed out.")
        binding.task.cancel()

    def _emit_done(self, task: asyncio.Task[None]) -> None:
        with self._lock:
            binding = self._emit_tasks.pop(id(task), None)
        if binding is not None:
            binding.timeout_handle.cancel()
        try:
            failure = task.exception()
        except asyncio.CancelledError:
            return
        except Exception:
            return
        if failure is not None:
            logger.warning(
                "Semantic Review revision event failed: %s",
                self._bounded_log_detail(failure),
            )

    async def drain_notifications(
        self,
        scope: ProjectScope | None = None,
    ) -> None:
        """Await emitters for one bounded interval without hiding live tasks."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.emit_timeout_seconds
        while True:
            with self._lock:
                tasks = tuple(
                    binding.task
                    for binding in self._emit_tasks.values()
                    if binding.loop is loop
                    and (scope is None or binding.scope == scope)
                )
            if not tasks:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.sleep(0)
                return
            _done, pending = await asyncio.wait(
                tasks,
                timeout=remaining,
            )
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.sleep(0)
                return

    def unregister_context(
        self,
        context_id: str,
    ) -> tuple[ProjectScope, ...]:
        """Cancel and forget all coordinator state owned by a context."""

        with self._lock:
            task_bindings = tuple(
                entry
                for key, entry in self._tasks.items()
                if key[1] == context_id
            )
            for key in tuple(self._tasks):
                if key[1] == context_id:
                    self._tasks.pop(key, None)
            configured_projects = {
                key[0] for key in self._configs if key[1] == context_id
            }
            for key in tuple(self._configs):
                if key[1] == context_id:
                    self._configs.pop(key, None)
                    self._scopes.pop(key, None)
            removed = self.registry.unregister_context(context_id)
            projects = configured_projects | {
                scope.project_id for scope in removed
            }
            emit_bindings = tuple(
                binding
                for binding in self._emit_tasks.values()
                if binding.scope.context_id == context_id
            )
            worker_bindings = tuple(
                binding
                for binding in self._workers.values()
                if binding.scope.context_id == context_id
            )
            for project_id in projects:
                if not any(
                    key[0] == project_id for key in self._configs
                ):
                    self._evict_projects.add(project_id)
                    self._maybe_evict_project_lock_locked(project_id)

        for entry in task_bindings:
            self._kill_task(self._task_from_entry(entry))
        for binding in emit_bindings:
            self._cancel_emit(binding)
        for binding in worker_bindings:
            binding.future.cancel()
        return removed

    @staticmethod
    def _has_relevant_hint(
        scope: ProjectScope,
        path_hints: list[str],
    ) -> bool:
        if not path_hints:
            return True
        probe = ReviewRegistry()
        probe.register(scope)
        return bool(probe.scopes_matching(path_hints))

    def schedule_for_agent(
        self,
        agent: object,
        trigger: str,
        path_hints: list[str],
    ) -> None:
        config = config_for_agent(agent)
        scope = scope_for_agent(agent, config.watched_subdirectory)
        self.register_scope(scope, config)
        if (
            config.automatic_refresh
            and self._has_relevant_hint(scope, path_hints)
        ):
            self.schedule(scope, trigger, path_hints)

    def schedule_for_path_hints(
        self,
        trigger: str,
        path_hints: list[str],
    ) -> None:
        for scope in self.registry.scopes_matching(path_hints):
            config = self._config(scope)
            if config.automatic_refresh:
                self.schedule(scope, trigger, path_hints)

    def schedule(
        self,
        scope: ProjectScope,
        trigger: str,
        path_hints: list[str],
    ) -> int:
        del trigger, path_hints
        previous_task: Any | None = None
        with self._lock:
            self._ensure_open_locked()
            generation = self.registry.reserve_generation(
                scope,
                lane="working",
            )
            self.registry.mark_pending(
                scope,
                generation,
                lane="working",
            )
            key = self._key(scope)
            previous_task = self._task_from_entry(
                self._tasks.get(key)
            )
            try:
                task = self.task_factory()
                started = task.start_task(
                    self._debounced_refresh,
                    scope,
                    generation,
                )
            except BaseException:
                self.registry.settle_generation(
                    scope,
                    generation,
                    lane="working",
                )
                raise
            self._tasks[key] = _TaskBinding(
                scope,
                generation,
                started,
            )
        self._kill_task(previous_task)
        return generation

    async def _debounced_refresh(
        self,
        scope: ProjectScope,
        generation: int,
    ) -> DiffSnapshot:
        try:
            await self.sleep(self._config(scope).debounce_ms / 1000)
            return await self._refresh_generation(
                scope,
                DiffRequest("working"),
                generation,
                lane="working",
                skip_if_current=True,
            )
        finally:
            self.registry.settle_generation(
                scope,
                generation,
                lane="working",
            )
            self._remove_task_if_current(scope, generation)

    def _reserve_direct(
        self,
        scope: ProjectScope,
        lane: RefreshLane,
    ) -> tuple[int, Any | None]:
        previous_task: Any | None = None
        with self._lock:
            self._ensure_open_locked()
            if lane == "working":
                previous_task = self._task_from_entry(
                    self._tasks.pop(self._key(scope), None)
                )
            generation = self.registry.reserve_generation(
                scope,
                lane=lane,
            )
            self.registry.mark_pending(
                scope,
                generation,
                lane=lane,
            )
        return generation, previous_task

    async def refresh_now(
        self,
        scope: ProjectScope,
        request: DiffRequest,
    ) -> DiffSnapshot:
        lane: RefreshLane = (
            "working" if request.mode == "working" else "view"
        )
        generation, previous_task = self._reserve_direct(scope, lane)
        self._kill_task(previous_task)
        return await self._refresh_generation(
            scope,
            request,
            generation,
            lane=lane,
            skip_if_current=False,
        )

    def _emit_best_effort(
        self,
        scope: ProjectScope,
        revision: int,
    ) -> None:
        async def deliver() -> None:
            if self._is_closed():
                return
            await self.emit(scope, revision)

        loop = asyncio.get_running_loop()
        with self._lock:
            if self._closed:
                return
            task = loop.create_task(deliver())
            timeout_handle = loop.call_later(
                self.emit_timeout_seconds,
                self._emit_timed_out,
                id(task),
            )
            binding = _EmitBinding(
                scope=scope,
                loop=loop,
                task=task,
                timeout_handle=timeout_handle,
            )
            self._emit_tasks[id(task)] = binding
        task.add_done_callback(self._emit_done)

    def _emit_current_revision(
        self,
        scope: ProjectScope,
    ) -> None:
        revision = int(self.registry.public_status(scope)["revision"])
        self._emit_best_effort(scope, revision)

    def _execute_diff_attempt(
        self,
        scope: ProjectScope,
        request: DiffRequest,
        generation: int,
        *,
        lane: RefreshLane,
        skip_if_current: bool,
    ) -> _ExecutionAttempt:
        config = self._config(scope)
        maximum_working_bytes = config.working_tree_payload_mb * 1024 * 1024
        before = self.fingerprint(
            scope.project_root,
            scope.watched_relative,
            request,
            maximum_working_bytes,
        )
        if not self.registry.generation_is_current(
            scope,
            generation,
            lane=lane,
        ):
            raise _ObsoleteGeneration
        if skip_if_current and lane == "working":
            current = self.registry.current_working(scope)
            if (
                current is not None
                and current.fingerprint == before
                and not current.stale
                and not current.error
            ):
                return _ExecutionAttempt(
                    snapshot=current,
                    stable=True,
                    skipped=True,
                )
        runner = self.runner_for(scope, config)
        snapshot = runner.diff(
            scope,
            request,
            before,
        )
        after = self.fingerprint(
            scope.project_root,
            scope.watched_relative,
            request,
            maximum_working_bytes,
        )
        return _ExecutionAttempt(
            snapshot=snapshot,
            stable=before == after,
        )

    def _execute_manual_diff(
        self,
        scope: ProjectScope,
        normalized: object,
        fingerprint: str,
        generation: int,
    ) -> DiffSnapshot:
        if not self.registry.generation_is_current(
            scope,
            generation,
            lane="view",
        ):
            raise _ObsoleteGeneration
        runner = self.runner_for(scope, self._config(scope))
        return runner.manual_diff(
            scope,
            normalized,
            fingerprint,
        )

    async def _refresh_generation(
        self,
        scope: ProjectScope,
        request: DiffRequest,
        generation: int,
        *,
        lane: RefreshLane,
        skip_if_current: bool,
    ) -> DiffSnapshot:
        self.registry.mark_pending(scope, generation, lane=lane)
        try:
            for _attempt in range(2):
                attempt = await self._run_project_worker(
                    scope,
                    generation,
                    lane,
                    lambda: self._execute_diff_attempt(
                        scope,
                        request,
                        generation,
                        lane=lane,
                        skip_if_current=skip_if_current,
                    ),
                )
                if attempt.skipped:
                    return attempt.snapshot
                if not attempt.stable:
                    continue
                publisher = (
                    self.registry.publish_working
                    if lane == "working"
                    else self.registry.publish_view
                )
                if not self._publish_if_open(
                    publisher,
                    scope,
                    attempt.snapshot,
                    generation,
                ):
                    raise asyncio.CancelledError
                published = (
                    self.registry.current_working(scope)
                    if lane == "working"
                    else self.registry.current_view(scope)
                )
                if published is None:
                    raise RefreshRaceError(
                        "Published semantic state was unavailable."
                    )
                self._emit_best_effort(
                    scope,
                    published.revision,
                )
                return published
            raise RefreshRaceError(
                "Working tree changed during semantic review."
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._publish_error_if_open(
                scope,
                generation,
                exc,
                lane=lane,
            ):
                self._emit_current_revision(scope)
            raise
        finally:
            self.registry.settle_generation(
                scope,
                generation,
                lane=lane,
            )

    async def refresh_manual(
        self,
        scope: ProjectScope,
        files: object,
    ) -> DiffSnapshot:
        normalized = normalize_manual_files(files)
        payload = canonical_manual_payload(normalized)
        fingerprint = hashlib.sha256(
            b"sem-manual-v1\0" + payload.encode("utf-8")
        ).hexdigest()
        generation, _previous = self._reserve_direct(scope, "view")
        try:
            snapshot = await self._run_project_worker(
                scope,
                generation,
                "view",
                lambda: self._execute_manual_diff(
                    scope,
                    normalized,
                    fingerprint,
                    generation,
                ),
            )
            if not self._publish_if_open(
                self.registry.publish_view,
                scope,
                snapshot,
                generation,
            ):
                raise asyncio.CancelledError
            published = self.registry.current_view(scope)
            if published is None:
                raise RefreshRaceError(
                    "Manual semantic result was unavailable."
                )
            self._emit_best_effort(scope, published.revision)
            return published
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._publish_error_if_open(
                scope,
                generation,
                exc,
                lane="view",
            ):
                self._emit_current_revision(scope)
            raise
        finally:
            self.registry.settle_generation(
                scope,
                generation,
                lane="view",
            )

    async def _query_current_view(
        self,
        scope: ProjectScope,
        *,
        revision: int,
        fingerprint: str,
        operation: Callable[[SemRunner], Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Run one bounded query while holding the exact view binding."""

        self._ensure_open()
        if self.registry.pending_generation(scope, lane="view"):
            raise RefreshRaceError("Semantic view is refreshing; retry the query.")
        current = self.registry.current_view(scope)
        if (
            current is None
            or current.request.mode == "stdin"
            or current.revision != revision
            or current.fingerprint != fingerprint
        ):
            raise RefreshRaceError("Semantic view changed; refresh and retry.")
        generation = self.registry.reserve_generation(scope, lane="view")
        try:
            result = await self._run_project_worker(
                scope,
                generation,
                "view",
                lambda: operation(self.runner_for(scope, self._config(scope))),
            )
            latest = self.registry.current_view(scope)
            if (
                latest is None
                or latest.revision != revision
                or latest.fingerprint != fingerprint
            ):
                raise RefreshRaceError(
                    "Semantic view changed while the query was running."
                )
            if not isinstance(result, Mapping):
                raise RefreshRaceError("Semantic query returned an invalid result.")
            return result
        finally:
            self.registry.settle_generation(scope, generation, lane="view")

    async def query_context(
        self,
        scope: ProjectScope,
        entity: EntityRef,
        *,
        revision: int,
        fingerprint: str,
        token_budget: int,
    ) -> Mapping[str, Any]:
        return await self._query_current_view(
            scope,
            revision=revision,
            fingerprint=fingerprint,
            operation=lambda runner: runner.context(
                scope,
                entity,
                token_budget,
            ),
        )

    async def query_impact(
        self,
        scope: ProjectScope,
        entity: EntityRef,
        *,
        revision: int,
        fingerprint: str,
    ) -> Mapping[str, Any]:
        return await self._query_current_view(
            scope,
            revision=revision,
            fingerprint=fingerprint,
            operation=lambda runner: runner.impact(scope, entity),
        )

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RefreshTimeoutError(
                "Pending semantic refresh did not finish in time."
            )
        return remaining

    async def ensure_current(
        self,
        scope: ProjectScope,
        timeout_seconds: float,
    ) -> DiffSnapshot:
        self._ensure_open()
        if timeout_seconds <= 0:
            raise RefreshTimeoutError(
                "Pending semantic refresh did not finish in time."
            )
        deadline = time.monotonic() + timeout_seconds
        while True:
            self._ensure_open()
            pending = self.registry.pending_generation(
                scope,
                lane="working",
            )
            if pending:
                task = self._matching_task(scope, pending)
                if task is not None:
                    try:
                        await task.result(self._remaining(deadline))
                        self._ensure_open()
                    except (
                        TimeoutError,
                        asyncio.TimeoutError,
                    ) as exc:
                        raise RefreshTimeoutError(
                            "Pending semantic refresh did not finish "
                            "in time."
                        ) from exc
                    except concurrent.futures.CancelledError:
                        self.registry.settle_generation(
                            scope,
                            pending,
                            lane="working",
                        )
                        self._remove_task_if_current(scope, pending)
                        continue
                    continue

            request = DiffRequest("working")
            try:
                expected = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.fingerprint,
                        scope.project_root,
                        scope.watched_relative,
                        request,
                        self._config(scope).working_tree_payload_mb
                        * 1024
                        * 1024,
                    ),
                    timeout=self._remaining(deadline),
                )
            except asyncio.TimeoutError as exc:
                raise RefreshTimeoutError(
                    "Pending semantic refresh did not finish in time."
                ) from exc
            self._ensure_open()
            current = self.registry.current_working_if_settled(
                scope,
                expected,
            )
            if current is not None:
                self._ensure_open()
                return current
            self._ensure_open()
            try:
                refreshed = await asyncio.wait_for(
                    self.refresh_now(scope, request),
                    timeout=self._remaining(deadline),
                )
            except asyncio.TimeoutError as exc:
                raise RefreshTimeoutError(
                    "Pending semantic refresh did not finish in time."
                ) from exc
            self._ensure_open()
            return refreshed

    def close(self) -> None:
        """Terminally stop work without waiting for non-cooperative workers."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            entries = tuple(self._tasks.values())
            self._tasks.clear()
            emit_bindings = tuple(self._emit_tasks.values())
            worker_bindings = tuple(self._workers.values())
            scopes = tuple(self._scopes.values())
            projects = {
                key[0] for key in self._configs
            } | {
                binding.scope.project_id
                for binding in worker_bindings
            } | set(self._execution_locks)
            self._configs.clear()
            self._scopes.clear()
            self._evict_projects.update(projects)
        for entry in entries:
            self._kill_task(self._task_from_entry(entry))
            if isinstance(entry, _TaskBinding):
                self.registry.settle_generation(
                    entry.scope,
                    entry.generation,
                    lane="working",
                )
        for scope in scopes:
            for lane in ("working", "view"):
                pending = self.registry.pending_generation(
                    scope,
                    lane=lane,
                )
                if pending:
                    self.registry.settle_generation(
                        scope,
                        pending,
                        lane=lane,
                    )
        for binding in emit_bindings:
            self._cancel_emit(binding)
        for binding in worker_bindings:
            binding.future.cancel()
        self._worker_executor.shutdown(
            wait=False,
            cancel_futures=True,
        )
        with self._lock:
            for project_id in projects:
                self._maybe_evict_project_lock_locked(project_id)

    def cancel_all(self) -> None:
        """Backward-compatible terminal cleanup used by service reset."""

        self.close()
