from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from usr.plugins.sem_review_loop.helpers.project_scope import make_scope
from usr.plugins.sem_review_loop.helpers.sem_runner import (
    MAX_MANUAL_BYTES,
    CommandResult,
    SemCommandError,
    SemRunner,
    canonical_manual_payload,
    normalize_manual_files,
    run_bounded,
)
from usr.plugins.sem_review_loop.helpers.sem_types import (
    MAX_CONTENT_BYTES,
    DiffRequest,
    EntityRef,
    SemParseError,
)


EMPTY_DIFF = {
    "summary": {
        "fileCount": 0,
        "added": 0,
        "modified": 0,
        "deleted": 0,
        "moved": 0,
        "renamed": 0,
        "reordered": 0,
        "binary": 0,
        "orphan": 0,
        "total": 0,
    },
    "changes": [],
    "binaryChanges": [],
}


@contextmanager
def fixed_binary_lease() -> Iterator[Path]:
    yield Path("/opt/sem")


def test_environment_is_narrow_and_forces_local_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scope = make_scope("ctx", "project", project, ".")
    cache_root = tmp_path / "plugin-cache"
    monkeypatch.setenv("PATH", "/synthetic/bin")
    monkeypatch.setenv("HOME", "/synthetic/home")
    monkeypatch.setenv("USERPROFILE", "/synthetic/profile")
    monkeypatch.setenv("TMPDIR", "/synthetic/tmp")
    monkeypatch.setenv("TEMP", "/synthetic/temp")
    monkeypatch.setenv("TMP", "/synthetic/windows-tmp")
    monkeypatch.setenv("LANG", "C.UTF-8")
    for key in (
        "SHOULD_NOT_REACH_SEM",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "AWS_ACCESS_KEY_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AZURE_CLIENT_SECRET",
        "SENTRY_DSN",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "SEM_CLOUD_TOKEN",
    ):
        monkeypatch.setenv(key, "secret")

    runner = SemRunner(
        cache_root,
        execute=lambda **_: CommandResult(0, "{}", ""),
        binary_lease=fixed_binary_lease,
    )
    env = runner.environment(scope)

    assert {
        "SEM_LOCAL": "1",
        "SEM_NO_NETWORK": "1",
        "SEM_NO_TELEMETRY": "1",
        "SEM_NO_UPDATE_CHECK": "1",
        "SEM_NO_AUTOWARM": "1",
        "SEM_NO_SIDECAR": "1",
        "DO_NOT_TRACK": "1",
    }.items() <= env.items()
    assert env["SEM_REPO"] == str(project.resolve())
    assert env["SEM_CACHE_DIR"] == str(cache_root / scope.project_id)
    assert env["PATH"] == "/synthetic/bin"
    assert env["LANG"] == "C.UTF-8"
    assert Path(env["SEM_CACHE_DIR"]).is_dir()
    runtime_root = cache_root / scope.project_id / "runtime"
    assert env["HOME"] == str(runtime_root / "home")
    assert env["USERPROFILE"] == str(runtime_root / "home")
    assert env["TMPDIR"] == str(runtime_root / "tmp")
    assert env["TEMP"] == str(runtime_root / "tmp")
    assert env["TMP"] == str(runtime_root / "tmp")
    assert env["XDG_CACHE_HOME"] == str(runtime_root / "home" / ".cache")
    assert env["XDG_CONFIG_HOME"] == str(runtime_root / "home" / ".config")
    assert env["XDG_DATA_HOME"] == str(
        runtime_root / "home" / ".local" / "share"
    )
    assert Path(env["HOME"]).is_dir()
    assert Path(env["TMPDIR"]).is_dir()
    assert "/synthetic/home" not in env.values()
    assert "/synthetic/profile" not in env.values()
    for key in (
        "SHOULD_NOT_REACH_SEM",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "AWS_ACCESS_KEY_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AZURE_CLIENT_SECRET",
        "SENTRY_DSN",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "SEM_CLOUD_TOKEN",
    ):
        assert key not in env


