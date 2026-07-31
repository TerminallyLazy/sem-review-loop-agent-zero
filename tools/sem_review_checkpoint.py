from __future__ import annotations

from typing import Any

from helpers.tool import Response, Tool
from usr.plugins.sem_review_loop.helpers.config import config_for_agent
from usr.plugins.sem_review_loop.helpers.project_scope import scope_for_agent
from usr.plugins.sem_review_loop.helpers.registry import ReviewCheckpoint
from usr.plugins.sem_review_loop.helpers.sanitization import (
    SanitizationError,
    sanitize_plain_line,
)
from usr.plugins.sem_review_loop.helpers.services import (
    get_lesson_store,
    get_registry,
)


VALID_OUTCOMES = frozenset({"pass", "repaired", "unresolved", "cancelled"})
MAX_STRUCTURAL_ENTITIES = 200
MAX_FINDINGS = 20
MAX_FINDING_BYTES = 500


def _rejected(message: str) -> Response:
    return Response(
        message=f"Checkpoint rejected: {message}",
        break_loop=False,
    )


class SemReviewCheckpoint(Tool):
    async def execute(
        self,
        fingerprint: str = "",
        outcome: str = "",
        structural_entities: list[str] | None = None,
        findings: list[str] | None = None,
        **kwargs: Any,
    ) -> Response:
        del kwargs
        try:
            config = config_for_agent(self.agent)
            scope = scope_for_agent(
                self.agent,
                config.watched_subdirectory,
            )
            registry = get_registry()
            snapshot = registry.current_working(scope)
        except Exception:
            return _rejected(
                "project-scoped Semantic Review state is unavailable."
            )
        if (
            snapshot is None
            or not isinstance(fingerprint, str)
            or fingerprint != snapshot.fingerprint
        ):
            return _rejected("use the exact current fingerprint.")
        if snapshot.stale or bool(snapshot.error):
            return _rejected(
                "use a fresh current fingerprint after semantic refresh."
            )

        if not isinstance(outcome, str) or outcome not in VALID_OUTCOMES:
            return _rejected("invalid outcome.")

        actual_entities = {
            change.entity.entity_id
            for change in snapshot.changes
            if change.structural
        }
        supplied = [] if structural_entities is None else structural_entities
        if (
            not isinstance(supplied, list)
            or len(supplied) > MAX_STRUCTURAL_ENTITIES
            or len(actual_entities) > MAX_STRUCTURAL_ENTITIES
            or any(not isinstance(value, str) for value in supplied)
            or len(set(supplied)) != len(supplied)
            or set(supplied) != actual_entities
        ):
            return _rejected(
                "include all structural entities exactly once within the "
                f"{MAX_STRUCTURAL_ENTITIES}-entity bound."
            )

        raw_findings = [] if findings is None else findings
        if not isinstance(raw_findings, list) or len(raw_findings) > MAX_FINDINGS:
            return _rejected(f"provide at most {MAX_FINDINGS} findings.")
        try:
            normalized_findings = tuple(
                sanitize_plain_line(value, maximum=MAX_FINDING_BYTES)
                for value in raw_findings
            )
        except SanitizationError as exc:
            return _rejected(f"unsafe finding ({exc}).")

        checkpoint = ReviewCheckpoint(
            project_id=scope.project_id,
            fingerprint=snapshot.fingerprint,
            outcome=outcome,
            structural_entities=tuple(sorted(actual_entities)),
            findings=normalized_findings,
        )
        try:
            recorded = registry.record_checkpoint_if_current(
                scope,
                checkpoint,
            )
        except Exception:
            return _rejected(
                "project-scoped Semantic Review state is unavailable."
            )
        if not recorded:
            return _rejected(
                "semantic state changed during checkpoint; refresh and retry."
            )
        proposal_note = ""
        if outcome in {"pass", "repaired"}:
            try:
                proposal = get_lesson_store().propose_from_checkpoint(
                    scope,
                    snapshot,
                    problem=(
                        normalized_findings[0]
                        if normalized_findings
                        else "Structural semantic review completed."
                    ),
                    resolution=(
                        f"Checkpoint recorded with outcome {outcome}."
                    ),
                )
                if proposal is not None:
                    proposal_note = (
                        " A project lesson proposal is pending user approval."
                    )
            except Exception:
                # A local lesson-store failure must not turn a valid checkpoint
                # into a raw-error or completion blocker.
                proposal_note = " Lesson proposal was not saved."
        return Response(
            message=(
                "Semantic review checkpoint recorded for "
                f"{snapshot.fingerprint[:12]} with outcome "
                f"{outcome}.{proposal_note}"
            ),
            break_loop=False,
        )
