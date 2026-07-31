from __future__ import annotations

from helpers.api import ApiHandler, Request, Response
from usr.plugins.sem_review_loop.helpers.api_support import (
    APIInputError,
    agent_and_scope,
    bad_request,
)
from usr.plugins.sem_review_loop.helpers.services import get_registry


MAX_ACTION_CHARS = 20
MAX_FINGERPRINT_CHARS = 256


class SemReview(ApiHandler):
    async def process(
        self,
        input: dict,
        request: Request,
    ) -> dict | Response:
        del request
        try:
            _agent, scope = agent_and_scope(self, input)
            action_value = input.get("action", "status")
            if not isinstance(action_value, str):
                raise APIInputError("action must be a string")
            action = action_value.strip().lower()
            if not action or len(action) > MAX_ACTION_CHARS:
                raise APIInputError("action is invalid")

            registry = get_registry()
            if action == "status":
                return {
                    "ok": True,
                    "review": registry.public_status(scope),
                }
            if action != "cancel":
                return bad_request(f"Unknown review action: {action}")

            revision = input.get("revision")
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 0
            ):
                raise APIInputError(
                    "revision must be a non-negative integer"
                )
            fingerprint = input.get("fingerprint")
            if (
                not isinstance(fingerprint, str)
                or not fingerprint
                or len(fingerprint) > MAX_FINGERPRINT_CHARS
            ):
                raise APIInputError("fingerprint is invalid")

            if not registry.record_cancelled_checkpoint_if_current(
                scope,
                revision=revision,
                fingerprint=fingerprint,
            ):
                return bad_request(
                    "Semantic working snapshot changed; refresh first.",
                    status=409,
                )
            return {
                "ok": True,
                "review": registry.public_status(scope),
            }
        except (APIInputError, ValueError) as exc:
            return bad_request(exc)
