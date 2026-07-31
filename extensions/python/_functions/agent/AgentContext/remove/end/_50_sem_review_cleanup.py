from __future__ import annotations

import logging
from typing import Any

from helpers.extension import Extension
from usr.plugins.sem_review_loop.helpers import services


logger = logging.getLogger(__name__)


class SemReviewContextCleanup(Extension):
    def execute(
        self,
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        payload = data or {}
        args = payload.get("args")
        context_id = (
            str(args[0] or "")
            if isinstance(args, tuple) and args
            else ""
        )
        if not context_id:
            removed = payload.get("result")
            context_id = str(getattr(removed, "id", "") or "")
        if not context_id:
            return
        try:
            services.get_coordinator().unregister_context(context_id)
        except Exception as exc:
            detail = " ".join(str(exc).split())[:300] or "unknown error"
            logger.warning(
                "Semantic Review context cleanup failed: %s",
                detail,
            )
