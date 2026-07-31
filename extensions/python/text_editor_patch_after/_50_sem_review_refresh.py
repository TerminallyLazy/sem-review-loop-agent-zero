from __future__ import annotations

import logging
from typing import Any

from helpers.extension import Extension
from usr.plugins.sem_review_loop.helpers import services


logger = logging.getLogger(__name__)


class SemReviewTextPatchRefresh(Extension):
    async def execute(
        self,
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        if not self.agent:
            return
        try:
            path = str((data or {}).get("path") or "")
            services.get_coordinator().schedule_for_agent(
                self.agent,
                trigger="text_editor_patch",
                path_hints=[path] if path else [],
            )
        except Exception as exc:
            detail = " ".join(str(exc).split())[:300] or "unknown error"
            logger.warning(
                "Semantic Review patch refresh scheduling failed: %s",
                detail,
            )
