from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from usr.plugins.sem_review_loop.helpers.project_scope import (
    AGENT_ZERO_METADATA_EXCLUDE_PATHSPEC,
    literal_watched_pathspec,
)
from usr.plugins.sem_review_loop.helpers.sem_types import DiffRequest


_ERROR_DETAIL_LIMIT = 1000
_REF_OUTPUT_LIMIT = 128
_STREAM_CHUNK_SIZE = 64 * 1024
_GIT_TIMEOUT_SECONDS = 15
_VALID_OID_LENGTHS = frozenset({40, 64})


class FingerprintError(RuntimeError):
    pass


def _bounded_error_detail(stderr: bytes) -> str:
    detail = stderr.decode("utf-8", errors="replace").strip()
    if len(detail) <= _ERROR_DETAIL_LIMIT:
        return detail
    return f"{detail[: _ERROR_DETAIL_LIMIT - 3]}..."


def _stream_git_stdout(
    root: Path,
    args: list[str],
    consume_stdout: Callable[[bytes], None],
    *,
    max_stdout_bytes: int | None = None,
) -> None:
    """Feed Git stdout to a consumer without retaining the full output."""

    try:
        process = subprocess.Popen(
            ["git", *args],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except OSError as exc:
        detail = _bounded_error_detail(
            str(exc).encode("utf-8", errors="replace")
        )
        suffix = f": {detail}" if detail else "."
        raise FingerprintError(
            f"Git fingerprint command could not start{suffix}"
        ) from exc

    if process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise FingerprintError(
            "Git fingerprint command did not provide output pipes."
        )

    stderr_capture = bytearray()
    stderr_read_error: list[Exception] = []

    def drain_stderr() -> None:
        try:
            while chunk := process.stderr.read(_STREAM_CHUNK_SIZE):
                remaining = (
                    _ERROR_DETAIL_LIMIT + 1 - len(stderr_capture)
                )
                if remaining > 0:
                    stderr_capture.extend(chunk[:remaining])
        except Exception as exc:
            stderr_read_error.append(exc)

    stderr_thread = threading.Thread(
        target=drain_stderr,
        name="sem-review-git-stderr",
        daemon=True,
    )
    stderr_thread.start()

    timed_out = threading.Event()

    def kill_on_timeout() -> None:
        if process.poll() is None:
            timed_out.set()
            try:
                process.kill()
            except OSError:
                pass

    timeout = threading.Timer(
        _GIT_TIMEOUT_SECONDS,
        kill_on_timeout,
    )
    timeout.daemon = True
    timeout.start()

    stdout_bytes = 0
    stream_error: Exception | None = None
    try:
        while chunk := process.stdout.read(_STREAM_CHUNK_SIZE):
            stdout_bytes += len(chunk)
            if (
                max_stdout_bytes is not None
                and stdout_bytes > max_stdout_bytes
            ):
                stream_error = FingerprintError(
                    "Git fingerprint reference output exceeded its byte limit."
                )
                process.kill()
                break
            consume_stdout(chunk)
    except Exception as exc:
        stream_error = exc
        try:
            process.kill()
        except OSError:
            pass
    finally:
        returncode = process.wait()
        timeout.cancel()
        stderr_thread.join(timeout=1)
        process.stdout.close()
        process.stderr.close()

    if timed_out.is_set():
        raise FingerprintError(
            "Git fingerprint command timed out after 15 seconds."
        )
    if stderr_thread.is_alive() or stderr_read_error:
        raise FingerprintError(
            "Git fingerprint command stderr could not be read safely."
        )
    if stream_error is not None:
        if isinstance(stream_error, FingerprintError):
            raise stream_error
        raise FingerprintError(
            "Git fingerprint command output could not be read."
        ) from stream_error
    if returncode != 0:
        detail = _bounded_error_detail(bytes(stderr_capture))
        suffix = f": {detail}" if detail else "."
        raise FingerprintError(
            f"Git fingerprint command failed{suffix}"
        )


def _resolve_commit(root: Path, ref: str) -> bytes:
    """Resolve and validate one ref as an immutable commit object ID."""

    output = bytearray()
    _stream_git_stdout(
        root,
        [
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{ref}^{{commit}}",
        ],
        output.extend,
        max_stdout_bytes=_REF_OUTPUT_LIMIT,
    )
    object_id = bytes(output).strip()
    if (
        len(object_id) not in _VALID_OID_LENGTHS
        or any(
            character not in b"0123456789abcdef"
            for character in object_id
        )
    ):
        raise FingerprintError(
            "Git fingerprint ref did not resolve to a valid commit object ID."
        )
    return object_id


def working_fingerprint(root: Path, watched_relative: str) -> str:
    """Hash sem's tracked-file working diff.

    Untracked files are intentionally excluded until sem supports an
    equivalent stable input mode.
    """
    return diff_fingerprint(
        root,
        watched_relative,
        DiffRequest("working"),
    )


def diff_fingerprint(
    root: Path,
    watched_relative: str,
    request: DiffRequest,
) -> str:
    pathspec = [
        "--",
        literal_watched_pathspec(watched_relative),
        AGENT_ZERO_METADATA_EXCLUDE_PATHSPEC,
    ]
    if request.mode == "working":
        resolved_refs = _resolve_commit(root, "HEAD")
        command = [
            "diff",
            "--binary",
            resolved_refs.decode("ascii"),
            *pathspec,
        ]
    elif request.mode == "staged":
        resolved_refs = _resolve_commit(root, "HEAD")
        command = [
            "diff",
            "--binary",
            "--cached",
            resolved_refs.decode("ascii"),
            *pathspec,
        ]
    elif request.mode == "commit":
        resolved_refs = _resolve_commit(root, request.commit)
        command = [
            "show",
            "--format=",
            "--binary",
            resolved_refs.decode("ascii"),
            *pathspec,
        ]
    elif request.mode == "range":
        from_object_id = _resolve_commit(root, request.from_ref)
        to_object_id = _resolve_commit(root, request.to_ref)
        command = [
            "diff",
            "--binary",
            from_object_id.decode("ascii"),
            to_object_id.decode("ascii"),
            *pathspec,
        ]
        resolved_refs = b"\0".join(
            [
                from_object_id,
                to_object_id,
            ]
        )
    elif request.mode == "stdin":
        raise FingerprintError(
            "stdin comparisons use an in-memory fingerprint."
        )
    else:
        raise FingerprintError(
            f"Unsupported fingerprint mode: {request.mode!r}."
        )

    request_identity = json.dumps(
        asdict(request),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(
        b"sem-review-v1\0"
        + request_identity
        + b"\0"
        + resolved_refs
        + b"\0"
    )
    _stream_git_stdout(root, command, digest.update)
    return digest.hexdigest()
