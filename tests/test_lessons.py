from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from usr.plugins.sem_review_loop.helpers.lessons import (
    LessonError,
    LessonStaleError,
    LessonStore,
)
from usr.plugins.sem_review_loop.helpers.project_scope import ProjectScope
from usr.plugins.sem_review_loop.helpers.sem_types import (
    DiffRequest,
    DiffSnapshot,
    DiffSummary,
    EntityChange,
    EntityDetail,
    EntityRef,
)


def make_scope(root: Path) -> ProjectScope:
    root.mkdir()
    return ProjectScope("ctx", "project", "a" * 24, root, root, ".")


def make_snapshot(fingerprint: str = "a" * 64) -> DiffSnapshot:
    change = EntityChange(
        EntityRef("entity-1", "function", "function", "src/app.py"),
        "modified", 1, 2, 1, 2, "", "", True,
    )
    return DiffSnapshot(
        DiffRequest("working"), fingerprint, 4,
        DiffSummary(1, 0, 1, 0, 0, 0, 0, 0, 0, 1),
        (change,), MappingProxyType({"entity-1": EntityDetail("before", "after")}),
        "0.21.0", "2026-07-29T00:00:00+00:00",
    )


def test_propose_deduplicates_across_fingerprints_and_requires_approval(tmp_path: Path) -> None:
    scope = make_scope(tmp_path / "project")
    store = LessonStore(tmp_path / "data")
    first = store.propose_from_checkpoint(
        scope, make_snapshot(), problem="A bounded review finding.",
        resolution="The checkpoint was recorded.",
    )
    assert first is not None
    second = store.propose_from_checkpoint(
        scope, make_snapshot("b" * 64), problem="A bounded review finding.",
        resolution="The checkpoint was recorded.",
    )
    assert second is not None and second.proposal_id == first.proposal_id
    assert len(store.list(scope)["pending"]) == 1
    with pytest.raises(LessonStaleError):
        store.approve(scope, first.proposal_id, current_fingerprint="b" * 64)
    approved = store.approve(scope, first.proposal_id, current_fingerprint="a" * 64)
    assert approved.status == "approved"
    assert store.list(scope)["pending"] == []
    assert store.list(scope)["approved"][0]["status"] == "approved"


def test_persisted_unknown_fields_and_unsafe_prose_fail_closed(tmp_path: Path) -> None:
    scope = make_scope(tmp_path / "project")
    store = LessonStore(tmp_path / "data")
    store.propose_from_checkpoint(
        scope, make_snapshot(), problem="A bounded review finding.",
        resolution="The checkpoint was recorded.",
    )
    path = tmp_path / "data" / scope.project_id / "lessons.json"
    text = path.read_text(encoding="utf-8").replace(
        '"status": "pending"', '"unknown": true,\n        "status": "pending"', 1,
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(LessonError):
        store.list(scope)


def test_matching_is_bounded_and_project_scoped(tmp_path: Path) -> None:
    scope = make_scope(tmp_path / "project")
    other = ProjectScope("ctx", "other", "b" * 24, scope.project_root, scope.watched_root, ".")
    store = LessonStore(tmp_path / "data")
    proposal = store.propose_from_checkpoint(
        scope, make_snapshot(), problem="A bounded review finding.",
        resolution="The checkpoint was recorded.",
    )
    assert proposal is not None
    store.approve(scope, proposal.proposal_id, current_fingerprint="a" * 64)
    assert len(store.match(scope, make_snapshot())) == 1
    assert store.match(other, make_snapshot()) == ()