@pytest.mark.parametrize(
    ("diff_request", "mode_args"),
    [
        (DiffRequest("working"), []),
        (DiffRequest("staged"), ["--staged"]),
        (
            DiffRequest("commit", commit="abc123"),
            ["--commit", "abc123"],
        ),
        (
            DiffRequest("range", from_ref="HEAD~2", to_ref="HEAD"),
            ["--from", "HEAD~2", "--to", "HEAD"],
        ),
    ],
)
def test_diff_modes_use_exact_array_commands_and_git_pathspecs(
    tmp_path: Path,
    diff_request: DiffRequest,
    mode_args: list[str],
) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    scope = make_scope("ctx", "project", project, "src")
    calls: list[dict[str, object]] = []

    def execute(**kwargs: object) -> CommandResult:
        calls.append(kwargs)
        return CommandResult(0, json.dumps(EMPTY_DIFF), "")

    snapshot = SemRunner(
        tmp_path / "cache",
        execute=execute,
        binary_lease=fixed_binary_lease,
    ).diff(scope, diff_request, "fingerprint")

    assert snapshot.request == diff_request
    assert calls[0]["args"] == [
        "/opt/sem",
        "diff",
        *mode_args,
        "--format",
        "json",
        "--",
        "src",
    ]
    assert calls[0]["cwd"] == project.resolve()
    assert calls[0]["stdin"] is None


