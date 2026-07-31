from __future__ import annotations

from typing import Any

from helpers.api import ApiHandler, Request, Response
from usr.plugins.sem_review_loop.helpers.api_support import (
    APIConflictError,
    APIInputError,
    agent_scope_config,
    bad_request,
    json_response,
    required_text,
)
from usr.plugins.sem_review_loop.helpers.lessons import LessonError, LessonStaleError
from usr.plugins.sem_review_loop.helpers.services import get_lesson_store, get_registry


def _reviewed_fingerprint(scope: object) -> str:
    state = get_registry().completion_review_state(scope)  # type: ignore[arg-type]
    if (
        not state.settled
        or state.snapshot is None
        or state.checkpoint is None
        or state.checkpoint.outcome not in {"pass", "repaired"}
        or state.checkpoint.fingerprint != state.snapshot.fingerprint
    ):
        raise APIConflictError(
            "A current resolved semantic checkpoint is required. "
            "Run the working-tree review and record pass or repaired for "
            "this exact fingerprint before approving the lesson."
        )
    return state.snapshot.fingerprint


class SemLessons(ApiHandler):
    async def process(
        self,
        input: dict[str, Any],
        request: Request,
    ) -> dict[str, object] | Response:
        del request
        try:
            _agent, scope, _config = agent_scope_config(self, input)
            action_value = input.get("action", "list")
            if not isinstance(action_value, str):
                raise APIInputError("action must be a string")
            action = action_value.strip().lower()
            store = get_lesson_store()
            registry = get_registry()
            current = registry.current_working(scope)
            fingerprint = current.fingerprint if current is not None else ""
            if action == "list":
                return json_response(
                    {"ok": True, "lessons": store.list(scope, current_fingerprint=fingerprint)}
                )
            if action == "forget_all":
                return json_response({"ok": True, "forgotten": store.forget_all(scope)})
            proposal_id = required_text(input, "proposal_id", maximum=80)
            if action == "approve":
                reviewed = _reviewed_fingerprint(scope)
                lesson = store.approve(
                    scope,
                    proposal_id,
                    current_fingerprint=reviewed,
                )
                return json_response({"ok": True, "lesson": lesson.__dict__})
            if action == "discard":
                return json_response(
                    {"ok": True, "discarded": store.discard(scope, proposal_id)}
                )
            if action == "delete":
                return json_response(
                    {"ok": True, "deleted": store.delete(scope, proposal_id)}
                )
            if action == "propose":
                if current is None:
                    raise APIConflictError("A current semantic snapshot is required")
                reviewed = _reviewed_fingerprint(scope)
                if reviewed != current.fingerprint:
                    raise APIConflictError("Semantic state changed; refresh and retry")
                problem = required_text(input, "problem", maximum=2048)
                resolution = required_text(input, "resolution", maximum=2048)
                proposal = store.propose_from_checkpoint(
                    scope,
                    current,
                    problem=problem,
                    resolution=resolution,
                    verification_summary=input.get("verification_summary", ""),
                    verification_result=input.get("verification_result", ""),
                    applicability_tags=input.get("applicability_tags", ()),
                    impact_relations=input.get("impact_relations", ()),
                )
                if proposal is None:
                    raise APIConflictError("Current semantic snapshot has no lesson evidence")
                return json_response({"ok": True, "lesson": proposal.__dict__})
            raise APIInputError("unknown lesson action")
        except APIConflictError as exc:
            return bad_request(exc, status=409)
        except LessonStaleError as exc:
            return bad_request(exc, status=409)
        except (APIInputError, LessonError, ValueError) as exc:
            return bad_request(exc)
