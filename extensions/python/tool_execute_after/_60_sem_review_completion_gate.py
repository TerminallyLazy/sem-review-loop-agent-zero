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
        original_message = response.message
        enforce_completion(self.agent, response, prepared)
        if response.message != original_message:
            # The response is streamed before this hook. Keep the visible final
            # answer aligned with the disclosed result returned to the caller.
            loop_data = getattr(self.agent, "loop_data", None)
            temporary = getattr(loop_data, "params_temporary", {})
            log_item = temporary.get("log_item_response") if isinstance(temporary, dict) else None
            if log_item is not None:
                log_item.update(content=response.message)
