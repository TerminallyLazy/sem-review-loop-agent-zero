from __future__ import annotations

import json
import threading
from pathlib import Path
from types import MappingProxyType

from usr.plugins.sem_review_loop.helpers.project_scope import ProjectScope
from usr.plugins.sem_review_loop.helpers.registry import (
    ReviewCheckpoint,
    ReviewRegistry,
)
from usr.plugins.sem_review_loop.helpers.sem_types import (
    DiffRequest,
    DiffSnapshot,
    DiffSummary,
    EntityChange,
    EntityDetail,
    EntityRef,
)


SCOPE = ProjectScope(
    context_id="ctx",
    project_name="project",
    project_id="project-id",
    project_root=Path("/absolute/project"),
    watched_root=Path("/absolute/project/src"),
    watched_relative="src",
)


def snapshot(
    fingerprint: str,
    *,
    request: DiffRequest = DiffRequest("working"),
    structural: bool = True,
) -> DiffSnapshot:
    entity = EntityRef("entity-1", "run", "function", "src/app.py")
    change = EntityChange(
        entity=entity,
        change_type="modified",
        start_line=1,
        end_line=2,
        old_start_line=1,
        old_end_line=2,
        old_entity_name="",
        old_file_path="",
        structural=structural,
    )
    return DiffSnapshot(
        request=request,
        fingerprint=fingerprint,
        revision=0,
        summary=DiffSummary(1, 0, 1, 0, 0, 0, 0, 0, 0, 1),
        changes=(change,),
        details=MappingProxyType(
            {"entity-1": EntityDetail("before", "after")}
        ),
        sem_version="0.21.0",
        completed_at="2026-07-29T00:00:00+00:00",
    )


def test_stale_generation_cannot_replace_newer_snapshot() -> None:
    registry = ReviewRegistry()
    registry.register(SCOPE)
    registry.reserve_generation(SCOPE)
    newer = registry.reserve_generation(SCOPE)
    assert registry.publish_working(SCOPE, snapshot("new"), newer)
    assert not registry.publish_working(SCOPE, snapshot("old"), newer - 1)
    current = registry.current_working(SCOPE)
    assert current is not None
    assert current.fingerprint == "new"


def test_view_lane_never_invalidates_completion_lane() -> None:
    registry = ReviewRegistry()
    registry.register(SCOPE)
    working_generation = registry.reserve_generation(SCOPE, lane="working")
    registry.mark_pending(SCOPE, working_generation, lane="working")
    view_generation = registry.reserve_generation(SCOPE, lane="view")
    registry.mark_pending(SCOPE, view_generation, lane="view")

    assert registry.publish_view(
        SCOPE,
        snapshot(
            "commit",
            request=DiffRequest("commit", commit="HEAD"),
        ),
        view_generation,
    )
    registry.clear_pending(SCOPE, view_generation, lane="view")
    assert registry.pending_generation(SCOPE, lane="working") == (
        working_generation
    )
    assert registry.publish_working(
        SCOPE,
        snapshot("working"),
        working_generation,
    )
    current = registry.current_working(SCOPE)
    selected = registry.current_view(SCOPE)
    assert current is not None and current.fingerprint == "working"
    assert selected is not None and selected.fingerprint == "commit"


def test_legacy_generation_api_still_supports_view_publication() -> None:
    registry = ReviewRegistry()
    registry.register(SCOPE)
    working_generation = registry.reserve_generation(SCOPE)
    assert registry.publish_working(
        SCOPE,
        snapshot("working"),
        working_generation,
    )
    view_generation = registry.reserve_generation(SCOPE)
    assert registry.publish_view(
        SCOPE,
        snapshot(
            "commit",
            request=DiffRequest("commit", commit="HEAD"),
        ),
        view_generation,
    )


def test_public_status_contains_no_source_or_absolute_path() -> None:
    registry = ReviewRegistry()
    registry.register(SCOPE)
    generation = registry.reserve_generation(SCOPE)
    registry.publish_working(SCOPE, snapshot("working"), generation)
    encoded = json.dumps(registry.public_status(SCOPE))
    assert "before_content" not in encoded
    assert "after_content" not in encoded
    assert "before" not in encoded
    assert "after" not in encoded
    assert str(SCOPE.project_root) not in encoded


def test_checkpoint_requires_project_and_exact_fingerprint() -> None:
    registry = ReviewRegistry()
    registry.register(SCOPE)
    generation = registry.reserve_generation(SCOPE)
    registry.publish_working(SCOPE, snapshot("current"), generation)
    registry.record_checkpoint(
        SCOPE,
        ReviewCheckpoint(
            project_id="other",
            fingerprint="current",
            outcome="pass",
            structural_entities=("entity-1",),
            findings=(),
        ),
    )
    assert registry.checkpoint_is_current(SCOPE, "current") is False
    registry.record_checkpoint(
        SCOPE,
        ReviewCheckpoint(
            project_id=SCOPE.project_id,
            fingerprint="current",
            outcome="pass",
            structural_entities=("entity-1",),
            findings=(),
        ),
    )
    assert registry.checkpoint_is_current(SCOPE, "current") is True
    assert registry.checkpoint_is_current(SCOPE, "different") is False


