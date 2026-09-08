from __future__ import annotations

from typing import Any

from helpers.api import ApiHandler, Request, Response
from usr.plugins.sem_review_loop.helpers import installer
from usr.plugins.sem_review_loop.helpers.api_support import (
    APIInputError,
    agent_scope_config,
    bad_request,
    json_response,
)
from usr.plugins.sem_review_loop.helpers.services import (
    get_coordinator,
    get_mcp_manager,
    get_registry,
)


def _binary_status(custom_binary: str) -> dict[str, object]:
    try:
        if custom_binary:
            version = installer.validate_binary(custom_binary)
            return {"available": True, "managed": False, "version": version}
        path = installer.installed_binary_path()
        if not path.is_file() or path.is_symlink():
            return {
                "available": False,
                "managed": True,
                "version": installer.SEM_VERSION,
                "error": "Managed sem binary is not installed.",
            }
        version = installer.validate_binary(path)
        return {"available": True, "managed": True, "version": version}
    except Exception:
        return {
            "available": False,
            "managed": not bool(custom_binary),
            "version": installer.SEM_VERSION,
            "error": "Pinned sem binary is unavailable.",
        }


class SemStatus(ApiHandler):
    async def process(
        self,
        input: dict[str, Any],
        request: Request,
    ) -> dict[str, object] | Response:
        del request
        try:
            _agent, scope, config = agent_scope_config(self, input)
            registry = get_registry()
            manager = get_mcp_manager()
            try:
                mcp = await manager.ensure_enabled(scope, config)
            except Exception:
                mcp = {
                    "configured": False,
                    "armed": False,
                    "enabled": False,
                    "drifted": False,
                    "conflict": False,
                    "tools": [],
                    "error": "Project MCP status is unavailable.",
                }
            review = registry.public_status(scope)
            if config.automatic_refresh:
                get_coordinator().poll(scope)
            return json_response(
                {
                    "ok": True,
                    "review": review,
                    "sem": _binary_status(config.custom_sem_binary),
                    "mcp": mcp,
                    "policy": {
                        "local_only": True,
                        "telemetry_disabled": True,
                    },
                }
            )
        except APIInputError as exc:
            return bad_request(exc)
