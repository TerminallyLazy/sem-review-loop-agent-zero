from __future__ import annotations

from typing import Any

from helpers.api import ApiHandler, Request, Response
from usr.plugins.sem_review_loop.helpers.api_support import (
    APIInputError,
    agent_scope_config,
    bad_request,
    json_response,
    required_text,
)
from usr.plugins.sem_review_loop.helpers.coordinator import (
    RefreshRaceError,
    RefreshTimeoutError,
)
from usr.plugins.sem_review_loop.helpers.sem_runner import SemCommandError
from usr.plugins.sem_review_loop.helpers.sem_types import DiffRequest
from usr.plugins.sem_review_loop.helpers.services import get_coordinator, get_registry


MODES = frozenset({"working", "staged", "commit", "range", "stdin"})


class SemDiff(ApiHandler):
    async def process(
        self,
        input: dict[str, Any],
        request: Request,
    ) -> dict[str, object] | Response:
        del request
        try:
            _agent, scope, _config = agent_scope_config(self, input)
            mode_value = input.get("mode", "working")
            if not isinstance(mode_value, str):
                raise APIInputError("mode must be a string")
            mode = mode_value.strip().lower()
            if mode not in MODES:
                raise APIInputError("unsupported semantic diff mode")
            coordinator = get_coordinator()
            if mode == "stdin":
                snapshot = await coordinator.refresh_manual(scope, input.get("files"))
            else:
                commit = ""
                from_ref = ""
                to_ref = ""
                if mode == "commit":
                    commit = required_text(input, "commit", maximum=200)
                elif mode == "range":
                    from_ref = required_text(input, "from_ref", maximum=200)
                    to_ref = required_text(input, "to_ref", maximum=200)
                snapshot = await coordinator.refresh_now(
                    scope,
                    DiffRequest(
                        mode, commit=commit, from_ref=from_ref, to_ref=to_ref
                    ),
                )
            public = get_registry().public_status(scope).get("snapshot")
            if not isinstance(public, dict):
                raise RefreshRaceError("Semantic diff result is unavailable")
            return json_response({"ok": True, "snapshot": public})
        except RefreshTimeoutError as exc:
            return bad_request(exc, status=409)
        except RefreshRaceError as exc:
            return bad_request(exc, status=409)
        except (APIInputError, SemCommandError, ValueError) as exc:
            return bad_request(exc)
