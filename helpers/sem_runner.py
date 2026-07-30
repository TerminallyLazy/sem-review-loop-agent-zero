from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ContextManager

from usr.plugins.sem_review_loop.helpers.sem_types import (
    MAX_ENTITY_ID_BYTES,
    MAX_OUTPUT_BYTES,
    DiffRequest,
    DiffSnapshot,
    EntityRef,
    ParsedDiff,
    SemParseError,
    parse_diff,
    sem_v021_change_counters,
    snapshot_from_parsed,
    validate_relative_posix_path,
)

if TYPE_CHECKING:
    from usr.plugins.sem_review_loop.helpers.project_scope import ProjectScope


DIFF_TIMEOUT_SECONDS = 30
QUERY_TIMEOUT_SECONDS = 20
MAX_STDOUT_BYTES = MAX_OUTPUT_BYTES
MAX_STDERR_BYTES = MAX_OUTPUT_BYTES
READ_CHUNK_BYTES = 64 * 1024
PROCESS_REAP_TIMEOUT_SECONDS = 2.0
IO_WORKER_JOIN_SECONDS = 1.0
MAX_ERROR_CHARS = 500
MAX_REF_BYTES = 200
MAX_MANUAL_FILES = 20
MAX_MANUAL_BYTES = 2 * 1024 * 1024
MAX_CONTEXT_TOKEN_BUDGET = 32_000
MIN_CONTEXT_TOKEN_BUDGET = 1_000

LOCAL_ENV = {
    "SEM_LOCAL": "1",
    "SEM_NO_NETWORK": "1",
    "SEM_NO_TELEMETRY": "1",
    "SEM_NO_UPDATE_CHECK": "1",
    "SEM_NO_AUTOWARM": "1",
    "SEM_NO_SIDECAR": "1",
    "DO_NOT_TRACK": "1",
}
PASSTHROUGH_ENV = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SYSTEMROOT",
    "WINDIR",
)
ALLOWED_MANUAL_STATUS = frozenset(
    {"added", "modified", "deleted", "renamed"}
)
MANUAL_FIELDS = frozenset(
    {
        "filePath",
        "oldFilePath",
        "status",
        "beforeContent",
        "afterContent",
    }
)

_CACHE_COMPONENT = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_SEM_GLOB_CHARACTERS = frozenset("*?[")
_PROTECTED_METADATA_ROOT = ".a0proj"