def test_checkpoint_atomic_record_requires_exact_current_structural_set() -> None:
    registry = ReviewRegistry()
    registry.register(SCOPE)
    generation = registry.reserve_generation(SCOPE)
    registry.publish_working(SCOPE, snapshot("current"), generation)
    valid = ReviewCheckpoint(
        project_id=SCOPE.project_id,
        fingerprint="current",
        outcome="pass",
        structural_entities=("entity-1",),
        findings=(),
    )
    wrong_entities = ReviewCheckpoint(
        project_id=SCOPE.project_id,
        fingerprint="current",
        outcome="pass",
        structural_entities=(),
        findings=(),
    )
    assert not registry.record_checkpoint_if_current(
        SCOPE,
        wrong_entities,
    )
    pending = registry.reserve_generation(SCOPE, lane="working")
    registry.mark_pending(SCOPE, pending, lane="working")
    assert not registry.record_checkpoint_if_current(SCOPE, valid)
    registry.clear_pending(SCOPE, pending, lane="working")
    assert not registry.record_checkpoint_if_current(SCOPE, valid)
    assert registry.publish_working(
        SCOPE,
        snapshot("current"),
        pending,
    )
    assert registry.record_checkpoint_if_current(SCOPE, valid)

    newer = registry.reserve_generation(SCOPE)
    registry.publish_working(SCOPE, snapshot("newer"), newer)
    assert not registry.record_checkpoint_if_current(SCOPE, valid)
    assert registry.checkpoint_for(SCOPE) is None


def test_abandoned_generation_settlement_restores_exact_checkpoint_state() -> None:
    registry = ReviewRegistry()
    registry.register(SCOPE)
    published = registry.reserve_generation(SCOPE, lane="working")
    assert registry.publish_working(
        SCOPE,
        snapshot("current"),
        published,
    )
    abandoned = registry.reserve_generation(SCOPE, lane="working")
    registry.mark_pending(SCOPE, abandoned, lane="working")
    checkpoint = ReviewCheckpoint(
        project_id=SCOPE.project_id,
        fingerprint="current",
        outcome="pass",
        structural_entities=("entity-1",),
        findings=(),
    )

    assert not registry.generation_is_settled(SCOPE, lane="working")
    assert not registry.record_checkpoint_if_current(SCOPE, checkpoint)
    registry.settle_generation(SCOPE, abandoned, lane="working")

    assert registry.pending_generation(SCOPE, lane="working") == 0
    assert registry.generation_is_settled(SCOPE, lane="working")
    assert (
        registry.current_working_if_settled(SCOPE, "current")
        is not None
    )
    assert registry.record_checkpoint_if_current(SCOPE, checkpoint)


def test_changed_fingerprint_resets_cycle_checkpoint_and_disclosure() -> None:
    registry = ReviewRegistry()
    registry.register(SCOPE)
    first_generation = registry.reserve_generation(SCOPE)
    registry.publish_working(SCOPE, snapshot("first"), first_generation)
    registry.increment_repair_cycle(SCOPE, "first")
    assert registry.claim_disclosure(SCOPE, "first") is True
    registry.record_checkpoint(
        SCOPE,
        ReviewCheckpoint(
            project_id=SCOPE.project_id,
            fingerprint="first",
            outcome="pass",
            structural_entities=("entity-1",),
            findings=(),
        ),
    )
    second_generation = registry.reserve_generation(SCOPE)
    registry.publish_working(SCOPE, snapshot("second"), second_generation)
    assert registry.repair_cycle(SCOPE, "second") == 0
    assert registry.checkpoint_for(SCOPE) is None
    assert registry.claim_disclosure(SCOPE, "second") is True


def test_scopes_matching_accepts_workdir_virtual_paths_and_ignores_metadata() -> None:
    registry = ReviewRegistry()
    registry.register(SCOPE)
    assert registry.scopes_matching(["/src/app.py"]) == (SCOPE,)
    assert registry.scopes_matching(
        ["/.a0proj/mcp_servers.json"]
    ) == ()


def test_absolute_hint_prefers_real_registered_scope_before_virtual_fallback() -> None:
    registry = ReviewRegistry()
    broad_scope = ProjectScope(
        context_id="ctx-broad",
        project_name="broad",
        project_id="broad-id",
        project_root=Path("/other"),
        watched_root=Path("/other"),
        watched_relative=".",
    )
    real_scope = ProjectScope(
        context_id="ctx-real",
        project_name="real",
        project_id="real-id",
        project_root=Path("/real"),
        watched_root=Path("/real"),
        watched_relative=".",
    )
    registry.register(broad_scope)
    registry.register(real_scope)

    assert registry.scopes_matching(["/real/src/app.py"]) == (
        real_scope,
    )
    assert registry.scopes_matching(["/src/app.py"]) == (
        broad_scope,
        real_scope,
    )


def test_unregister_context_removes_state_and_final_project_mcp_flag() -> None:
    registry = ReviewRegistry()
    sibling = ProjectScope(
        context_id="ctx-sibling",
        project_name=SCOPE.project_name,
        project_id=SCOPE.project_id,
        project_root=SCOPE.project_root,
        watched_root=SCOPE.watched_root,
        watched_relative=SCOPE.watched_relative,
    )
    registry.register(SCOPE)
    registry.register(sibling)
    registry.set_mcp_enabled(SCOPE, True)

    assert registry.unregister_context(SCOPE.context_id) == (SCOPE,)
    assert not registry.has_scope(SCOPE)
    assert registry.has_scope(sibling)
    assert registry.mcp_enabled(sibling)

    assert registry.unregister_context(sibling.context_id) == (sibling,)
    assert not registry.has_scope(sibling)
    assert not registry.mcp_enabled(sibling)


def test_generation_reservations_are_thread_safe() -> None:
    registry = ReviewRegistry()
    registry.register(SCOPE)
    generations: list[int] = []
    guard = threading.Lock()

    def reserve() -> None:
        value = registry.reserve_generation(SCOPE, lane="working")
        with guard:
            generations.append(value)

    threads = [threading.Thread(target=reserve) for _index in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(generations)) == 40
    assert min(generations) == 1
    assert max(generations) == 40
