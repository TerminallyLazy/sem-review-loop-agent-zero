from __future__ import annotations

import json
import re
from typing import Any

from helpers.api import Response
from usr.plugins.sem_review_loop.helpers.config import (
    PLUGIN_NAME,
    PluginConfig,
    config_for_agent,
)
from usr.plugins.sem_review_loop.helpers.project_scope import (
    ProjectScope,
    ProjectScopeError,
    scope_for_agent,
)
from usr.plugins.sem_review_loop.helpers.sem_types import EntityRef


MAX_CONTEXT_ID_CHARS = 256
MAX_ENTITY_ID_CHARS = 2_048
MAX_FINGERPRINT_CHARS = 256
MAX_REF_CHARS = 200
MAX_ACTION_CHARS = 32


class APIInputError(ValueError):
    """A bounded client error suitable for a 4xx response."""


class APIConflictError(APIInputError):
    """The authenticated project view changed while a request was bound."""


def _bounded(value: object, limit: int = 300) -> str:
    text = " ".join(str(value).split())
    text = re.sub(r"(?i)\b(?:token|secret|password|api[_ -]?key|credential)\s*[:=]\s*\S+", "credential=[redacted]", text)
    text = re.sub(r"\b(?:/Users|/home|/private|/tmp)/[^\s,;]+", "<local path>", text)
    return text[:limit]


def json_response(payload: object, *, status: int = 200) -> Response:
    return Response(
        response=json.dumps(payload, ensure_ascii=False),
        status=status,
        mimetype="application/json",
        headers={"Cache-Control": "no-store"},
    )


def required_text(
    input_data: dict[str, Any],
    field: str,
    *,
    maximum: int,
    empty: bool = False,
) -> str:
    value = input_data.get(field)
    if not isinstance(value, str):
        raise APIInputError(f"{field} must be a string")
    normalized = value.strip()
    if (not normalized and not empty) or len(normalized) > maximum:
        raise APIInputError(f"{field} is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise APIInputError(f"{field} is invalid")
    return normalized


def optional_text(
    input_data: dict[str, Any],
    field: str,
    *,
    maximum: int,
) -> str:
    if field not in input_data or input_data[field] in (None, ""):
        return ""
    return required_text(input_data, field, maximum=maximum, empty=True)


def nonnegative_int(input_data: dict[str, Any], field: str) -> int:
    value = input_data.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise APIInputError(f"{field} must be a non-negative integer")
    return value


def current_entity(
    scope: ProjectScope,
    input_data: dict[str, Any],
    registry: object,
) -> tuple[EntityRef, int, str]:
    entity_id = required_text(
        input_data,
        "entity_id",
        maximum=MAX_ENTITY_ID_CHARS,
    )
    revision = nonnegative_int(input_data, "revision")
    fingerprint = required_text(
        input_data,
        "fingerprint",
        maximum=MAX_FINGERPRINT_CHARS,
    )
    current_view = registry.current_view(scope)  # type: ignore[attr-defined]
    if (
        current_view is None
        or current_view.request.mode == "stdin"
        or current_view.revision != revision
        or current_view.fingerprint != fingerprint
    ):
        raise APIConflictError("Semantic view changed; refresh and retry")
    for change in current_view.changes:
        if change.entity.entity_id == entity_id:
            return change.entity, revision, fingerprint
    raise APIConflictError("Selected semantic entity is no longer current")


def agent_scope_config(
    handler: object,
    input_data: dict[str, Any],
) -> tuple[object, ProjectScope, PluginConfig]:
    if not isinstance(input_data, dict):
        raise APIInputError("request body must be an object")
    raw_context_id = input_data.get("context_id")
    if not isinstance(raw_context_id, str):
        raise APIInputError("context_id must be a string")
    context_id = raw_context_id.strip()
    if not context_id:
        raise APIInputError("context_id is required")
    if len(context_id) > MAX_CONTEXT_ID_CHARS:
        raise APIInputError("context_id exceeds the allowed length")

    use_context = getattr(handler, "use_context", None)
    if not callable(use_context):
        raise APIInputError("Agent Zero context lookup is unavailable")
    try:
        context = use_context(context_id, create_if_not_exists=False)
    except Exception as exc:
        raise APIInputError("Unknown Agent Zero context") from exc
    if context is None:
        raise APIInputError("Unknown Agent Zero context")
    agent = getattr(context, "agent0", None)
    if agent is None:
        raise APIInputError("Agent Zero context has no active root agent")

    from helpers.plugins import get_enabled_plugins

    try:
        enabled_plugins = get_enabled_plugins(agent)
    except Exception as exc:
        raise APIInputError("Unable to verify plugin activation") from exc
    if PLUGIN_NAME not in enabled_plugins:
        raise APIInputError(
            "Semantic Review Loop is not enabled for this project agent"
        )

    try:
        config = config_for_agent(agent)
        scope = scope_for_agent(agent, config.watched_subdirectory)
    except (ProjectScopeError, ValueError) as exc:
        raise APIInputError(_bounded(exc)) from exc

    from usr.plugins.sem_review_loop.helpers.services import get_coordinator

    get_coordinator().register_scope(scope, config)
    return agent, scope, config


def agent_and_scope(
    handler: object,
    input_data: dict[str, Any],
) -> tuple[object, ProjectScope]:
    agent, scope, _config = agent_scope_config(handler, input_data)
    return agent, scope


def bad_request(message: object, *, status: int = 400) -> Response:
    return Response(
        status=status,
        response=_bounded(message),
        mimetype="text/plain",
        headers={"Cache-Control": "no-store"},
    )