def test_runner_holds_verified_binary_lease_through_execution(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scope = make_scope("ctx", "project", project, ".")
    leased_binary = tmp_path / ".sem-lease-test"
    events: list[str] = []

    @contextmanager
    def binary_lease():
        events.append("acquired")
        try:
            yield leased_binary
        finally:
            events.append("released")

    def execute(**kwargs: object) -> CommandResult:
        assert events == ["acquired"]
        assert kwargs["args"] == [
            str(leased_binary),
            "diff",
            "--format",
            "json",
        ]
        return CommandResult(0, json.dumps(EMPTY_DIFF), "")

    SemRunner(
        tmp_path / "cache",
        execute=execute,
        binary_lease=binary_lease,
    ).diff(scope, DiffRequest("working"), "fingerprint")

    assert events == ["acquired", "released"]


def test_runner_cannot_be_constructed_without_binary_lease(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError, match="binary_lease"):
        SemRunner(tmp_path / "cache")  # type: ignore[call-arg]


def test_project_root_diff_still_excludes_agent_zero_metadata(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scope = make_scope("ctx", "project", project, ".")
    calls: list[dict[str, object]] = []

    def execute(**kwargs: object) -> CommandResult:
        calls.append(kwargs)
        return CommandResult(0, json.dumps(EMPTY_DIFF), "")

    SemRunner(
        tmp_path / "cache",
        execute=execute,
        binary_lease=fixed_binary_lease,
    ).diff(
        scope,
        DiffRequest("working"),
        "fingerprint",
    )

    assert calls[0]["args"] == [
        "/opt/sem",
        "diff",
        "--format",
        "json",
    ]


def test_working_diff_uses_stdin_for_untracked_tool_files(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(
        ["git", "init", "-q"],
        cwd=project,
        check=True,
        capture_output=True,
        shell=False,
    )
    subprocess.run(
        ["git", "config", "user.email", "synthetic@example.invalid"],
        cwd=project,
        check=True,
        capture_output=True,
        shell=False,
    )
    subprocess.run(
        ["git", "config", "user.name", "Synthetic Test User"],
        cwd=project,
        check=True,
        capture_output=True,
        shell=False,
    )
    (project / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "tracked.py"],
        cwd=project,
        check=True,
        capture_output=True,
        shell=False,
    )
    subprocess.run(
        ["git", "commit", "-qm", "base"],
        cwd=project,
        check=True,
        capture_output=True,
        shell=False,
    )
    (project / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    (project / "tool_created.py").write_text(
        "def generated():\n    return 3\n",
        encoding="utf-8",
    )
    scope = make_scope("ctx", "project", project, ".")
    calls: list[dict[str, object]] = []

    def execute(**kwargs: object) -> CommandResult:
        calls.append(kwargs)
        return CommandResult(0, json.dumps(EMPTY_DIFF), "")

    snapshot = SemRunner(
        tmp_path / "cache",
        execute=execute,
        binary_lease=fixed_binary_lease,
    ).diff(scope, DiffRequest("working"), "fingerprint")

    assert snapshot.request == DiffRequest("working")
    assert calls[0]["args"] == [
        "/opt/sem",
        "diff",
        "--stdin",
        "--format",
        "json",
    ]
    stdin = str(calls[0]["stdin"])
    assert "tool_created.py" in stdin
    assert "tracked.py" in stdin
    assert "generated" in stdin


def test_working_diff_excludes_oversized_tracked_file_without_failing(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(
        ["git", "init", "-q"],
        cwd=project,
        check=True,
        capture_output=True,
        shell=False,
    )
    subprocess.run(
        ["git", "config", "user.email", "synthetic@example.invalid"],
        cwd=project,
        check=True,
        capture_output=True,
        shell=False,
    )
    subprocess.run(
        ["git", "config", "user.name", "Synthetic Test User"],
        cwd=project,
        check=True,
        capture_output=True,
        shell=False,
    )
    source = project / "generated.py"
    source.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "generated.py"],
        cwd=project,
        check=True,
        capture_output=True,
        shell=False,
    )
    subprocess.run(
        ["git", "commit", "-qm", "base"],
        cwd=project,
        check=True,
        capture_output=True,
        shell=False,
    )
    source.write_bytes(b"x" * (MAX_CONTENT_BYTES + 1))
    scope = make_scope("ctx", "project", project, ".")
    calls: list[dict[str, object]] = []

    def execute(**kwargs: object) -> CommandResult:
        calls.append(kwargs)
        return CommandResult(0, json.dumps(EMPTY_DIFF), "")

    SemRunner(
        tmp_path / "cache",
        execute=execute,
        binary_lease=fixed_binary_lease,
    ).diff(scope, DiffRequest("working"), "fingerprint")

    assert calls[0]["args"] == [
        "/opt/sem",
        "diff",
        "--stdin",
        "--format",
        "json",
    ]
    assert calls[0]["stdin"] == "[]"


@pytest.mark.parametrize(
    "watched_name",
    [":scope", "src*", "src[1]"],
)
def test_magic_looking_watched_names_fail_closed_before_sem(
    tmp_path: Path,
    watched_name: str,
) -> None:
    project = tmp_path / "project"
    (project / watched_name).mkdir(parents=True)
    scope = make_scope("ctx", "project", project, watched_name)
    called = False

    def execute(**_kwargs: object) -> CommandResult:
        nonlocal called
        called = True
        return CommandResult(0, json.dumps(EMPTY_DIFF), "")

    with pytest.raises(SemCommandError, match="cannot be represented safely"):
        SemRunner(
            tmp_path / "cache",
            execute=execute,
            binary_lease=fixed_binary_lease,
        ).diff(
            scope,
            DiffRequest("working"),
            "fingerprint",
        )
    assert called is False


def sem_change(
    entity_id: str,
    file_path: str,
    *,
    change_type: str = "modified",
    old_file_path: str | None = None,
    source_marker: str = "public",
) -> dict[str, object]:
    return {
        "entityId": entity_id,
        "changeType": change_type,
        "entityType": "function",
        "entityName": "review",
        "startLine": 1,
        "endLine": 2,
        "oldStartLine": 1,
        "oldEndLine": 2,
        "oldEntityName": None,
        "filePath": file_path,
        "oldFilePath": old_file_path,
        "oldParentId": None,
        "beforeContent": f"def review():\n    return '{source_marker}-before'\n",
        "afterContent": f"def review():\n    return '{source_marker}-after'\n",
        "commitSha": None,
        "author": None,
        "structuralChange": True,
    }


def test_a0proj_changes_are_removed_and_summary_recounted_before_parse(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scope = make_scope("ctx", "project", project, ".")
    payload = {
        "summary": {
            "fileCount": 4,
            "added": 0,
            "modified": 3,
            "deleted": 0,
            "moved": 0,
            "renamed": 1,
            "reordered": 0,
            "binary": 1,
            "orphan": 0,
            "total": 4,
        },
        "changes": [
            sem_change(
                "src/public.py::function::review",
                "src/public.py",
            ),
            sem_change(
                ".a0proj/private.py::function::review",
                ".a0proj/private.py",
                source_marker="protected-secret",
            ),
            sem_change(
                "src/exported.py::function::review",
                "src/exported.py",
                change_type="renamed",
                old_file_path=".a0proj/exported.py",
                source_marker="protected-rename-secret",
            ),
        ],
        "binaryChanges": [
            {
                "changeType": "binary",
                "filePath": ".a0proj/secret.bin",
                "oldFilePath": None,
                "fileStatus": "modified",
            }
        ],
    }
    calls: list[dict[str, object]] = []

    def execute(**kwargs: object) -> CommandResult:
        calls.append(kwargs)
        return CommandResult(0, json.dumps(payload), "")

    snapshot = SemRunner(
        tmp_path / "cache",
        execute=execute,
        binary_lease=fixed_binary_lease,
    ).diff(scope, DiffRequest("working"), "fingerprint")

    assert calls[0]["args"][-2:] == ["--format", "json"]
    assert snapshot.summary.file_count == 1
    assert snapshot.summary.modified == 1
    assert snapshot.summary.renamed == 0
    assert snapshot.summary.binary == 0
    assert snapshot.summary.total == 1
    assert [change.entity.file_path for change in snapshot.changes] == [
        "src/public.py"
    ]
    encoded_details = json.dumps(
        {
            entity_id: detail.before_content
            for entity_id, detail in snapshot.details.items()
        }
    )
    assert "protected-secret" not in encoded_details
    assert "protected-rename-secret" not in encoded_details


def test_a0proj_recount_preserves_edited_move_modified_overlap(
    tmp_path: Path,
) -> None:
    payload = {
        "summary": {
            "fileCount": 2,
            "added": 0,
            "modified": 2,
            "deleted": 0,
            "moved": 1,
            "renamed": 0,
            "reordered": 0,
            "binary": 0,
            "orphan": 0,
            "total": 2,
        },
        "changes": [
            sem_change(
                "src/new.py::function::review",
                "src/new.py",
                change_type="moved",
                old_file_path="src/old.py",
            ),
            sem_change(
                ".a0proj/private.py::function::review",
                ".a0proj/private.py",
                source_marker="protected-secret",
            ),
        ],
        "binaryChanges": [],
    }
    runner, scope = runner_for_payload(tmp_path, payload)

    snapshot = runner.diff(
        scope,  # type: ignore[arg-type]
        DiffRequest("working"),
        "fingerprint",
    )

    assert snapshot.summary.modified == 1
    assert snapshot.summary.moved == 1
    assert snapshot.summary.total == 1


def protected_semantic_payload() -> dict[str, object]:
    return {
        "summary": {
            "fileCount": 1,
            "added": 0,
            "modified": 1,
            "deleted": 0,
            "moved": 0,
            "renamed": 0,
            "reordered": 0,
            "binary": 0,
            "orphan": 0,
            "total": 1,
        },
        "changes": [
            sem_change(
                ".a0proj/private.py::function::review",
                ".a0proj/private.py",
                source_marker="protected-secret",
            )
        ],
        "binaryChanges": [],
    }


def test_protected_validation_alias_cannot_collapse_real_file_count(
    tmp_path: Path,
) -> None:
    payload = protected_semantic_payload()
    summary = payload["summary"]
    changes = payload["changes"]
    assert isinstance(summary, dict)
    assert isinstance(changes, list)
    changes.insert(
        0,
        sem_change(
            "a0proj_/private.py::function::review",
            "a0proj_/private.py",
        ),
    )
    summary["fileCount"] = 2
    summary["modified"] = 2
    summary["total"] = 2
    runner, scope = runner_for_payload(tmp_path, payload)

    snapshot = runner.diff(
        scope,  # type: ignore[arg-type]
        DiffRequest("working"),
        "fingerprint",
    )

    assert snapshot.summary.file_count == 1
    assert [change.entity.file_path for change in snapshot.changes] == [
        "a0proj_/private.py"
    ]


def runner_for_payload(
    tmp_path: Path,
    payload: object,
) -> tuple[SemRunner, object]:
    project = tmp_path / "project"
    project.mkdir()
    scope = make_scope("ctx", "project", project, ".")
    runner = SemRunner(
        tmp_path / "cache",
        execute=lambda **_: CommandResult(0, json.dumps(payload), ""),
        binary_lease=fixed_binary_lease,
    )
    return runner, scope


def test_malformed_protected_semantic_record_is_rejected_before_filter(
    tmp_path: Path,
) -> None:
    payload = protected_semantic_payload()
    changes = payload["changes"]
    assert isinstance(changes, list)
    record = changes[0]
    assert isinstance(record, dict)
    del record["entityName"]
    runner, scope = runner_for_payload(tmp_path, payload)

    with pytest.raises(SemParseError, match="entityName is required"):
        runner.diff(scope, DiffRequest("working"), "fingerprint")  # type: ignore[arg-type]


def test_malformed_protected_binary_record_is_rejected_before_filter(
    tmp_path: Path,
) -> None:
    payload = {
        "summary": {
            "fileCount": 1,
            "added": 0,
            "modified": 0,
            "deleted": 0,
            "moved": 0,
            "renamed": 0,
            "reordered": 0,
            "binary": 1,
            "orphan": 0,
            "total": 1,
        },
        "changes": [],
        "binaryChanges": [
            {
                "changeType": "binary",
                "filePath": ".a0proj/secret.bin",
                "oldFilePath": None,
            }
        ],
    }
    runner, scope = runner_for_payload(tmp_path, payload)

    with pytest.raises(SemParseError, match="fileStatus is required"):
        runner.diff(scope, DiffRequest("working"), "fingerprint")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("summary_mutation", "message"),
    [
        (("remove", "total", None), r"summary\.total is required"),
        (("replace", "modified", "1"), r"summary\.modified"),
        (
            ("replace", "total", 2),
            r"summary\.total does not match",
        ),
        (
            ("replace", "fileCount", 2),
            r"summary\.fileCount does not match",
        ),
    ],
)
def test_original_summary_is_validated_before_protected_recount(
    tmp_path: Path,
    summary_mutation: tuple[str, str, object],
    message: str,
) -> None:
    payload = protected_semantic_payload()
    summary = payload["summary"]
    assert isinstance(summary, dict)
    action, field, value = summary_mutation
    if action == "remove":
        del summary[field]
    else:
        summary[field] = value
    runner, scope = runner_for_payload(tmp_path, payload)

    with pytest.raises(SemParseError, match=message):
        runner.diff(scope, DiffRequest("working"), "fingerprint")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "diff_request",
    [
        DiffRequest("commit"),
        DiffRequest("commit", commit="\x00"),
        DiffRequest("commit", commit="x" * 201),
        DiffRequest("commit", commit="--help"),
        DiffRequest("range", from_ref="", to_ref="HEAD"),
        DiffRequest("range", from_ref="HEAD~1", to_ref="\nHEAD"),
        DiffRequest("range", from_ref="x" * 201, to_ref="HEAD"),
        DiffRequest("stdin"),
    ],
)
def test_invalid_or_in_memory_requests_never_reach_diff_process(
    tmp_path: Path,
    diff_request: DiffRequest,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scope = make_scope("ctx", "project", project, ".")
    called = False

    def execute(**_kwargs: object) -> CommandResult:
        nonlocal called
        called = True
        return CommandResult(0, json.dumps(EMPTY_DIFF), "")

    runner = SemRunner(
        tmp_path / "cache",
        execute=execute,
        binary_lease=fixed_binary_lease,
    )
    with pytest.raises(SemCommandError):
        runner.diff(scope, diff_request, "fingerprint")
    assert called is False


def test_nonzero_exit_uses_bounded_control_free_error(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scope = make_scope("ctx", "project", project, ".")
    runner = SemRunner(
        tmp_path / "cache",
        execute=lambda **_: CommandResult(
            2,
            "",
            "\x1b[31mbad\nref\x1b[0m" + ("x" * 2_000),
        ),
        binary_lease=fixed_binary_lease,
    )

    with pytest.raises(SemCommandError) as captured:
        runner.diff(scope, DiffRequest("working"), "fingerprint")

    message = str(captured.value)
    assert "exit 2" in message
    assert "bad ref" in message
    assert "\x1b" not in message
    assert "\n" not in message
    assert len(message) <= 560


def test_json_is_parsed_only_after_success_and_must_be_strict(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scope = make_scope("ctx", "project", project, ".")

    failed = SemRunner(
        tmp_path / "cache-a",
        execute=lambda **_: CommandResult(1, "{not-json", "failed"),
        binary_lease=fixed_binary_lease,
    )
    with pytest.raises(SemCommandError, match="exit 1"):
        failed.diff(scope, DiffRequest("working"), "fingerprint")

    invalid = SemRunner(
        tmp_path / "cache-b",
        execute=lambda **_: CommandResult(0, "{not-json", ""),
        binary_lease=fixed_binary_lease,
    )
    with pytest.raises(SemCommandError, match="invalid JSON"):
        invalid.diff(scope, DiffRequest("working"), "fingerprint")

    duplicate = SemRunner(
        tmp_path / "cache-c",
        execute=lambda **_: CommandResult(0, '{"summary":{},"summary":{}}', ""),
        binary_lease=fixed_binary_lease,
    )
    with pytest.raises(SemCommandError, match="invalid JSON"):
        duplicate.diff(scope, DiffRequest("working"), "fingerprint")


class RecordingPipe(io.BytesIO):
    def __init__(self, initial: bytes = b"") -> None:
        super().__init__(initial)
        self.close_called = False

    def close(self) -> None:
        self.close_called = True


class FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        completed: bool = False,
    ) -> None:
        self.stdout = RecordingPipe(stdout)
        self.stderr = RecordingPipe(stderr)
        self.stdin = RecordingPipe()
        self.returncode: int | None = 0 if completed else None
        self.killed = False
        self.reaped = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            self.reaped = True
            return self.returncode
        if not self.killed:
            raise subprocess.TimeoutExpired(["sem"], timeout or 0)
        self.returncode = -9
        self.reaped = True
        return self.returncode

    def kill(self) -> None:
        self.killed = True


@pytest.mark.parametrize(
    ("stdout", "stderr", "stream_name"),
    [
        (b"x" * 11, b"", "stdout"),
        (b"", b"x" * 11, "stderr"),
    ],
)
def test_each_output_stream_overflow_kills_and_reaps(
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
    stderr: bytes,
    stream_name: str,
) -> None:
    process = FakeProcess(stdout=stdout, stderr=stderr)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(SemCommandError, match=stream_name):
        run_bounded(
            args=["sem", "diff"],
            cwd=Path("/tmp"),
            env={},
            timeout=1,
            stdout_limit=10,
            stderr_limit=10,
        )

    assert process.killed is True
    assert process.reaped is True
    assert process.stdin.close_called is True
    assert process.stdout.close_called is True
    assert process.stderr.close_called is True


def test_timeout_kills_and_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(SemCommandError, match="timed out"):
        run_bounded(
            args=["sem", "diff"],
            cwd=Path("/tmp"),
            env={},
            timeout=0.01,
        )

    assert process.killed is True
    assert process.reaped is True


def test_run_bounded_passes_array_without_shell_and_writes_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(stdout=b"{}", completed=True)
    captured: dict[str, Any] = {}

    def popen(args: list[str], **kwargs: object) -> FakeProcess:
        captured["args"] = args
        captured.update(kwargs)
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    result = run_bounded(
        args=["sem", "diff", "--stdin"],
        cwd=Path("/tmp"),
        env={"SEM_LOCAL": "1"},
        timeout=1,
        stdin='[{"filePath":"a.py"}]',
    )

    assert result == CommandResult(0, "{}", "")
    assert captured["args"] == ["sem", "diff", "--stdin"]
    assert captured["shell"] is False
    if os.name == "nt":
        assert captured["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert captured["start_new_session"] is True
    assert process.stdin.getvalue() == b'[{"filePath":"a.py"}]'


def test_run_bounded_drains_stdout_and_stderr_simultaneously(
    tmp_path: Path,
) -> None:
    size = 512 * 1024
    script = (
        "import os\n"
        f"os.write(1, b'o' * {size})\n"
        f"os.write(2, b'e' * {size})\n"
    )

    result = run_bounded(
        args=[sys.executable, "-c", script],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=5,
        stdout_limit=size,
        stderr_limit=size,
    )

    assert result.returncode == 0
    assert len(result.stdout) == size
    assert len(result.stderr) == size


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_run_bounded_kills_descendant_that_holds_output_pipe(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "child.pid"
    script = (
        "import pathlib, subprocess, sys\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "    stdout=sys.stdout,\n"
        "    stderr=sys.stderr,\n"
        ")\n"
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))\n"
        "print('{}')\n"
    )
    started = time.monotonic()

    with pytest.raises(SemCommandError, match="I/O workers"):
        run_bounded(
            args=[sys.executable, "-c", script],
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            timeout=5,
        )

    assert time.monotonic() - started < 4
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        proc_stat = Path(f"/proc/{child_pid}/stat")
        if proc_stat.exists():
            fields = proc_stat.read_text(encoding="utf-8").split()
            if len(fields) > 2 and fields[2] == "Z":
                break
        time.sleep(0.02)
    else:
        pytest.fail("descendant remained alive after bounded process cleanup")


def test_run_bounded_rejects_invalid_utf8_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(stdout=b"\xff", completed=True)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(SemCommandError, match="UTF-8"):
        run_bounded(
            args=["sem", "diff"],
            cwd=Path("/tmp"),
            env={},
            timeout=1,
        )

    assert process.stdin.close_called is True
    assert process.stdout.close_called is True
    assert process.stderr.close_called is True


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../secret.py",
        "/secret.py",
        ".a0proj/review.py",
        "src/\x00secret.py",
        "src\\secret.py",
        "C:/secret.py",
    ],
)
def test_manual_files_reject_unsafe_paths(unsafe_path: str) -> None:
    with pytest.raises(SemCommandError, match="outside|POSIX|control"):
        normalize_manual_files(
            [
                {
                    "filePath": unsafe_path,
                    "status": "modified",
                    "beforeContent": "",
                    "afterContent": "",
                }
            ]
        )


@pytest.mark.parametrize("count", [0, 21])
def test_manual_file_count_is_bounded(count: int) -> None:
    with pytest.raises(SemCommandError, match="1 to 20"):
        normalize_manual_files(
            [
                {
                    "filePath": f"file-{index}.py",
                    "status": "added",
                    "afterContent": "",
                }
                for index in range(count)
            ]
        )


def test_manual_files_reject_unknown_fields_and_invalid_status() -> None:
    with pytest.raises(SemCommandError, match="unknown fields"):
        normalize_manual_files(
            [
                {
                    "filePath": "a.py",
                    "status": "modified",
                    "futureField": True,
                }
            ]
        )
    with pytest.raises(SemCommandError, match="status is invalid"):
        normalize_manual_files(
            [{"filePath": "a.py", "status": True}]
        )
    with pytest.raises(SemCommandError, match="oldFilePath"):
        normalize_manual_files(
            [
                {
                    "filePath": "a.py",
                    "oldFilePath": ["old.py"],
                    "status": "renamed",
                }
            ]
        )


def test_manual_payload_is_canonical_and_matches_pinned_stdin_schema() -> None:
    payload = canonical_manual_payload(
        [
            {
                "status": " Modified ",
                "afterContent": "value = 2\n",
                "filePath": "src/sample.py",
                "beforeContent": "value = 1\n",
            }
        ]
    )

    assert payload == (
        '[{"afterContent":"value = 2\\n",'
        '"beforeContent":"value = 1\\n",'
        '"filePath":"src/sample.py","status":"modified"}]'
    )
    assert json.loads(payload) == [
        {
            "afterContent": "value = 2\n",
            "beforeContent": "value = 1\n",
            "filePath": "src/sample.py",
            "status": "modified",
        }
    ]


def test_manual_payload_accepts_old_file_path_for_rename() -> None:
    normalized = normalize_manual_files(
        [
            {
                "filePath": "src/new.py",
                "oldFilePath": "src/old.py",
                "status": "renamed",
                "beforeContent": "old\n",
                "afterContent": "new\n",
            }
        ]
    )
    assert normalized[0]["oldFilePath"] == "src/old.py"


def test_manual_payload_enforces_utf8_json_byte_limit() -> None:
    with pytest.raises(SemCommandError, match="2 MiB"):
        normalize_manual_files(
            [
                {
                    "filePath": "large.py",
                    "status": "added",
                    "afterContent": "é" * (MAX_MANUAL_BYTES // 2),
                }
            ]
        )


def test_manual_diff_uses_bounded_stdin_without_working_mode(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scope = make_scope("ctx", "project", project, ".")
    calls: list[dict[str, object]] = []

    def execute(**kwargs: object) -> CommandResult:
        calls.append(kwargs)
        return CommandResult(0, json.dumps(EMPTY_DIFF), "")

    snapshot = SemRunner(
        tmp_path / "cache",
        execute=execute,
        binary_lease=fixed_binary_lease,
    ).manual_diff(
        scope,
        [
            {
                "filePath": "sample.py",
                "status": "modified",
                "beforeContent": "value = 1\n",
                "afterContent": "value = 2\n",
            }
        ],
        "manual-fingerprint",
    )

    assert snapshot.request == DiffRequest("stdin")
    assert snapshot.fingerprint == "manual-fingerprint"
    assert calls[0]["args"] == [
        "/opt/sem",
        "diff",
        "--stdin",
        "--format",
        "json",
    ]
    assert calls[0]["stdin"] == canonical_manual_payload(
        [
            {
                "filePath": "sample.py",
                "status": "modified",
                "beforeContent": "value = 1\n",
                "afterContent": "value = 2\n",
            }
        ]
    )


def test_manual_diff_never_targets_real_home_or_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    real_home = tmp_path / "real-home"
    real_temp = tmp_path / "real-temp"
    real_home.mkdir()
    real_temp.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("USERPROFILE", str(real_home))
    monkeypatch.setenv("TMPDIR", str(real_temp))
    cache_root = tmp_path / "plugin-cache"
    scope = make_scope("ctx", "project", project, ".")
    calls: list[dict[str, object]] = []

    def execute(**kwargs: object) -> CommandResult:
        calls.append(kwargs)
        execution_env = kwargs["env"]
        assert isinstance(execution_env, dict)
        stats_path = (
            Path(str(execution_env["HOME"])) / ".sem" / "stats.json"
        )
        stats_path.parent.mkdir(parents=True)
        stats_path.write_text("{}\n", encoding="utf-8")
        return CommandResult(0, json.dumps(EMPTY_DIFF), "")

    SemRunner(
        cache_root,
        execute=execute,
        binary_lease=fixed_binary_lease,
    ).manual_diff(
        scope,
        [
            {
                "filePath": "sample.py",
                "status": "added",
                "afterContent": "value = 1\n",
            }
        ],
        "manual-fingerprint",
    )

    env = calls[0]["env"]
    assert isinstance(env, dict)
    runtime_root = cache_root / scope.project_id / "runtime"
    assert env["HOME"] == str(runtime_root / "home")
    assert env["USERPROFILE"] == str(runtime_root / "home")
    assert env["TMPDIR"] == str(runtime_root / "tmp")
    assert str(real_home) not in env.values()
    assert str(real_temp) not in env.values()
    assert not (real_home / ".sem" / "stats.json").exists()
    assert (
        runtime_root / "home" / ".sem" / "stats.json"
    ).read_text(encoding="utf-8") == "{}\n"


def test_context_and_impact_use_stable_entity_metadata(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source = project / "src" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    scope = make_scope("ctx", "project", project, "src")
    calls: list[dict[str, object]] = []

    def execute(**kwargs: object) -> CommandResult:
        calls.append(kwargs)
        return CommandResult(0, '{"ok":true}', "")

    runner = SemRunner(
        tmp_path / "cache",
        execute=execute,
        binary_lease=fixed_binary_lease,
    )
    entity = EntityRef(
        entity_id="src/module.py::function::run",
        entity_name="run",
        entity_type="function",
        file_path="src/module.py",
    )

    assert runner.context(scope, entity, 4_000) == {"ok": True}
    assert runner.impact(scope, entity) == {"ok": True}
    assert calls[0]["args"] == [
        "/opt/sem",
        "context",
        "--file",
        "src/module.py",
        "--budget",
        "4000",
        "--hops",
        "1",
        "--json",
        "--",
        entity.entity_name,
    ]
    assert calls[1]["args"] == [
        "/opt/sem",
        "impact",
        "--file",
        "src/module.py",
        "--depth",
        "2",
        "--json",
        "--",
        entity.entity_name,
    ]


def test_entity_queries_ignore_diff_only_entity_id_suffix(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source = project / "module.py"
    project.mkdir()
    source.write_text("def authorize():\n    return True\n", encoding="utf-8")
    scope = make_scope("ctx", "project", project, ".")
    calls: list[dict[str, object]] = []

    def execute(**kwargs: object) -> CommandResult:
        calls.append(kwargs)
        return CommandResult(0, '{"ok":true}', "")

    runner = SemRunner(
        tmp_path / "cache",
        execute=execute,
        binary_lease=fixed_binary_lease,
    )
    entity = EntityRef(
        entity_id="module.py::function::authorize@added@L1-2",
        entity_name="authorize",
        entity_type="function",
        file_path="module.py",
    )

    assert runner.context(scope, entity, 4_000) == {"ok": True}
    assert runner.impact(scope, entity) == {"ok": True}
    assert all(
        "--entity-id" not in list(call["args"])
        for call in calls
    )
    assert all(
        list(call["args"])[-1] == "authorize"
        for call in calls
    )


@pytest.mark.parametrize("budget", [999, 32_001, True])
def test_context_token_budget_is_bounded(
    tmp_path: Path,
    budget: object,
) -> None:
    project = tmp_path / "project"
    source = project / "source.py"
    project.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    scope = make_scope("ctx", "project", project, ".")
    runner = SemRunner(
        tmp_path / "cache",
        binary_lease=fixed_binary_lease,
    )
    entity = EntityRef("id", "name", "variable", "source.py")

    with pytest.raises(SemCommandError, match="token budget"):
        runner.context(scope, entity, budget)  # type: ignore[arg-type]


def test_entity_query_rejects_paths_outside_watched_scope(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    outside = project / "outside.py"
    outside.write_text("secret = 1\n", encoding="utf-8")
    scope = make_scope("ctx", "project", project, "src")
    runner = SemRunner(
        tmp_path / "cache",
        binary_lease=fixed_binary_lease,
    )
    entity = EntityRef("id", "name", "variable", "outside.py")

    with pytest.raises(SemCommandError, match="outside review scope"):
        runner.impact(scope, entity)


def test_query_result_must_be_a_json_object(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "source.py"
    project.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    scope = make_scope("ctx", "project", project, ".")
    runner = SemRunner(
        tmp_path / "cache",
        execute=lambda **_: CommandResult(0, "[]", ""),
        binary_lease=fixed_binary_lease,
    )
    entity = EntityRef("id", "name", "variable", "source.py")

    with pytest.raises(SemCommandError, match="JSON object"):
        runner.impact(scope, entity)


def test_query_result_is_bounded_before_api_storage(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "source.py"
    project.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    scope = make_scope("ctx", "project", project, ".")
    runner = SemRunner(
        tmp_path / "cache",
        execute=lambda **_: CommandResult(0, json.dumps({"source": "x" * 250_000}), ""),
        binary_lease=fixed_binary_lease,
    )
    entity = EntityRef("id", "name", "variable", "source.py")

    with pytest.raises(SemCommandError, match="bounded response"):
        runner.impact(scope, entity)
