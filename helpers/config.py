from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


PLUGIN_NAME = "sem_review_loop"


@dataclass(frozen=True)
class PluginConfig:
    watched_subdirectory: str
    automatic_refresh: bool
    debounce_ms: int
    automatic_repair: bool
    max_repair_cycles: int
    custom_sem_binary: str
    context_token_budget: int


def _bounded_int(value: object, default: int, low: int, high: int) -> int:
    if isinstance(value, bool):
        parsed = default
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            parsed = default
    return min(high, max(low, parsed))


def _boolean(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def parse_config(raw: Mapping[str, Any] | None) -> PluginConfig:
    values = dict(raw or {})
    watched_subdirectory = str(
        values.get("watched_subdirectory", ".") or "."
    ).strip()
    return PluginConfig(
        watched_subdirectory=watched_subdirectory or ".",
        automatic_refresh=_boolean(
            values.get("automatic_refresh"),
            True,
        ),
        debounce_ms=_bounded_int(
            values.get("debounce_ms"),
            400,
            100,
            5000,
        ),
        automatic_repair=_boolean(
            values.get("automatic_repair"),
            False,
        ),
        max_repair_cycles=_bounded_int(
            values.get("max_repair_cycles"),
            2,
            1,
            3,
        ),
        custom_sem_binary=str(
            values.get("custom_sem_binary", "") or ""
        ).strip(),
        context_token_budget=_bounded_int(
            values.get("context_token_budget"),
            8000,
            1000,
            32000,
        ),
    )


def config_for_agent(agent: object) -> PluginConfig:
    from helpers.plugins import get_plugin_config

    return parse_config(
        get_plugin_config(PLUGIN_NAME, agent=agent)
    )
