from __future__ import annotations

import logging
from typing import Any

from helpers.extension import Extension
from usr.plugins.sem_review_loop.helpers import services


logger = logging.getLogger(__name__)


class SemReviewCodeExecutionRefresh(Extension):
    async def execute(
        self,
        tool_name: str = "",
        response: Any = None,
        **kwargs: Any,
    ) -> None:
        del response, kwargs
        if tool_name != "code_execution_tool" or not self.agent:
            return
        try:
            tool = getattr(
                getattr(self.agent, "loop_data", None),
                "current_tool",
                None,
            )
            args = getattr(tool, "args", {}) if tool else {}
            runtime = (
                str(args.get("runtime") or "")
                if isinstance(args, dict)
                else ""
            )
            if runtime == "output":
                return
            services.get_coordinator().schedule_for_agent(
                self.agent,
                trigger="code_execution",
                path_hints=[],
            )
        except Exception as exc:
            detail = " ".join(str(exc).split())[:300] or "unknown error"
            logger.warning(
                "Semantic Review code refresh scheduling failed: %s",
                detail,
            )
