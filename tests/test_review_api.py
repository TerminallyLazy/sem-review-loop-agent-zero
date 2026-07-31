from __future__ import annotations

from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from usr.plugins.sem_review_loop.api import sem_review
from usr.plugins.sem_review_loop.helpers.project_scope import ProjectScope
from usr.plugins.sem_review_loop.helpers.registry import ReviewRegistry
from usr.plugins.sem_review_loop.helpers.sem_types import (
    DiffRequest,
    DiffSnapshot,
    DiffSummary,
    EntityChange,
    EntityRef,
)


SCOPE = ProjectScope(
    "ctx",
    "project",
    "project-id",
    Path("/project"),
    Path("/project"),
    ".",
)
CHANGE = EntityChange(
    EntityRef("entity-1", "run", "function", "src/app.py"),
    "modified",
    1,
    2,
    1,
    2,
    "",
    "",
    True,
)


def snapshot(
    fingerprint: str,
    *,
    mode: str = "working",
) -> DiffSnapshot:
    return DiffSnapshot(
        DiffRequest(mode),  # type: ignore[arg-type]
        fingerprint,
        0,
        DiffSummary(1, 0, 1, 0, 0, 0, 0, 0, 0, 1),
        (CHANGE,),
        MappingProxyType({}),
        "0.21.0",
        "2026-07-29T00:00:00+00:00",
    )


def handler() -> sem_review.SemReview:
    return object.__new__(sem_review.SemReview)


def publish_working(registry: ReviewRegistry) -> DiffSnapshot:
    registry.register(SCOPE)
    generation = registry.reserve_generation(SCOPE, lane="working")
    assert registry.publish_working(
        SCOPE,
        snapshot("working-fingerprint"),
        generation,
    )
    current = registry.current_working(SCOPE)
    assert current is not None
    return current


@pytest.fixture(autouse=True)
def scope_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sem_review,
        "agent_and_scope",
        lambda _handler, _input: (SimpleNamespace(), SCOPE),
    )


@pytest.mark.asyncio
async def test_cancel_records_exact_current_revision_fingerprint_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ReviewRegistry()
    current = publish_working(registry)
    monkeypatch.setattr(sem_review, "get_registry", lambda: registry)

    result = await handler().process(
        {
            "context_id": SCOPE.context_id,
            "action": "cancel",
            "revision": current.revision,
            "fingerprint": current.fingerprint,
        },
        SimpleNamespace(),
    )

    checkpoint = registry.checkpoint_for(SCOPE)
    assert result["ok"] is True
    assert checkpoint is not None
    assert checkpoint.outcome == "cancelled"
    assert checkpoint.fingerprint == current.fingerprint
    assert checkpoint.structural_entities == ("entity-1",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("revision_delta", "fingerprint"),
    [
        (1, "working-fingerprint"),
        (0, "stale-fingerprint"),
    ],
)
async def test_cancel_returns_conflict_on_snapshot_drift(
    monkeypatch: pytest.MonkeyPatch,
    revision_delta: int,
    fingerprint: str,
) -> None:
    registry = ReviewRegistry()
    current = publish_working(registry)
    monkeypatch.setattr(sem_review, "get_registry", lambda: registry)

    response = await handler().process(
        {
            "context_id": SCOPE.context_id,
            "action": "cancel",
            "revision": current.revision + revision_delta,
            "fingerprint": fingerprint,
        },
        SimpleNamespace(),
    )

    assert response.status_code == 409
    assert registry.checkpoint_for(SCOPE) is None


@pytest.mark.asyncio
async def test_cancel_rejects_manual_view_tokens_and_pending_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ReviewRegistry()
    working = publish_working(registry)
    view_generation = registry.reserve_generation(SCOPE, lane="view")
    assert registry.publish_view(
        SCOPE,
        snapshot("manual-fingerprint", mode="stdin"),
        view_generation,
    )
    manual = registry.current_view(SCOPE)
    assert manual is not None
    monkeypatch.setattr(sem_review, "get_registry", lambda: registry)

    manual_response = await handler().process(
        {
            "context_id": SCOPE.context_id,
            "action": "cancel",
            "revision": manual.revision,
            "fingerprint": manual.fingerprint,
        },
        SimpleNamespace(),
    )
    assert manual_response.status_code == 409
    assert registry.checkpoint_for(SCOPE) is None

    pending = registry.reserve_generation(SCOPE, lane="working")
    registry.mark_pending(SCOPE, pending, lane="working")
    pending_response = await handler().process(
        {
            "context_id": SCOPE.context_id,
            "action": "cancel",
            "revision": working.revision,
            "fingerprint": working.fingerprint,
        },
        SimpleNamespace(),
    )
    assert pending_response.status_code == 409
    assert registry.checkpoint_for(SCOPE) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("revision", "fingerprint"),
    [
        (None, "working-fingerprint"),
        (True, "working-fingerprint"),
        (-1, "working-fingerprint"),
        (1, ""),
        (1, "x" * 257),
    ],
)
async def test_cancel_rejects_invalid_preconditions(
    monkeypatch: pytest.MonkeyPatch,
    revision: object,
    fingerprint: object,
) -> None:
    registry = ReviewRegistry()
    publish_working(registry)
    monkeypatch.setattr(sem_review, "get_registry", lambda: registry)

    response = await handler().process(
        {
            "context_id": SCOPE.context_id,
            "action": "cancel",
            "revision": revision,
            "fingerprint": fingerprint,
        },
        SimpleNamespace(),
    )

    assert response.status_code == 400
    assert registry.checkpoint_for(SCOPE) is None
