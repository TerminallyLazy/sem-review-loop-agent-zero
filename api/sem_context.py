from __future__ import annotations

from typing import Any

from helpers.api import ApiHandler, Request, Response
from usr.plugins.sem_review_loop.helpers.api_support import (
    APIConflictError,
    APIInputError,
    agent_scope_config,
    bad_request,
    current_entity,
    json_response,
)
from usr.plugins.sem_review_loop.helpers.coordinator import RefreshRaceError
from usr.plugins.sem_review_loop.helpers.sem_runner import SemCommandError
from usr.plugins.sem_review_loop.helpers.services import get_coordinator, get_registry


class SemContext(ApiHandler):
    async def process(
        self,
        input: dict[str, Any],
        request: Request,
    ) -> dict[str, object] | Response:
        del request
        try:
            _agent, scope, config = agent_scope_config(self, input)
            entity, revision, fingerprint = current_entity(
                scope, input, get_registry()
            )
            context = await get_coordinator().query_context(
                scope,
                entity,
                revision=revision,
                fingerprint=fingerprint,
                token_budget=config.context_token_budget,
            )
            return json_response(
                {
                    "ok": True,
                    "context": context,
                    "revision": revision,
                    "fingerprint": fingerprint,
                }
            )
        except (APIConflictError, RefreshRaceError) as exc:
            return bad_request(exc, status=409)
        except (APIInputError, SemCommandError, ValueError) as exc:
            return bad_request(exc)
