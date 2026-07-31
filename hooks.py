from __future__ import annotations

from typing import Any

from helpers.print_style import PrintStyle
from usr.plugins.sem_review_loop.helpers.installer import (
    UnsupportedPlatformError,
    ensure_installed,
    remove_plugin_data,
)


def maintain(**kwargs: Any) -> bool:
    """Validate or repair the pinned local sem binary during lifecycle work."""

    del kwargs
    try:
        ensure_installed()
    except UnsupportedPlatformError as exc:
        PrintStyle.warning(
            "sem_review_loop installed without a managed sem binary on this "
            "platform. Configure an absolute custom sem 0.21.0 binary, then "
            f"use the plugin or first semantic review to validate it: {exc}"
        )
    return True


def install(**kwargs: Any) -> bool:
    return maintain(**kwargs)


def pre_update(**kwargs: Any) -> bool:
    return maintain(**kwargs)


async def uninstall(**kwargs: Any) -> bool:
    del kwargs
    try:
        from usr.plugins.sem_review_loop.helpers.services import get_mcp_manager

        drifted = await get_mcp_manager().disable_all_managed()
    except Exception as exc:
        detail = " ".join(str(exc).split())[:500]
        PrintStyle.error(
            "sem_review_loop uninstall aborted because managed MCP cleanup "
            "failed unexpectedly; plugin recovery data was preserved: "
            f"{detail or exc.__class__.__name__}"
        )
        raise
    if drifted:
        labels = ", ".join(sorted(str(item) for item in drifted))
        PrintStyle.warning(
            "sem_review_loop preserved drifted MCP entries for manual "
            f"cleanup: {labels}"
        )
    remove_plugin_data()
    return True
