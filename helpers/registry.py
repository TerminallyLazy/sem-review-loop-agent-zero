from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal

from usr.plugins.sem_review_loop.helpers.project_scope import ProjectScope
from usr.plugins.sem_review_loop.helpers.sem_types import DiffSnapshot


CheckpointOutcome = Literal[
    "pass",
    "repaired",
    "unresolved",
    "cancelled",
]
RefreshLane = Literal["working", "view"]
ScopeKey = tuple[str, str]


@dataclass(frozen=True)
class ReviewCheckpoint:
    project_id: str
    fingerprint: str
    outcome: CheckpointOutcome
    structural_entities: tuple[str, ...]
    findings: tuple[str, ...]


@dataclass
class ProjectReviewState:
    scope: ProjectScope
    working_snapshot: DiffSnapshot | None = None
    view_snapshot: DiffSnapshot | None = None
    next_generation: int = 0
    working_reserved_generation: int = 0
    view_reserved_generation: int = 0
    working_generation: int = 0
    view_generation: int = 0
    working_pending_generation: int = 0
    view_pending_generation: int = 0
    revision: int = 0
    checkpoint: ReviewCheckpoint | None = None
    repair_fingerprint: str = ""
    repair_cycles: int = 0
    disclosure_fingerprints: set[str] = field(default_factory=set)
    last_error: str = ""


@dataclass(frozen=True)
class CompletionReviewState:
    """One atomic view of checkpoint-eligible working review state."""

    snapshot: DiffSnapshot | None
    checkpoint: ReviewCheckpoint | None
    settled: bool
    refresh_pending: bool


def _validate_lane(lane: RefreshLane) -> RefreshLane:
    if lane not in {"working", "view"}:
        raise ValueError(f"Unknown semantic refresh lane: {lane!r}")
    return lane


def _clean_hint(hint: object) -> str:
    raw = str(hint or "").strip()
    if (
        not raw
        or "\x00" in raw
        or any(ord(character) < 32 for character in raw)
    ):
        return ""
    return raw


def _scope_contains_candidate(
    scope: ProjectScope,
    candidate: Path,
) -> bool:
    try:
        resolved = candidate.resolve(strict=False)
        relative = resolved.relative_to(scope.project_root)
        resolved.relative_to(scope.watched_root)
    except (OSError, RuntimeError, ValueError):
        return False
    return relative.parts[:1] != (".a0proj",)


