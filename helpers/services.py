from __future__ import annotations

import threading
from functools import partial
from typing import TYPE_CHECKING

from usr.plugins.sem_review_loop.helpers import installer
from usr.plugins.sem_review_loop.helpers.config import PluginConfig
from usr.plugins.sem_review_loop.helpers.coordinator import RefreshCoordinator
from usr.plugins.sem_review_loop.helpers.paths import (
    CACHE_ROOT,
    PROJECT_DATA_ROOT,
)
from usr.plugins.sem_review_loop.helpers.project_scope import ProjectScope
from usr.plugins.sem_review_loop.helpers.registry import ReviewRegistry
from usr.plugins.sem_review_loop.helpers.sem_runner import SemRunner

if TYPE_CHECKING:
    from usr.plugins.sem_review_loop.helpers.lessons import LessonStore
    from usr.plugins.sem_review_loop.helpers.mcp_manager import MCPManager


_lock = threading.RLock()
_registry: ReviewRegistry | None = None
_coordinator: RefreshCoordinator | None = None
_mcp_manager: MCPManager | None = None
_lesson_store: LessonStore | None = None


def get_registry() -> ReviewRegistry:
    global _registry
    with _lock:
        if _registry is None:
            _registry = ReviewRegistry()
        return _registry


def _runner_for(
    scope: ProjectScope,
    config: PluginConfig,
) -> SemRunner:
    del scope
    return SemRunner(
        CACHE_ROOT,
        binary_lease=partial(
            installer.lease_binary,
            config.custom_sem_binary,
        ),
    )


def get_coordinator() -> RefreshCoordinator:
    global _coordinator
    with _lock:
        if _coordinator is None:
            _coordinator = RefreshCoordinator(
                registry=get_registry(),
                runner_for=_runner_for,
            )
        return _coordinator


def get_mcp_manager() -> MCPManager:
    global _mcp_manager
    with _lock:
        if _mcp_manager is None:
            from usr.plugins.sem_review_loop.helpers.mcp_manager import (
                MCPManager,
            )

            _mcp_manager = MCPManager(registry=get_registry())
        return _mcp_manager


def get_lesson_store() -> LessonStore:
    global _lesson_store
    with _lock:
        if _lesson_store is None:
            from usr.plugins.sem_review_loop.helpers.lessons import (
                LessonStore,
            )

            _lesson_store = LessonStore(PROJECT_DATA_ROOT)
        return _lesson_store


def reset_services_for_tests() -> None:
    global _registry, _coordinator, _mcp_manager, _lesson_store
    with _lock:
        coordinator = _coordinator
        _registry = None
        _coordinator = None
        _mcp_manager = None
        _lesson_store = None
    if coordinator is not None:
        coordinator.cancel_all()
