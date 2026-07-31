from __future__ import annotations

from typing import Any

from helpers.extension import Extension
from helpers.tool import Response
from usr.plugins.sem_review_loop.helpers.completion_gate import (
    PREPARED_KEY,
    PreparedCompletion,
    enforce_completion,
    prepare_completion,
    revalidate_completion,
)


class SemReviewCompletionGate(Extension):
    async def execute(
        self,
        tool_name: str = "",
        response: Response | None = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        if tool_name != "response" or self.agent is None:
            return
        data = getattr(self.agent, "data", None)
        prepared = (
            data.pop(PREPARED_KEY, None)
            if isinstance(data, dict)
            else None
        )
        if response is None:
            return
        if not isinstance(prepared, PreparedCompletion):
            prepared = await prepare_completion(self.agent)
        if response.break_loop is True:
            prepared = await revalidate_completion(prepared)
        enforce_completion(self.agent, response, prepared)
