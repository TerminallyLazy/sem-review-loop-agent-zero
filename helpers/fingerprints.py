from __future__ import annotations

import hashlib
import json
import os
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
from usr.plugins.sem_review_loop.helpers.working_tree import (
    MAX_WORKING_BYTES,
    WorkingTreeError,
    canonical_working_payload,
    collect_working_files,
)


_ERROR_DETAIL_LIMIT = 1000
_REF_OUTPUT_LIMIT = 128
_STREAM_CHUNK_SIZE = 64 * 1024
_GIT_TIMEOUT_SECONDS = 15
_VALID_OID_LENGTHS = frozenset({40, 64})


class FingerprintError(RuntimeError):
    pass


class RepositoryNotReadyError(FingerprintError):
    """Review has no Git baseline; this is setup, not an agent repair task."""


NOT_A_REPOSITORY = (
    "Semantic Review is unavailable: the Agent Zero project directory is not a Git repository. "
    "Review requires a project rooted in a Git checkout with an initial commit. "
    "Repositories in child folders are not detected from the project root."
)
NO_INITIAL_COMMIT = (
    "Semantic Review is unavailable: this Git repository has no initial commit. "
    "An initial commit is required before reviewing changes against HEAD."
)


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
            env={**os.environ, "LC_ALL": "C"},
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
        if "not a git repository" in detail.lower():
            raise RepositoryNotReadyError(NOT_A_REPOSITORY)
        suffix = f": {detail}" if detail else "."
        raise FingerprintError(
            f"Git fingerprint command failed{suffix}"
        )


def _has_unborn_head(root: Path) -> bool:
    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, shell=False,
            timeout=_GIT_TIMEOUT_SECONDS, env={**os.environ, "LC_ALL": "C"},
        )
    try:
        inside = run("rev-parse", "--is-inside-work-tree")
        if inside.returncode or inside.stdout.strip() != b"true":
            return False
        symbolic = run("symbolic-ref", "--quiet", "HEAD")
        branch = symbolic.stdout.decode("utf-8").strip()
        if symbolic.returncode or not branch.startswith("refs/heads/"):
            return False
        return run("show-ref", "--verify", "--quiet", branch).returncode == 1
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return False


def _resolve_commit(root: Path, ref: str) -> bytes:
    """Resolve and validate one ref as an immutable commit object ID."""

    output = bytearray()
    try:
        _stream_git_stdout(
            root,
            ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
            output.extend,
            max_stdout_bytes=_REF_OUTPUT_LIMIT,
        )
    except RepositoryNotReadyError:
        raise
    except FingerprintError:
        # Only an unborn symbolic HEAD is a missing baseline. Invalid refs,
        # permissions, corruption and other Git failures remain real errors.
        if ref == "HEAD" and _has_unborn_head(root):
            raise RepositoryNotReadyError(NO_INITIAL_COMMIT) from None
        raise
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


def working_fingerprint(
    root: Path,
    watched_relative: str,
    maximum_bytes: int = MAX_WORKING_BYTES,
) -> str:
    """Hash the bounded working set, including untracked tool-created files."""
    return diff_fingerprint(
        root,
        watched_relative,
        DiffRequest("working"),
        maximum_bytes,
    )


def diff_fingerprint(
    root: Path,
    watched_relative: str,
    request: DiffRequest,
    maximum_working_bytes: int = MAX_WORKING_BYTES,
) -> str:
    pathspec = [
        "--",
        literal_watched_pathspec(watched_relative),
        AGENT_ZERO_METADATA_EXCLUDE_PATHSPEC,
    ]
    if request.mode == "working":
        resolved_refs = _resolve_commit(root, "HEAD")
        try:
            working_files = collect_working_files(
                root,
                watched_relative,
                maximum_working_bytes,
            )
            working_payload = canonical_working_payload(
                working_files,
                maximum_working_bytes,
            )
        except WorkingTreeError as exc:
            detail = str(exc)
            if len(detail) > _ERROR_DETAIL_LIMIT:
                detail = f"{detail[: _ERROR_DETAIL_LIMIT - 3]}..."
            raise FingerprintError(
                f"Git working-tree inspection failed: {detail}"
            ) from exc
        request_identity = json.dumps(
            asdict(request),
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256()
        digest.update(
            b"sem-review-v2\0"
            + request_identity
            + b"\0"
            + resolved_refs
            + b"\0"
            + working_payload.encode("utf-8")
        )
        return digest.hexdigest()
    if request.mode == "staged":
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
