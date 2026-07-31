from __future__ import annotations

from helpers import ws_manager
from usr.plugins.sem_review_loop.helpers.project_scope import ProjectScope


async def emit_revision(
    scope: ProjectScope,
    revision: int,
) -> None:
    """Emit only the opaque revision identity used by the browser."""

    await ws_manager.send_data(
        "sem_review_revision",
        {
            "context_id": scope.context_id,
            "project_id": scope.project_id,
            "revision": int(revision),
        },
        endpoint_name="/ws",
    )