class SemCommandError(RuntimeError):
    """A bounded, source-free failure from local SEM command execution."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    pid = getattr(process, "pid", None)
    if os.name == "posix" and isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            # Fall through to the direct-process kill. The child is always
            # started in its own session, so a successful killpg terminates
            # every descendant that remained in the process group.
            pass
    elif os.name == "nt" and isinstance(pid, int) and pid > 0:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
                timeout=PROCESS_REAP_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    try:
        running = process.poll() is None
    except Exception:
        running = True
    if running:
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> bool:
    _kill_process_tree(process)
    deadline = time.monotonic() + PROCESS_REAP_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            try:
                return process.poll() is not None
            except Exception:
                return False
        try:
            process.wait(timeout=min(0.05, remaining))
            return True
        except subprocess.TimeoutExpired:
            continue
        except (ChildProcessError, ProcessLookupError):
            return True
        except OSError:
            try:
                return process.poll() is not None
            except Exception:
                return False


def _join_workers(
    workers: list[threading.Thread],
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    for worker in workers:
        worker.join(timeout=max(0.0, deadline - time.monotonic()))
    return not any(worker.is_alive() for worker in workers)


def _bounded_error(value: object) -> str:
    text = _ANSI_ESCAPE.sub("", str(value))
    printable = "".join(
        character if character.isprintable() else " " for character in text
    )
    return " ".join(printable.split())[:MAX_ERROR_CHARS]


def run_bounded(
    *,
    args: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    stdin: str | None = None,
    stdout_limit: int = MAX_STDOUT_BYTES,
    stderr_limit: int = MAX_STDERR_BYTES,
) -> CommandResult:
    if (
        not args
        or any(
            not isinstance(argument, str) or "\x00" in argument
            for argument in args
        )
    ):
        raise SemCommandError("sem command arguments are invalid.")
    if timeout <= 0:
        raise SemCommandError("sem command timeout must be positive.")
    if stdout_limit < 0 or stderr_limit < 0:
        raise SemCommandError("sem output limits must be non-negative.")
    try:
        stdin_bytes = b"" if stdin is None else stdin.encode("utf-8")
    except (AttributeError, UnicodeEncodeError) as exc:
        raise SemCommandError("sem stdin is not valid UTF-8 text.") from exc

    try:
        isolation: dict[str, object]
        if os.name == "nt":
            isolation = {
                "creationflags": getattr(
                    subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    0,
                )
            }
        else:
            isolation = {"start_new_session": True}
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            bufsize=0,
            **isolation,
        )
    except OSError as exc:
        detail = _bounded_error(exc) or "unable to start process"
        raise SemCommandError(f"sem command could not start: {detail}") from exc

    if process.stdin is None or process.stdout is None or process.stderr is None:
        _terminate_and_reap(process)
        raise SemCommandError("sem command pipes were unavailable.")

    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()
    overflow_stream: list[str] = []
    reader_failure = threading.Event()
    writer_failure = threading.Event()
    state_lock = threading.Lock()

    def read_stream(name: str, stream: Any, limit: int) -> None:
        try:
            while True:
                chunk = stream.read(READ_CHUNK_BYTES)
                if not chunk:
                    return
                if not isinstance(chunk, bytes):
                    reader_failure.set()
                    return
                with state_lock:
                    remaining = limit - len(buffers[name])
                    if remaining > 0:
                        buffers[name].extend(chunk[:remaining])
                    if len(chunk) > max(remaining, 0):
                        if not overflow_stream:
                            overflow_stream.append(name)
                        overflow.set()
                        return
        except Exception:
            reader_failure.set()

    def write_stdin() -> None:
        try:
            if stdin_bytes:
                process.stdin.write(stdin_bytes)
                process.stdin.flush()
        except BrokenPipeError:
            pass
        except Exception:
            writer_failure.set()
        finally:
            try:
                process.stdin.close()
            except Exception:
                pass

    readers = [
        threading.Thread(
            target=read_stream,
            args=("stdout", process.stdout, stdout_limit),
            name="sem-stdout-reader",
        ),
        threading.Thread(
            target=read_stream,
            args=("stderr", process.stderr, stderr_limit),
            name="sem-stderr-reader",
        ),
    ]
    writer = threading.Thread(target=write_stdin, name="sem-stdin-writer")
    for reader in readers:
        reader.start()
    writer.start()

    failure: SemCommandError | None = None
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if overflow.is_set():
                name = overflow_stream[0] if overflow_stream else "output"
                failure = SemCommandError(
                    f"sem {name} exceeded its byte limit."
                )
                break
            if reader_failure.is_set():
                failure = SemCommandError("sem output reader failed.")
                break
            if writer_failure.is_set():
                failure = SemCommandError("sem stdin writer failed.")
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = SemCommandError(
                    f"sem command timed out after {timeout:g} seconds."
                )
                break
            try:
                process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                continue

        if failure is not None:
            _terminate_and_reap(process)

        workers = [writer, *readers]
        workers_stopped = _join_workers(
            workers,
            IO_WORKER_JOIN_SECONDS,
        )

        if overflow.is_set() and failure is None:
            name = overflow_stream[0] if overflow_stream else "output"
            failure = SemCommandError(
                f"sem {name} exceeded its byte limit."
            )
        if reader_failure.is_set() and failure is None:
            failure = SemCommandError("sem output reader failed.")
        if writer_failure.is_set() and failure is None:
            failure = SemCommandError("sem stdin writer failed.")
        if not workers_stopped:
            _terminate_and_reap(process)
            _join_workers(workers, IO_WORKER_JOIN_SECONDS)
            failure = SemCommandError(
                "sem command I/O workers did not terminate."
            )
        if failure is not None:
            raise failure

        returncode = process.returncode
        if returncode is None:
            try:
                returncode = process.wait(
                    timeout=PROCESS_REAP_TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired as exc:
                _terminate_and_reap(process)
                raise SemCommandError(
                    "sem command did not terminate cleanly."
                ) from exc
        try:
            stdout = bytes(buffers["stdout"]).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SemCommandError(
                "sem stdout was not valid UTF-8."
            ) from exc
        return CommandResult(
            returncode=int(returncode),
            stdout=stdout,
            stderr=bytes(buffers["stderr"]).decode(
                "utf-8",
                errors="replace",
            ),
        )
    finally:
        if process.poll() is None or any(
            worker.is_alive() for worker in [writer, *readers]
        ):
            _terminate_and_reap(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except Exception:
                pass
        _join_workers(
            [writer, *readers],
            IO_WORKER_JOIN_SECONDS,
        )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _unique_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_loads(value: str) -> object:
    try:
        return json.loads(
            value,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SemCommandError("sem returned invalid JSON.") from exc


def _encoded_size(value: str, field: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise SemCommandError(f"{field} is not valid UTF-8 text.") from exc


def _manual_path(value: object, field: str) -> str:
    try:
        return validate_relative_posix_path(value, field)
    except SemParseError as exc:
        raise SemCommandError(str(exc)) from exc


def normalize_manual_files(files: object) -> list[dict[str, str]]:
    if not isinstance(files, list):
        raise SemCommandError("files must be an array.")
    if not 1 <= len(files) <= MAX_MANUAL_FILES:
        raise SemCommandError(
            f"Manual comparison accepts 1 to {MAX_MANUAL_FILES} files."
        )

    normalized: list[dict[str, str]] = []
    for index, value in enumerate(files):
        field = f"files[{index}]"
        if not isinstance(value, dict):
            raise SemCommandError(f"{field} must be an object.")
        if any(not isinstance(key, str) for key in value):
            raise SemCommandError(f"{field} keys must be strings.")
        unknown = sorted(set(value) - MANUAL_FIELDS)
        if unknown:
            raise SemCommandError(
                f"{field} contains unknown fields: {', '.join(unknown)}."
            )

        raw_status = value.get("status")
        if not isinstance(raw_status, str):
            raise SemCommandError(f"{field}.status is invalid.")
        status = raw_status.strip().lower()
        if status not in ALLOWED_MANUAL_STATUS:
            raise SemCommandError(f"{field}.status is invalid.")

        record = {
            "filePath": _manual_path(
                value.get("filePath"),
                f"{field}.filePath",
            ),
            "status": status,
        }
        old_path = value.get("oldFilePath")
        if old_path is not None and old_path != "":
            record["oldFilePath"] = _manual_path(
                old_path,
                f"{field}.oldFilePath",
            )
        for content_field in ("beforeContent", "afterContent"):
            content = value.get(content_field, "")
            if not isinstance(content, str):
                raise SemCommandError(
                    f"{field}.{content_field} must be text."
                )
            record[content_field] = content
        normalized.append(record)

    payload = _encode_manual_payload(normalized)
    if len(payload) > MAX_MANUAL_BYTES:
        raise SemCommandError("Manual comparison exceeds the 2 MiB limit.")
    return normalized


def _encode_manual_payload(files: list[dict[str, str]]) -> bytes:
    try:
        return json.dumps(
            files,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError) as exc:
        raise SemCommandError(
            "Manual comparison contains invalid text."
        ) from exc


def canonical_manual_payload(files: object) -> str:
    normalized = normalize_manual_files(files)
    return _encode_manual_payload(normalized).decode("utf-8")


def _bounded_ref(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.startswith("-")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or _encoded_size(value, field) > MAX_REF_BYTES
    ):
        raise SemCommandError(f"A valid {field} ref is required.")
    return value


def _is_protected_metadata_path(value: object) -> bool:
    return isinstance(value, str) and (
        value == _PROTECTED_METADATA_ROOT
        or value.startswith(f"{_PROTECTED_METADATA_ROOT}/")
    )


def _touches_protected_metadata(value: object) -> bool:
    return isinstance(value, dict) and (
        _is_protected_metadata_path(value.get("filePath"))
        or _is_protected_metadata_path(value.get("oldFilePath"))
    )


def _validation_path_alias(value: object) -> object:
    if not _is_protected_metadata_path(value):
        return value
    assert isinstance(value, str)
    return f"a0proj_{value[len(_PROTECTED_METADATA_ROOT):]}"


def _full_validation_payload(payload: object) -> object:
    """Alias only the protected root so the strict path validator can run."""

    if not isinstance(payload, dict):
        return payload
    validation_payload = dict(payload)
    for collection_name in ("changes", "binaryChanges"):
        collection = payload.get(collection_name)
        if not isinstance(collection, list):
            continue
        validation_records: list[object] = []
        for value in collection:
            if not isinstance(value, dict):
                validation_records.append(value)
                continue
            record = dict(value)
            for path_field in ("filePath", "oldFilePath"):
                if path_field in record:
                    record[path_field] = _validation_path_alias(
                        record[path_field]
                    )
            validation_records.append(record)
        validation_payload[collection_name] = validation_records
    return validation_payload


def _validate_file_count(
    payload: object,
    parsed: ParsedDiff,
) -> None:
    if not isinstance(payload, dict):
        return
    changes = payload.get("changes")
    binary_changes = payload.get("binaryChanges")
    if not isinstance(changes, list) or not isinstance(binary_changes, list):
        return
    semantic_files = {
        change["filePath"]
        for change in changes
        if isinstance(change, dict)
        and isinstance(change.get("filePath"), str)
    }
    expected = len(semantic_files) + len(binary_changes)
    if parsed.summary.file_count != expected:
        raise SemParseError(
            "summary.fileCount does not match changed files."
        )


def _filter_protected_diff_payload(payload: object) -> object:
    """Remove Agent Zero metadata before the strict parser retains details."""

    if not isinstance(payload, dict):
        return payload
    changes = payload.get("changes")
    binary_changes = payload.get("binaryChanges")
    if not isinstance(changes, list) or not isinstance(binary_changes, list):
        return payload

    kept_changes = [
        change
        for change in changes
        if not _touches_protected_metadata(change)
    ]
    kept_binary_changes = [
        change
        for change in binary_changes
        if not _touches_protected_metadata(change)
    ]
    if (
        len(kept_changes) == len(changes)
        and len(kept_binary_changes) == len(binary_changes)
    ):
        return payload

    sanitized = dict(payload)
    sanitized["changes"] = kept_changes
    sanitized["binaryChanges"] = kept_binary_changes
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return sanitized

    counters = sem_v021_change_counters(kept_changes)
    semantic_files: set[str] = set()
    orphan_count = 0
    for change in kept_changes:
        if not isinstance(change, dict):
            continue
        file_path = change.get("filePath")
        if isinstance(file_path, str):
            semantic_files.add(file_path)
        if change.get("entityType") == "orphan":
            orphan_count += 1

    sanitized_summary = dict(summary)
    sanitized_summary.update(counters)
    sanitized_summary.update(
        {
            "fileCount": len(semantic_files) + len(kept_binary_changes),
            "binary": len(kept_binary_changes),
            "orphan": orphan_count,
            "total": len(kept_changes) + len(kept_binary_changes),
        }
    )
    sanitized["summary"] = sanitized_summary
    return sanitized


def _parse_filtered_diff(payload: object) -> ParsedDiff:
    validation_payload = _full_validation_payload(payload)
    full_parsed = parse_diff(validation_payload)
    _validate_file_count(payload, full_parsed)
    filtered_payload = _filter_protected_diff_payload(payload)
    filtered_parsed = parse_diff(filtered_payload)
    _validate_file_count(filtered_payload, filtered_parsed)
    return filtered_parsed


class SemRunner:
    def __init__(
        self,
        cache_root: Path,
        execute: Callable[..., CommandResult] = run_bounded,
        *,
        binary_lease: Callable[[], ContextManager[Path]],
    ) -> None:
        self.cache_root = Path(cache_root)
        self._execute = execute
        self._binary_lease = binary_lease

    def _binary_context(self) -> ContextManager[Path]:
        return self._binary_lease()

    def environment(self, scope: ProjectScope) -> dict[str, str]:
        project_id = str(scope.project_id)
        if _CACHE_COMPONENT.fullmatch(project_id) is None:
            raise SemCommandError("Project cache identity is invalid.")
        try:
            project_root = Path(scope.project_root).resolve(strict=True)
            cache_root = self.cache_root.resolve(strict=False)
            cache_root.mkdir(parents=True, exist_ok=True)
            cache = (cache_root / project_id).resolve(strict=False)
            if cache_root not in cache.parents:
                raise SemCommandError(
                    "SEM cache path is outside plugin-owned storage."
                )
            cache.mkdir(parents=True, exist_ok=True)
            runtime_root = (cache / "runtime").resolve(strict=False)
            if cache not in runtime_root.parents:
                raise SemCommandError(
                    "SEM runtime path is outside plugin-owned storage."
                )
            runtime_home = runtime_root / "home"
            runtime_temp = runtime_root / "tmp"
            xdg_cache = runtime_home / ".cache"
            xdg_config = runtime_home / ".config"
            xdg_data = runtime_home / ".local" / "share"
            xdg_state = runtime_home / ".local" / "state"
            app_data = runtime_home / "AppData" / "Roaming"
            local_app_data = runtime_home / "AppData" / "Local"
            for directory in (
                runtime_home,
                runtime_temp,
                xdg_cache,
                xdg_config,
                xdg_data,
                xdg_state,
                app_data,
                local_app_data,
            ):
                directory.mkdir(parents=True, exist_ok=True)
        except SemCommandError:
            raise
        except (OSError, RuntimeError) as exc:
            raise SemCommandError(
                "Unable to prepare local SEM execution paths."
            ) from exc
        return {
            **{
                key: os.environ[key]
                for key in PASSTHROUGH_ENV
                if os.environ.get(key)
            },
            **LOCAL_ENV,
            "SEM_REPO": str(project_root),
            "SEM_CACHE_DIR": str(cache),
            "HOME": str(runtime_home),
            "USERPROFILE": str(runtime_home),
            "TMPDIR": str(runtime_temp),
            "TEMP": str(runtime_temp),
            "TMP": str(runtime_temp),
            "XDG_CACHE_HOME": str(xdg_cache),
            "XDG_CONFIG_HOME": str(xdg_config),
            "XDG_DATA_HOME": str(xdg_data),
            "XDG_STATE_HOME": str(xdg_state),
            "APPDATA": str(app_data),
            "LOCALAPPDATA": str(local_app_data),
        }

    def _sem_watched_pathspec(self, scope: ProjectScope) -> str:
        watched = str(scope.watched_relative)
        if watched == ".":
            return watched
        try:
            safe_path = validate_relative_posix_path(
                watched,
                "watched pathspec",
            )
        except SemParseError as exc:
            raise SemCommandError(str(exc)) from exc
        if (
            any(
                part.startswith(":")
                for part in safe_path.split("/")
            )
            or any(
                character in _SEM_GLOB_CHARACTERS
                for character in safe_path
            )
        ):
            raise SemCommandError(
                "Watched directory cannot be represented safely by "
                "sem 0.21.0 path filtering."
            )
        return safe_path

    def _diff_args(
        self,
        scope: ProjectScope,
        request: DiffRequest,
    ) -> list[str]:
        if request.mode == "working":
            mode_args: list[str] = []
        elif request.mode == "staged":
            mode_args = ["--staged"]
        elif request.mode == "commit":
            mode_args = [
                "--commit",
                _bounded_ref(request.commit, "commit"),
            ]
        elif request.mode == "range":
            mode_args = [
                "--from",
                _bounded_ref(request.from_ref, "from"),
                "--to",
                _bounded_ref(request.to_ref, "to"),
            ]
        elif request.mode == "stdin":
            raise SemCommandError(
                "stdin comparisons must use the in-memory manual API."
            )
        else:
            raise SemCommandError("Unsupported semantic diff mode.")
        return [
            "diff",
            *mode_args,
            "--format",
            "json",
            "--",
            self._sem_watched_pathspec(scope),
        ]

    def _json_command(
        self,
        scope: ProjectScope,
        args: list[str],
        *,
        timeout: float,
        stdin: str | None = None,
    ) -> object:
        environment = self.environment(scope)
        with self._binary_context() as leased_binary:
            result = self._execute(
                args=[str(Path(leased_binary)), *args],
                cwd=Path(scope.project_root),
                env=environment,
                timeout=timeout,
                stdin=stdin,
            )
        if not isinstance(result, CommandResult):
            raise SemCommandError("sem executor returned an invalid result.")
        if (
            _encoded_size(result.stdout, "sem stdout") > MAX_STDOUT_BYTES
            or _encoded_size(result.stderr, "sem stderr") > MAX_STDERR_BYTES
        ):
            raise SemCommandError("sem command output exceeded its byte limit.")
        if result.returncode != 0:
            detail = _bounded_error(result.stderr or result.stdout or "failed")
            raise SemCommandError(
                f"sem command failed (exit {result.returncode}): "
                f"{detail or 'failed'}"
            )
        return _strict_json_loads(result.stdout)

    def diff(
        self,
        scope: ProjectScope,
        request: DiffRequest,
        fingerprint: str,
    ) -> DiffSnapshot:
        payload = self._json_command(
            scope,
            self._diff_args(scope, request),
            timeout=DIFF_TIMEOUT_SECONDS,
        )
        return snapshot_from_parsed(
            _parse_filtered_diff(payload),
            request,
            fingerprint,
        )

    def manual_diff(
        self,
        scope: ProjectScope,
        files: object,
        fingerprint: str,
    ) -> DiffSnapshot:
        payload = canonical_manual_payload(files)
        parsed = self._json_command(
            scope,
            ["diff", "--stdin", "--format", "json"],
            timeout=DIFF_TIMEOUT_SECONDS,
            stdin=payload,
        )
        return snapshot_from_parsed(
            _parse_filtered_diff(parsed),
            DiffRequest("stdin"),
            fingerprint,
        )

    def _entity_file(
        self,
        scope: ProjectScope,
        entity: EntityRef,
    ) -> str:
        try:
            safe_path = validate_relative_posix_path(
                entity.file_path,
                "Selected entity file",
            )
            resolved = (
                Path(scope.project_root) / Path(safe_path)
            ).resolve(strict=True)
            watched_root = Path(scope.watched_root).resolve(strict=True)
            resolved.relative_to(watched_root)
        except (SemParseError, OSError, RuntimeError, ValueError) as exc:
            raise SemCommandError(
                "Selected entity file is outside review scope."
            ) from exc
        if not resolved.is_file():
            raise SemCommandError(
                "Selected entity file is outside review scope."
            )
        return resolved.relative_to(
            Path(scope.project_root).resolve(strict=True)
        ).as_posix()

    def _entity_id(self, entity: EntityRef) -> str:
        value = entity.entity_id
        if (
            not isinstance(value, str)
            or not value.strip()
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in value
            )
            or _encoded_size(value, "entity id") > MAX_ENTITY_ID_BYTES
        ):
            raise SemCommandError("Selected entity metadata is invalid.")
        return value

    def _query(
        self,
        scope: ProjectScope,
        args: list[str],
    ) -> Mapping[str, Any]:
        payload = self._json_command(
            scope,
            args,
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        if not isinstance(payload, dict):
            raise SemCommandError(
                "sem query result must be a JSON object."
            )
        return payload

    def context(
        self,
        scope: ProjectScope,
        entity: EntityRef,
        token_budget: int,
    ) -> Mapping[str, Any]:
        if (
            isinstance(token_budget, bool)
            or not isinstance(token_budget, int)
            or not MIN_CONTEXT_TOKEN_BUDGET
            <= token_budget
            <= MAX_CONTEXT_TOKEN_BUDGET
        ):
            raise SemCommandError("Context token budget is out of range.")
        return self._query(
            scope,
            [
                "context",
                "--entity-id",
                self._entity_id(entity),
                "--file",
                self._entity_file(scope, entity),
                "--budget",
                str(token_budget),
                "--json",
            ],
        )

    def impact(
        self,
        scope: ProjectScope,
        entity: EntityRef,
    ) -> Mapping[str, Any]:
        return self._query(
            scope,
            [
                "impact",
                "--entity-id",
                self._entity_id(entity),
                "--file",
                self._entity_file(scope, entity),
                "--depth",
                "2",
                "--json",
            ],
        )
