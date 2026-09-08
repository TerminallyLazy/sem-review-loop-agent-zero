from __future__ import annotations

from typing import Any

from helpers.print_style import PrintStyle
from usr.plugins.sem_review_loop.helpers.installer import (
    UnsupportedPlatformError,
    ensure_installed,
    remove_plugin_data,
)


async def maintain(**kwargs: Any) -> bool:
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
    from pathlib import Path
    from helpers import projects, plugins
    from usr.plugins.sem_review_loop.helpers.config import parse_config
    from usr.plugins.sem_review_loop.helpers.project_scope import make_scope
    from usr.plugins.sem_review_loop.helpers.services import get_mcp_manager

    parent = Path(projects.get_projects_parent_folder())
    project_list = projects.get_active_projects_list() if parent.is_dir() else []
    for project in project_list:
        name = project["name"]
        try:
            config = parse_config(plugins.get_plugin_config(
                "sem_review_loop", project_name=name,
            ))
            scope = make_scope(
                "install", name, Path(projects.get_project_folder(name)), ".",
            )
            status = await get_mcp_manager().ensure_enabled(scope, config)
            if not status.get("enabled"):
                PrintStyle.warning(
                    f"Semantic tools need attention for project {name}: "
                    f"{status.get('error') or 'MCP verification failed'}"
                )
        except Exception as exc:
            PrintStyle.warning(
                f"Semantic tools could not connect for project {name}: "
                f"{type(exc).__name__}. Open Semantic Review to retry."
            )
    return True


async def install(**kwargs: Any) -> bool:
    return await maintain(**kwargs)


async def pre_update(**kwargs: Any) -> bool:
    return await maintain(**kwargs)


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
