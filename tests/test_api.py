from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from usr.plugins.sem_review_loop.api import sem_context, sem_diff, sem_detail, sem_impact, sem_status
from usr.plugins.sem_review_loop.helpers.config import PluginConfig
from usr.plugins.sem_review_loop.helpers.project_scope import ProjectScope
from usr.plugins.sem_review_loop.helpers.sem_types import (
    DiffRequest,
    DiffSnapshot,
    DiffSummary,
    EntityChange,
    EntityDetail,
    EntityRef,
)


SCOPE = ProjectScope("ctx", "project", "a" * 24, Path("/project"), Path("/project"), ".")
CONFIG = PluginConfig(".", True, 400, False, 2, "", 8000)
CHANGE = EntityChange(
    EntityRef("entity-1", "run", "function", "src/app.py"),
    "modified", 1, 2, 1, 2, "", "", True,
)
SNAPSHOT = DiffSnapshot(
    DiffRequest("working"), "a" * 64, 3,
    DiffSummary(1, 0, 1, 0, 0, 0, 0, 0, 0, 1),
    (CHANGE,), MappingProxyType({"entity-1": EntityDetail("before", "after")}),
    "0.21.0", "2026-07-29T00:00:00+00:00",
)


def response_json(response: object) -> dict[str, object]:
    assert getattr(response, "status_code") == 200
    payload = json.loads(getattr(response, "get_data")().decode())
    assert isinstance(payload, dict)
    return payload


@pytest.mark.asyncio
async def test_diff_rejects_boolean_mode_and_returns_no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sem_diff, "agent_scope_config", lambda *_: (SimpleNamespace(), SCOPE, CONFIG))
    result = await object.__new__(sem_diff.SemDiff).process({"mode": True}, SimpleNamespace())
    assert result.status_code == 400
    assert result.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_detail_requires_exact_current_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = SimpleNamespace(current_view=lambda _scope: SNAPSHOT)
    monkeypatch.setattr(sem_detail, "agent_scope_config", lambda *_: (SimpleNamespace(), SCOPE, CONFIG))
    monkeypatch.setattr(sem_detail, "get_registry", lambda: registry)
    result = await object.__new__(sem_detail.SemDetail).process(
        {"entity_id": "entity-1", "revision": 3, "fingerprint": "b" * 64},
        SimpleNamespace(),
    )
    assert result.status_code == 409


@pytest.mark.asyncio
async def test_context_and_impact_bind_revision_and_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = SimpleNamespace(current_view=lambda _scope: SNAPSHOT)
    calls: list[tuple[str, int, str]] = []

    class Coordinator:
        async def query_context(self, scope, entity, *, revision, fingerprint, token_budget):
            calls.append((entity.entity_id, revision, fingerprint))
            return {"context": "bounded"}

        async def query_impact(self, scope, entity, *, revision, fingerprint):
            calls.append((entity.entity_id, revision, fingerprint))
            return {"impact": []}

    for module in (sem_context, sem_impact):
        monkeypatch.setattr(module, "agent_scope_config", lambda *_: (SimpleNamespace(), SCOPE, CONFIG))
        monkeypatch.setattr(module, "get_registry", lambda: registry)
        monkeypatch.setattr(module, "get_coordinator", lambda: Coordinator())

    context = await object.__new__(sem_context.SemContext).process(
        {"entity_id": "entity-1", "revision": 3, "fingerprint": "a" * 64}, SimpleNamespace()
    )
    impact = await object.__new__(sem_impact.SemImpact).process(
        {"entity_id": "entity-1", "revision": 3, "fingerprint": "a" * 64}, SimpleNamespace()
    )
    assert response_json(context)["ok"] is True
    assert response_json(impact)["ok"] is True
    assert calls == [("entity-1", 3, "a" * 64), ("entity-1", 3, "a" * 64)]


@pytest.mark.asyncio
async def test_status_does_not_install_missing_managed_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sem_status, "agent_scope_config", lambda *_: (SimpleNamespace(), SCOPE, CONFIG))
    monkeypatch.setattr(sem_status, "get_registry", lambda: SimpleNamespace(public_status=lambda _scope: {"revision": 0}))
    monkeypatch.setattr(sem_status, "get_mcp_manager", lambda: SimpleNamespace(status=lambda _scope: {"enabled": False}))
    monkeypatch.setattr(sem_status.installer, "installed_binary_path", lambda: Path("/missing/sem"))
    monkeypatch.setattr(sem_status, "get_coordinator", lambda: SimpleNamespace(poll=lambda scope: None))
    result = await object.__new__(sem_status.SemStatus).process({}, SimpleNamespace())
    payload = response_json(result)
    assert payload["sem"]["available"] is False
    assert payload["sem"]["managed"] is True
