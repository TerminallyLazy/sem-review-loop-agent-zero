from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from usr.plugins.sem_review_loop.helpers.sem_types import (
    MAX_CONTENT_BYTES,
    MAX_PATH_BYTES,
    SemParseError,
    validate_relative_posix_path,
)


MAX_WORKING_FILES = 20
MAX_WORKING_BYTES = 2 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 15
_GIT_STATUS_BYTES = 4 * 1024 * 1024


class WorkingTreeError(RuntimeError):
    """A bounded error while collecting the project working tree."""


def _git_error(result: subprocess.CompletedProcess[bytes]) -> str:
    detail = result.stderr.decode("utf-8", errors="replace").strip()
    if len(detail) > 500:
        detail = f"{detail[:497]}..."
    return detail or "git command failed"


def _run_git(root: Path, args: list[str]) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkingTreeError("Git working-tree inspection timed out.") from exc
    except OSError as exc:
        raise WorkingTreeError("Git working-tree inspection could not start.") from exc
    if result.returncode != 0:
        raise WorkingTreeError(_git_error(result))
    return result.stdout


def _safe_path(value: str) -> str:
    if len(value.encode("utf-8", errors="replace")) > MAX_PATH_BYTES:
        raise WorkingTreeError("Git returned an oversized working-tree path.")
    try:
        return validate_relative_posix_path(value, "working-tree path")
    except SemParseError as exc:
        raise WorkingTreeError(str(exc)) from exc


def _in_scope(path: str, watched_relative: str) -> bool:
    if path == ".a0proj" or path.startswith(".a0proj/"):
        return False
    watched = "" if watched_relative in {"", "."} else watched_relative
    return not watched or path == watched or path.startswith(f"{watched}/")


def _decode_source(data: bytes | None) -> str | None:
    if data is None:
        return None
    if len(data) > MAX_CONTENT_BYTES:
        raise WorkingTreeError("A working-tree file exceeds the 2 MiB limit.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(
        (ord(character) < 32 and character not in "\t\n\r")
        or ord(character) == 127
        for character in text
    ):
        return None
    return text


def _read_working_file(root: Path, path: str) -> bytes | None:
    candidate = root / path
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise WorkingTreeError("Working-tree path could not be resolved.") from exc
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise WorkingTreeError("Working-tree path escapes the project.")
    try:
        if candidate.is_symlink():
            raise WorkingTreeError("Symlinked working-tree files are unsupported.")
        if not candidate.exists():
            return None
        if not candidate.is_file():
            raise WorkingTreeError("Working-tree path is not a regular file.")
        return candidate.read_bytes()
    except OSError as exc:
        raise WorkingTreeError("Working-tree file could not be read.") from exc


def _head_file(root: Path, path: str) -> bytes | None:
    try:
        return _run_git(root, ["show", f"HEAD:{path}"])
    except WorkingTreeError as exc:
        # An added file has no HEAD version. Other failures remain visible.
        if "exists on disk, but not in 'HEAD'" in str(exc):
            return None
        if "Path '" in str(exc) and "does not exist in 'HEAD'" in str(exc):
            return None
        raise


def _status_records(raw: bytes) -> list[tuple[str, str, str | None]]:
    if len(raw) > _GIT_STATUS_BYTES:
        raise WorkingTreeError("Git working-tree status exceeded its byte limit.")
    records: list[tuple[str, str, str | None]] = []
    parts = raw.split(b"\0")
    index = 0
    while index < len(parts):
        token = parts[index]
        index += 1
        if not token:
            continue
        if len(token) < 4 or token[2:3] != b" ":
            raise WorkingTreeError("Git returned malformed working-tree status.")
        try:
            status = token[:2].decode("ascii")
            path = token[3:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkingTreeError("Git returned a non-UTF-8 working-tree path.") from exc
        old_path: str | None = None
        if status[0] in {"R", "C"} or status[1] in {"R", "C"}:
            if index >= len(parts) or not parts[index]:
                raise WorkingTreeError("Git returned an incomplete rename record.")
            try:
                old_path = parts[index].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WorkingTreeError("Git returned a non-UTF-8 rename path.") from exc
            index += 1
        records.append((status, path, old_path))
    return records


def collect_working_files(
    root: Path,
    watched_relative: str,
) -> list[dict[str, Any]]:
    """Return bounded Git working changes, including untracked files.

    The result uses the FileChange shape consumed by ``sem diff --stdin``.
    Binary files deliberately carry null source values so SEM emits a binary
    change instead of trying to parse arbitrary bytes as source code.
    """

    status = _run_git(
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    records: list[dict[str, Any]] = []
    total_bytes = 0
    for raw_status, raw_path, raw_old_path in _status_records(status):
        # Git status paths are repository-relative and do not contain a
        # leading slash. Validate before joining them to the project root.
        if raw_path == ".a0proj" or raw_path.startswith(".a0proj/"):
            continue
        path = _safe_path(raw_path)
        old_path = (
            None
            if not raw_old_path
            or raw_old_path == ".a0proj"
            or raw_old_path.startswith(".a0proj/")
            else _safe_path(raw_old_path)
        )
        if not _in_scope(path, watched_relative):
            continue
        if old_path is not None and not _in_scope(old_path, watched_relative):
            old_path = None

        if raw_status == "??" or "A" in raw_status:
            change_status = "added"
        elif "D" in raw_status:
            change_status = "deleted"
        elif "R" in raw_status or "C" in raw_status:
            change_status = "renamed" if old_path else "added"
        else:
            change_status = "modified"

        before_bytes = None
        if change_status in {"modified", "deleted", "renamed"}:
            before_bytes = _head_file(root, old_path or path)
        after_bytes = None
        if change_status in {"added", "modified", "renamed"}:
            after_bytes = _read_working_file(root, path)

        before_content = _decode_source(before_bytes)
        after_content = _decode_source(after_bytes)
        record: dict[str, Any] = {
            "filePath": path,
            "status": change_status,
            "beforeContent": before_content,
            "afterContent": after_content,
        }
        if change_status == "renamed" and old_path:
            record["oldFilePath"] = old_path
        encoded = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        total_bytes += len(encoded)
        if len(records) >= MAX_WORKING_FILES or total_bytes > MAX_WORKING_BYTES:
            raise WorkingTreeError(
                f"Working-tree comparison accepts at most {MAX_WORKING_FILES} "
                "files and 2 MiB."
            )
        records.append(record)
    records.sort(key=lambda item: (str(item["filePath"]), str(item["status"])))
    return records


def canonical_working_payload(files: list[dict[str, Any]]) -> str:
    if not files:
        return "[]"
    payload = json.dumps(
        files,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    if len(payload.encode("utf-8")) > MAX_WORKING_BYTES:
        raise WorkingTreeError("Working-tree comparison exceeds the 2 MiB limit.")
    return payload
