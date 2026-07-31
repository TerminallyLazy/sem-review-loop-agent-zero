from __future__ import annotations

from typing import Any

from helpers.extension import Extension
from usr.plugins.sem_review_loop.helpers.completion_gate import (
    PREPARED_KEY,
    prepare_completion,
)


class SemReviewPrepareCompletion(Extension):
    async def execute(
        self,
        tool_name: str = "",
        tool_args: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del tool_args, kwargs
        if tool_name != "response" or self.agent is None:
            return
        data = getattr(self.agent, "data", None)
        if not isinstance(data, dict):
            data = {}
            setattr(self.agent, "data", data)
        data.pop(PREPARED_KEY, None)
        prepared = await prepare_completion(self.agent)
        data[PREPARED_KEY] = prepared