class ReviewRegistry:
    """Thread-safe in-memory state for project/context semantic review."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[ScopeKey, ProjectReviewState] = {}
        self._mcp_enabled_projects: set[str] = set()

    @staticmethod
    def _key(scope: ProjectScope) -> ScopeKey:
        return scope.project_id, scope.context_id

    def _state(self, scope: ProjectScope) -> ProjectReviewState:
        key = self._key(scope)
        state = self._states.get(key)
        if state is None:
            state = ProjectReviewState(scope=scope)
            self._states[key] = state
        else:
            state.scope = scope
        return state

    @staticmethod
    def _lane_values(
        state: ProjectReviewState,
        lane: RefreshLane,
    ) -> tuple[int, int, int]:
        if lane == "working":
            return (
                state.working_reserved_generation,
                state.working_generation,
                state.working_pending_generation,
            )
        return (
            state.view_reserved_generation,
            state.view_generation,
            state.view_pending_generation,
        )

    @staticmethod
    def _set_reserved(
        state: ProjectReviewState,
        lane: RefreshLane,
        generation: int,
    ) -> None:
        if lane == "working":
            state.working_reserved_generation = generation
        else:
            state.view_reserved_generation = generation

    @staticmethod
    def _set_published(
        state: ProjectReviewState,
        lane: RefreshLane,
        generation: int,
    ) -> None:
        if lane == "working":
            state.working_generation = generation
        else:
            state.view_generation = generation

    @staticmethod
    def _set_pending(
        state: ProjectReviewState,
        lane: RefreshLane,
        generation: int,
    ) -> None:
        if lane == "working":
            state.working_pending_generation = generation
        else:
            state.view_pending_generation = generation

    def register(self, scope: ProjectScope) -> None:
        with self._lock:
            self._state(scope)

    def has_scope(self, scope: ProjectScope) -> bool:
        with self._lock:
            return self._key(scope) in self._states

    def unregister_context(
        self,
        context_id: str,
    ) -> tuple[ProjectScope, ...]:
        """Remove all source-bearing review state owned by a context."""

        with self._lock:
            removed = tuple(
                state.scope
                for key, state in self._states.items()
                if key[1] == context_id
            )
            for scope in removed:
                self._states.pop(self._key(scope), None)
            remaining_projects = {
                state.scope.project_id for state in self._states.values()
            }
            for scope in removed:
                if scope.project_id not in remaining_projects:
                    self._mcp_enabled_projects.discard(scope.project_id)
            return removed

    def scopes_matching(
        self,
        path_hints: list[str],
    ) -> tuple[ProjectScope, ...]:
        if not path_hints:
            return ()
        with self._lock:
            scopes = tuple(
                state.scope for state in self._states.values()
            )
        matched: dict[ScopeKey, ProjectScope] = {}
        for hint in path_hints:
            raw = _clean_hint(hint)
            if not raw:
                continue
            candidate = Path(raw)
            if not candidate.is_absolute():
                for scope in scopes:
                    if _scope_contains_candidate(
                        scope,
                        scope.project_root / candidate,
                    ):
                        matched[self._key(scope)] = scope
                continue

            real_matches = tuple(
                scope
                for scope in scopes
                if _scope_contains_candidate(scope, candidate)
            )
            if real_matches:
                for scope in real_matches:
                    matched[self._key(scope)] = scope
                continue

            # Agent Zero's file browser reports project-rooted virtual paths
            # such as /src/app.py. Only try that interpretation when the
            # absolute path did not match any registered real project.
            for scope in scopes:
                virtual = scope.project_root / raw.lstrip("/\\")
                if _scope_contains_candidate(scope, virtual):
                    matched[self._key(scope)] = scope
        return tuple(matched[key] for key in sorted(matched))

    def reserve_generation(
        self,
        scope: ProjectScope,
        *,
        lane: RefreshLane | None = None,
    ) -> int:
        """Reserve a generation.

        ``lane=None`` preserves the original public API used by callers that
        choose the lane when publishing. Coordinator code always supplies an
        explicit lane so a view request cannot supersede completion state.
        """

        with self._lock:
            state = self._state(scope)
            state.next_generation += 1
            generation = state.next_generation
            if lane is not None:
                self._set_reserved(
                    state,
                    _validate_lane(lane),
                    generation,
                )
            return generation

    def mark_pending(
        self,
        scope: ProjectScope,
        generation: int,
        *,
        lane: RefreshLane = "working",
    ) -> None:
        selected_lane = _validate_lane(lane)
        with self._lock:
            state = self._state(scope)
            reserved, published, _pending = self._lane_values(
                state,
                selected_lane,
            )
            if not 0 < generation <= state.next_generation:
                return
            if reserved > published and generation != reserved:
                return
            if generation <= published:
                return
            self._set_reserved(state, selected_lane, generation)
            self._set_pending(state, selected_lane, generation)

    def clear_pending(
        self,
        scope: ProjectScope,
        generation: int,
        *,
        lane: RefreshLane = "working",
    ) -> None:
        selected_lane = _validate_lane(lane)
        with self._lock:
            state = self._state(scope)
            _reserved, _published, pending = self._lane_values(
                state,
                selected_lane,
            )
            if pending == generation:
                self._set_pending(state, selected_lane, 0)

    def settle_generation(
        self,
        scope: ProjectScope,
        generation: int,
        *,
        lane: RefreshLane = "working",
    ) -> None:
        """Settle a completed or abandoned reservation without superseding work."""

        selected_lane = _validate_lane(lane)
        with self._lock:
            state = self._states.get(self._key(scope))
            if state is None:
                return
            reserved, published, pending = self._lane_values(
                state,
                selected_lane,
            )
            if pending == generation:
                self._set_pending(state, selected_lane, 0)
            if reserved == generation and generation > published:
                self._set_reserved(state, selected_lane, published)

    def pending_generation(
        self,
        scope: ProjectScope,
        *,
        lane: RefreshLane = "working",
    ) -> int:
        selected_lane = _validate_lane(lane)
        with self._lock:
            state = self._states.get(self._key(scope))
            if state is None:
                return 0
            return self._lane_values(state, selected_lane)[2]

    def generation_is_settled(
        self,
        scope: ProjectScope,
        *,
        lane: RefreshLane = "working",
    ) -> bool:
        selected_lane = _validate_lane(lane)
        with self._lock:
            state = self._states.get(self._key(scope))
            if state is None:
                return False
            reserved, published, pending = self._lane_values(
                state,
                selected_lane,
            )
            return pending == 0 and reserved <= published

    def generation_is_current(
        self,
        scope: ProjectScope,
        generation: int,
        *,
        lane: RefreshLane = "working",
    ) -> bool:
        selected_lane = _validate_lane(lane)
        with self._lock:
            state = self._states.get(self._key(scope))
            if state is None:
                return False
            reserved, published, _pending = self._lane_values(
                state,
                selected_lane,
            )
            return generation == reserved and generation > published

    @staticmethod
    def _can_publish(
        state: ProjectReviewState,
        generation: int,
        lane: RefreshLane,
    ) -> bool:
        reserved, published, _pending = ReviewRegistry._lane_values(
            state,
            lane,
        )
        if not 0 < generation <= state.next_generation:
            return False
        if generation <= published:
            return False
        return reserved <= published or generation == reserved

    def _publish(
        self,
        scope: ProjectScope,
        snapshot: DiffSnapshot,
        generation: int,
        *,
        lane: RefreshLane,
    ) -> bool:
        with self._lock:
            state = self._states.get(self._key(scope))
            if state is None:
                return False
            if not self._can_publish(state, generation, lane):
                return False
            self._set_reserved(state, lane, generation)
            self._set_published(state, lane, generation)
            state.revision += 1
            published = replace(
                snapshot,
                revision=state.revision,
                stale=False,
                error="",
            )
            state.last_error = ""
            if lane == "working":
                previous = state.working_snapshot
                state.working_snapshot = published
                if (
                    state.view_snapshot is None
                    or state.view_snapshot.request.mode == "working"
                ):
                    state.view_snapshot = published
                if (
                    previous is None
                    or previous.fingerprint != published.fingerprint
                ):
                    state.checkpoint = None
                    state.repair_fingerprint = published.fingerprint
                    state.repair_cycles = 0
                    state.disclosure_fingerprints.clear()
            else:
                state.view_snapshot = published
            return True

    def publish_working(
        self,
        scope: ProjectScope,
        snapshot: DiffSnapshot,
        generation: int,
    ) -> bool:
        return self._publish(
            scope,
            snapshot,
            generation,
            lane="working",
        )

    def publish_view(
        self,
        scope: ProjectScope,
        snapshot: DiffSnapshot,
        generation: int,
    ) -> bool:
        return self._publish(
            scope,
            snapshot,
            generation,
            lane="view",
        )

    @staticmethod
    def _bounded_error(scope: ProjectScope, error: object) -> str:
        text = " ".join(str(error).split())
        for path in (scope.watched_root, scope.project_root):
            path_text = str(path)
            if path_text:
                text = text.replace(path_text, "<project>")
        return text[:500]

    def publish_error(
        self,
        scope: ProjectScope,
        generation: int,
        error: object,
        *,
        lane: RefreshLane = "working",
    ) -> bool:
        selected_lane = _validate_lane(lane)
        with self._lock:
            state = self._states.get(self._key(scope))
            if state is None:
                return False
            if not self._can_publish(state, generation, selected_lane):
                return False
            self._set_reserved(state, selected_lane, generation)
            self._set_published(state, selected_lane, generation)
            message = self._bounded_error(scope, error)
            state.last_error = message
            state.revision += 1
            if (
                selected_lane == "working"
                and state.working_snapshot is not None
            ):
                state.working_snapshot = replace(
                    state.working_snapshot,
                    revision=state.revision,
                    stale=True,
                    error=message,
                )
                if (
                    state.view_snapshot is not None
                    and state.view_snapshot.request.mode == "working"
                ):
                    state.view_snapshot = state.working_snapshot
            elif (
                selected_lane == "view"
                and state.view_snapshot is not None
            ):
                state.view_snapshot = replace(
                    state.view_snapshot,
                    revision=state.revision,
                    stale=True,
                    error=message,
                )
            return True

    def current_working(
        self,
        scope: ProjectScope,
    ) -> DiffSnapshot | None:
        with self._lock:
            state = self._states.get(self._key(scope))
            return state.working_snapshot if state is not None else None

    def current_view(
        self,
        scope: ProjectScope,
    ) -> DiffSnapshot | None:
        with self._lock:
            state = self._states.get(self._key(scope))
            return state.view_snapshot if state is not None else None

    def completion_review_state(
        self,
        scope: ProjectScope,
    ) -> CompletionReviewState:
        """Atomically revalidate the exact working snapshot and checkpoint."""

        with self._lock:
            state = self._states.get(self._key(scope))
            if state is None:
                return CompletionReviewState(None, None, False, False)
            refresh_pending = bool(
                state.working_pending_generation != 0
                or (
                    state.working_reserved_generation
                    > state.working_generation
                )
            )
            snapshot = state.working_snapshot
            settled = bool(
                snapshot is not None
                and not snapshot.stale
                and not snapshot.error
                and not refresh_pending
            )
            return CompletionReviewState(
                snapshot=snapshot if settled else None,
                checkpoint=state.checkpoint if settled else None,
                settled=settled,
                refresh_pending=refresh_pending,
            )

    def current_working_if_settled(
        self,
        scope: ProjectScope,
        fingerprint: str,
    ) -> DiffSnapshot | None:
        """Return the exact checkpoint-eligible working snapshot atomically."""

        with self._lock:
            state = self._states.get(self._key(scope))
            if state is None:
                return None
            snapshot = state.working_snapshot
            if (
                snapshot is None
                or snapshot.fingerprint != fingerprint
                or snapshot.stale
                or bool(snapshot.error)
                or state.working_pending_generation != 0
                or (
                    state.working_reserved_generation
                    > state.working_generation
                )
            ):
                return None
            return snapshot

    def record_checkpoint(
        self,
        scope: ProjectScope,
        checkpoint: ReviewCheckpoint,
    ) -> None:
        with self._lock:
            self._state(scope).checkpoint = checkpoint

    def record_checkpoint_if_current(
        self,
        scope: ProjectScope,
        checkpoint: ReviewCheckpoint,
    ) -> bool:
        """Atomically bind a checkpoint to the exact working snapshot."""

        with self._lock:
            state = self._state(scope)
            snapshot = state.working_snapshot
            if (
                snapshot is None
                or checkpoint.project_id != scope.project_id
                or checkpoint.fingerprint != snapshot.fingerprint
                or snapshot.stale
                or bool(snapshot.error)
                or state.working_pending_generation != 0
                or (
                    state.working_reserved_generation
                    > state.working_generation
                )
            ):
                return False
            structural_entities = tuple(
                sorted(
                    change.entity.entity_id
                    for change in snapshot.changes
                    if change.structural
                )
            )
            if checkpoint.structural_entities != structural_entities:
                return False
            state.checkpoint = checkpoint
            return True

    def record_cancelled_checkpoint_if_current(
        self,
        scope: ProjectScope,
        *,
        revision: int,
        fingerprint: str,
    ) -> bool:
        """Atomically cancel only the exact settled working snapshot."""

        with self._lock:
            state = self._states.get(self._key(scope))
            if state is None:
                return False
            snapshot = state.working_snapshot
            if (
                snapshot is None
                or snapshot.request.mode != "working"
                or snapshot.revision != revision
                or snapshot.fingerprint != fingerprint
                or snapshot.stale
                or bool(snapshot.error)
                or state.working_pending_generation != 0
                or (
                    state.working_reserved_generation
                    > state.working_generation
                )
            ):
                return False
            structural_entities = tuple(
                sorted(
                    change.entity.entity_id
                    for change in snapshot.changes
                    if change.structural
                )
            )
            state.checkpoint = ReviewCheckpoint(
                project_id=scope.project_id,
                fingerprint=snapshot.fingerprint,
                outcome="cancelled",
                structural_entities=structural_entities,
                findings=(),
            )
            return True

    def checkpoint_for(
        self,
        scope: ProjectScope,
    ) -> ReviewCheckpoint | None:
        with self._lock:
            return self._state(scope).checkpoint

    def checkpoint_is_current(
        self,
        scope: ProjectScope,
        fingerprint: str,
    ) -> bool:
        with self._lock:
            state = self._state(scope)
            checkpoint = state.checkpoint
            snapshot = state.working_snapshot
            return bool(
                checkpoint
                and snapshot
                and checkpoint.project_id == scope.project_id
                and checkpoint.fingerprint == fingerprint
                and snapshot.fingerprint == fingerprint
                and not snapshot.stale
                and not snapshot.error
                and state.working_pending_generation == 0
                and (
                    state.working_reserved_generation
                    <= state.working_generation
                )
            )

    def mcp_enabled(self, scope: ProjectScope) -> bool:
        with self._lock:
            return scope.project_id in self._mcp_enabled_projects

    def set_mcp_enabled(
        self,
        scope: ProjectScope,
        enabled: bool,
    ) -> None:
        with self._lock:
            if enabled:
                self._mcp_enabled_projects.add(scope.project_id)
            else:
                self._mcp_enabled_projects.discard(scope.project_id)

    def increment_repair_cycle(
        self,
        scope: ProjectScope,
        fingerprint: str,
    ) -> int:
        with self._lock:
            state = self._state(scope)
            if state.repair_fingerprint != fingerprint:
                state.repair_fingerprint = fingerprint
                state.repair_cycles = 0
                state.disclosure_fingerprints.clear()
            state.repair_cycles += 1
            return state.repair_cycles

    def repair_cycle(
        self,
        scope: ProjectScope,
        fingerprint: str,
    ) -> int:
        with self._lock:
            state = self._state(scope)
            if state.repair_fingerprint != fingerprint:
                return 0
            return state.repair_cycles

    def claim_disclosure(
        self,
        scope: ProjectScope,
        fingerprint: str,
    ) -> bool:
        with self._lock:
            state = self._state(scope)
            if fingerprint in state.disclosure_fingerprints:
                return False
            state.disclosure_fingerprints.add(fingerprint)
            return True

    @staticmethod
    def _snapshot_public(
        snapshot: DiffSnapshot | None,
    ) -> dict[str, object] | None:
        if snapshot is None:
            return None
        return {
            "request": asdict(snapshot.request),
            "fingerprint": snapshot.fingerprint,
            "revision": snapshot.revision,
            "summary": asdict(snapshot.summary),
            "changes": [
                change.to_card() for change in snapshot.changes
            ],
            "sem_version": snapshot.sem_version,
            "completed_at": snapshot.completed_at,
            "stale": snapshot.stale,
            "error": snapshot.error,
        }

    def public_status(
        self,
        scope: ProjectScope,
    ) -> dict[str, object]:
        with self._lock:
            state = self._state(scope)
            checkpoint = state.checkpoint
            view = state.view_snapshot or state.working_snapshot
            fingerprint = (
                state.working_snapshot.fingerprint
                if state.working_snapshot
                else ""
            )
            return {
                "context_id": scope.context_id,
                "project_id": scope.project_id,
                "watched_relative": scope.watched_relative,
                "revision": state.revision,
                "pending_generation": (
                    state.working_pending_generation
                ),
                "working": self._snapshot_public(
                    state.working_snapshot
                ),
                "snapshot": self._snapshot_public(view),
                "checkpoint": (
                    {
                        "fingerprint": checkpoint.fingerprint,
                        "outcome": checkpoint.outcome,
                        "structural_entities": list(
                            checkpoint.structural_entities
                        ),
                        "findings": list(checkpoint.findings),
                    }
                    if checkpoint
                    else None
                ),
                "repair_cycle": (
                    state.repair_cycles
                    if state.repair_fingerprint == fingerprint
                    else 0
                ),
                "mcp_enabled": (
                    scope.project_id in self._mcp_enabled_projects
                ),
                "error": state.last_error,
            }
