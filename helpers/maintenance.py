from __future__ import annotations

import json

from usr.plugins.sem_review_loop.helpers.installer import (
    SEM_VERSION,
    ensure_installed,
)


MAX_ERROR_CHARS = 500


def _bounded_error(exc: Exception) -> str:
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__
    message = message.encode("utf-8", errors="replace").decode("utf-8")
    message = " ".join(message.replace("\x00", "").split())
    return message[:MAX_ERROR_CHARS] or exc.__class__.__name__


def main() -> int:
    try:
        path = ensure_installed()
        result = {
            "ok": True,
            "path": str(path),
            "version": SEM_VERSION,
        }
        status = 0
    except Exception as exc:
        result = {"ok": False, "error": _bounded_error(exc)}
        status = 1
    print(json.dumps(result, separators=(",", ":")))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
