from __future__ import annotations

from typing import Any

from helpers.api import ApiHandler, Request, Response
from usr.plugins.sem_review_loop.helpers.api_support import (
    APIInputError,
    agent_scope_config,
    bad_request,
    json_response,
)
from usr.plugins.sem_review_loop.helpers.mcp_manager import (
    MCPConflictError,
    MCPManagerError,
    MCPStalePreviewError,
)


class SemMcp(ApiHandler):
    async def process(
        self,
        input: dict[str, Any],
        request: Request,
    ) -> dict[str, object] | Response:
        del request
        try:
            _agent, scope, config = agent_scope_config(self, input)
            action_value = input.get("action", "status")
            if not isinstance(action_value, str):
                raise APIInputError("action must be a string")
            action = action_value.strip().lower()

            from usr.plugins.sem_review_loop.helpers.services import (
                get_mcp_manager,
            )

            manager = get_mcp_manager()
            if action == "status":
                return json_response({"ok": True, **manager.status(scope)})
            if action == "readiness":
                return json_response({"ok": True, **await manager.readiness(scope)})
            if action == "preview":
                return json_response({
                    "ok": True,
                    **manager.preview(scope, config),
                })
            if action == "enable":
                return json_response({
                    "ok": True,
                    **await manager.enable(
                        scope,
                        config,
                        confirmed=input.get("confirmed"),
                        preview_token=input.get("preview_token"),
                    ),
                })
            if action == "disable":
                return json_response({"ok": True, **await manager.disable(scope)})
            return bad_request(f"Unknown MCP action: {action}")
        except APIInputError as exc:
            return bad_request(exc)
        except MCPStalePreviewError as exc:
            return bad_request(exc, status=409)
        except MCPConflictError as exc:
            return bad_request(exc, status=409)
        except MCPManagerError as exc:
            return bad_request(exc)
