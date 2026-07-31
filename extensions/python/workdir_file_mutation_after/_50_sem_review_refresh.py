from __future__ import annotations

import logging
from typing import Any

from helpers.extension import Extension
from usr.plugins.sem_review_loop.helpers import services


logger = logging.getLogger(__name__)


class SemReviewWorkdirMutationRefresh(Extension):
    async def execute(
        self,
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        try:
            payload = data or {}
            paths = payload.get("paths")
            if not isinstance(paths, list):
                paths = [
                    payload.get("path")
                    or payload.get("current_path")
                    or payload.get("parent_path")
                ]
            services.get_coordinator().schedule_for_path_hints(
                trigger=(
                    f"file_browser_{payload.get('action') or 'mutation'}"
                ),
                path_hints=[str(path) for path in paths if path],
            )
        except Exception as exc:
            detail = " ".join(str(exc).split())[:300] or "unknown error"
            logger.warning(
                "Semantic Review workdir refresh scheduling failed: %s",
                detail,
            )
