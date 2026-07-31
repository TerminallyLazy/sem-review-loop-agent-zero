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
from usr.plugins.sem_review_loop.helpers.services import get_registry


MAX_DETAIL_BYTES = 256 * 1024


class SemDetail(ApiHandler):
    async def process(
        self,
        input: dict[str, Any],
        request: Request,
    ) -> dict[str, object] | Response:
        del request
        try:
            _agent, scope, _config = agent_scope_config(self, input)
            registry = get_registry()
            entity, revision, fingerprint = current_entity(scope, input, registry)
            view = registry.current_view(scope)
            if view is None:
                raise APIConflictError("Semantic view changed; refresh and retry")
            detail = view.details.get(entity.entity_id)
            if detail is None:
                raise APIConflictError("Selected semantic detail is unavailable")
            if (
                len(detail.before_content.encode("utf-8"))
                + len(detail.after_content.encode("utf-8"))
                > MAX_DETAIL_BYTES
            ):
                return bad_request(
                    "Selected semantic detail exceeds the display limit",
                    status=413,
                )
            latest = registry.current_view(scope)
            if (
                latest is None
                or latest.revision != revision
                or latest.fingerprint != fingerprint
            ):
                raise APIConflictError("Semantic view changed; refresh and retry")
            return json_response(
                {
                    "ok": True,
                    "detail": {
                        "entity": {
                            "entity_id": entity.entity_id,
                            "entity_name": entity.entity_name,
                            "entity_type": entity.entity_type,
                            "file_path": entity.file_path,
                        },
                        "change_type": next(
                            change.change_type
                            for change in view.changes
                            if change.entity.entity_id == entity.entity_id
                        ),
                        "before_content": detail.before_content,
                        "after_content": detail.after_content,
                        "fingerprint": fingerprint,
                        "revision": revision,
                    },
                }
            )
        except APIConflictError as exc:
            return bad_request(exc, status=409)
        except APIInputError as exc:
            return bad_request(exc)
