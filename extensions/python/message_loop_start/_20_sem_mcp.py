from __future__ import annotations

import logging
from helpers.extension import Extension
from usr.plugins.sem_review_loop.helpers.config import config_for_agent
from usr.plugins.sem_review_loop.helpers.project_scope import (
    ProjectScopeError, scope_for_agent,
)
from usr.plugins.sem_review_loop.helpers.services import get_mcp_manager


class SemReviewMCP(Extension):
    async def execute(self, **kwargs) -> None:
        if self.agent is None:
            return
        try:
            config = config_for_agent(self.agent)
            scope = scope_for_agent(self.agent, config.watched_subdirectory)
        except ProjectScopeError:
            return  # Chats without a project have no project MCP configuration.
        try:
            await get_mcp_manager().ensure_enabled(scope, config)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Semantic tools could not connect: %s", type(exc).__name__,
            )
