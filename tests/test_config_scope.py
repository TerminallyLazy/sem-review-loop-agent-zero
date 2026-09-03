from __future__ import annotations

import hashlib
import io
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

import usr.plugins.sem_review_loop.helpers.fingerprints as fingerprints_module
from usr.plugins.sem_review_loop.helpers.config import (
    config_for_agent,
    parse_config,
)
from usr.plugins.sem_review_loop.helpers.fingerprints import (
    FingerprintError,
    diff_fingerprint,
    working_fingerprint,
)
from usr.plugins.sem_review_loop.helpers.sem_types import MAX_CONTENT_BYTES
from usr.plugins.sem_review_loop.helpers.working_tree import (
    collect_working_files,
)
from usr.plugins.sem_review_loop.helpers.project_scope import (
    ProjectScopeError,
    literal_watched_pathspec,
    make_scope,
)
from usr.plugins.sem_review_loop.helpers.sem_types import DiffRequest


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        shell=False,
    )


def git_output(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        shell=False,
        text=True,
    )
    return result.stdout.strip()


def initialized_repo(repo: Path) -> Path:
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "synthetic@example.invalid")
    git(repo, "config", "user.name", "Synthetic Test User")
    source = repo / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    git(repo, "add", "app.py")
    git(repo, "commit", "-qm", "base")
    return source


def test_parse_config_defaults_and_bounds() -> None:
    defaults = parse_config(None)
    assert defaults.watched_subdirectory == "."
    assert defaults.automatic_refresh is True
    assert defaults.debounce_ms == 400
    assert defaults.automatic_repair is False
    assert defaults.max_repair_cycles == 2
    assert defaults.custom_sem_binary == ""
    assert defaults.context_token_budget == 8000
    assert defaults.working_tree_payload_mb == 256

    bounded = parse_config(
        {
            "watched_subdirectory": " src ",
            "automatic_refresh": "false",
            "debounce_ms": 1,
            "automatic_repair": "YES",
            "max_repair_cycles": 99,
            "custom_sem_binary": " /tmp/sem ",
            "context_token_budget": 100,
            "working_tree_payload_mb": 1,
        }
    )
    assert bounded.watched_subdirectory == "src"
    assert bounded.automatic_refresh is False
    assert bounded.debounce_ms == 100
    assert bounded.automatic_repair is True
    assert bounded.max_repair_cycles == 3
    assert bounded.custom_sem_binary == "/tmp/sem"
    assert bounded.context_token_budget == 1000
    assert bounded.working_tree_payload_mb == 16

    upper_bounds = parse_config(
        {
            "debounce_ms": 50_000,
            "max_repair_cycles": -1,
            "context_token_budget": 99_000,
            "working_tree_payload_mb": 10_000,
        }
    )
    assert upper_bounds.debounce_ms == 5000
    assert upper_bounds.max_repair_cycles == 1
    assert upper_bounds.context_token_budget == 32000
    assert upper_bounds.working_tree_payload_mb == 1024


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        (" 1 ", True),
        ("true", True),
        ("Yes", True),
        ("ON", True),
        (" 0 ", False),
        ("false", False),
        ("No", False),
        ("OFF", False),
    ],
)
def test_parse_config_accepts_only_friendly_booleans(
    value: object,
    expected: bool,
) -> None:
    config = parse_config(
        {
            "automatic_refresh": value,
            "automatic_repair": value,
        }
    )
    assert config.automatic_refresh is expected
    assert config.automatic_repair is expected


def test_parse_config_invalid_booleans_use_field_defaults() -> None:
    config = parse_config(
        {
            "automatic_refresh": "sometimes",
            "automatic_repair": 2,
        }
    )
    assert config.automatic_refresh is True
    assert config.automatic_repair is False


def test_parse_config_non_finite_numbers_use_field_defaults() -> None:
    config = parse_config(
        {
            "debounce_ms": float("inf"),
            "max_repair_cycles": float("-inf"),
            "context_token_budget": float("nan"),
            "working_tree_payload_mb": float("inf"),
        }
    )
    assert config.debounce_ms == 400
    assert config.max_repair_cycles == 2
    assert config.context_token_budget == 8000
    assert config.working_tree_payload_mb == 256


def test_config_for_agent_uses_agent_scoped_plugin_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = object()
    calls: list[tuple[str, object]] = []

    def fake_get_plugin_config(name: str, *, agent: object) -> dict[str, object]:
        calls.append((name, agent))
        return {"debounce_ms": 875}

    fake_plugins = ModuleType("helpers.plugins")
    fake_plugins.get_plugin_config = fake_get_plugin_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "helpers.plugins", fake_plugins)
    assert config_for_agent(agent).debounce_ms == 875
    assert calls == [("sem_review_loop", agent)]


def test_watched_path_cannot_escape_project(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    with pytest.raises(ProjectScopeError, match="escape"):
        make_scope("ctx", "project", root, "../outside")


def test_watched_symlink_cannot_escape_project(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectScopeError, match="escape"):
        make_scope("ctx", "project", root, "linked")


def test_project_id_is_stable_and_does_not_disclose_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    watched = root / "src"
    watched.mkdir(parents=True)

    first = make_scope("ctx-a", "project", root, ".")
    second = make_scope("ctx-b", "project", root, "src")

    assert first.project_id == second.project_id
    assert len(first.project_id) == 24
    assert all(character in "0123456789abcdef" for character in first.project_id)
    assert str(root) not in first.project_id
    assert first.watched_relative == "."
    assert second.watched_relative == "src"


def test_missing_scope_paths_raise_project_scope_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ProjectScopeError, match="does not exist"):
        make_scope("ctx", "project", missing, ".")

    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(ProjectScopeError, match="does not exist"):
        make_scope("ctx", "project", root, "missing")


def test_working_fingerprint_tracks_exact_diff_bytes(tmp_path: Path) -> None:
    source = initialized_repo(tmp_path)
    source.write_text("value = 2\n", encoding="utf-8")
    first = working_fingerprint(tmp_path, ".")
    source.write_text("value = 3\n", encoding="utf-8")
    second = working_fingerprint(tmp_path, ".")
    assert first != second


def test_working_fingerprint_tracks_untracked_tool_files_and_content(
    tmp_path: Path,
) -> None:
    initialized_repo(tmp_path)
    tool_file = tmp_path / "tool_created.py"
    tool_file.write_text("value = 1\n", encoding="utf-8")

    first = working_fingerprint(tmp_path, ".")
    tool_file.write_text("value = 2\n", encoding="utf-8")
    second = working_fingerprint(tmp_path, ".")

    assert first != second
    records = collect_working_files(tmp_path, ".")
    assert [record["filePath"] for record in records] == ["tool_created.py"]
    assert records[0]["status"] == "added"
    assert records[0]["beforeContent"] is None
    assert records[0]["afterContent"] == "value = 2\n"


def test_working_files_are_not_limited_to_twenty_changes(tmp_path: Path) -> None:
    initialized_repo(tmp_path)
    for index in range(25):
        (tmp_path / f"tool_created_{index}.py").write_text(
            f"value = {index}\n",
            encoding="utf-8",
        )

    records = collect_working_files(tmp_path, ".")

    assert len(records) == 25
    assert records[0]["filePath"] == "tool_created_0.py"
    assert records[-1]["filePath"] == "tool_created_9.py"


def test_oversized_working_file_does_not_block_other_changes(
    tmp_path: Path,
) -> None:
    initialized_repo(tmp_path)
    ordinary = tmp_path / "ordinary.py"
    ordinary.write_text("value = 2\n", encoding="utf-8")
    oversized = tmp_path / "generated.txt"
    oversized.write_bytes(b"x" * (MAX_CONTENT_BYTES + 1))

    records = collect_working_files(tmp_path, ".")

    assert records == [
        {
            "filePath": "ordinary.py",
            "status": "added",
            "beforeContent": None,
            "afterContent": "value = 2\n",
        }
    ]


def test_colon_prefixed_watched_directory_is_a_literal_scope(
    tmp_path: Path,
) -> None:
    source = initialized_repo(tmp_path)
    watched = tmp_path / ":watched"
    watched.mkdir()
    inside = watched / "inside.py"
    inside.write_text("value = 1\n", encoding="utf-8")
    git(
        tmp_path,
        "add",
        "--",
        ":(literal):watched/inside.py",
    )
    git(tmp_path, "commit", "-qm", "add colon-prefixed scope")

    scope = make_scope("ctx", "project", tmp_path, ":watched")
    assert literal_watched_pathspec(scope.watched_relative) == (
        ":(literal):watched"
    )
    clean = working_fingerprint(tmp_path, scope.watched_relative)

    source.write_text("value = 2\n", encoding="utf-8")
    assert working_fingerprint(tmp_path, scope.watched_relative) == clean

    inside.write_text("value = 2\n", encoding="utf-8")
    assert working_fingerprint(tmp_path, scope.watched_relative) != clean


def test_fingerprint_excludes_tracked_agent_zero_metadata(
    tmp_path: Path,
) -> None:
    initialized_repo(tmp_path)
    metadata = tmp_path / ".a0proj" / "mcp_servers.json"
    metadata.parent.mkdir()
    metadata.write_text('{"version": 1}\n', encoding="utf-8")
    git(tmp_path, "add", ".a0proj/mcp_servers.json")
    git(tmp_path, "commit", "-qm", "track metadata")

    before = working_fingerprint(tmp_path, ".")
    metadata.write_text('{"version": 2}\n', encoding="utf-8")
    assert working_fingerprint(tmp_path, ".") == before


def test_working_and_staged_fingerprints_have_distinct_identities(
    tmp_path: Path,
) -> None:
    source = initialized_repo(tmp_path)
    source.write_text("value = 2\n", encoding="utf-8")
    git(tmp_path, "add", "app.py")

    working = diff_fingerprint(tmp_path, ".", DiffRequest("working"))
    staged = diff_fingerprint(tmp_path, ".", DiffRequest("staged"))
    assert working != staged


def test_commit_ref_identity_and_resolved_content_affect_fingerprint(
    tmp_path: Path,
) -> None:
    source = initialized_repo(tmp_path)
    first_commit = git_output(tmp_path, "rev-parse", "HEAD")
    source.write_text("value = 2\n", encoding="utf-8")
    git(tmp_path, "add", "app.py")
    git(tmp_path, "commit", "-qm", "second")
    second_commit = git_output(tmp_path, "rev-parse", "HEAD")

    git(tmp_path, "branch", "review-target", first_commit)
    first_content = diff_fingerprint(
        tmp_path,
        ".",
        DiffRequest("commit", commit="review-target"),
    )
    git(tmp_path, "branch", "-f", "review-target", second_commit)
    second_content = diff_fingerprint(
        tmp_path,
        ".",
        DiffRequest("commit", commit="review-target"),
    )
    same_content_different_identity = diff_fingerprint(
        tmp_path,
        ".",
        DiffRequest("commit", commit=second_commit),
    )

    assert first_content != second_content
    assert second_content != same_content_different_identity


def test_commit_fingerprint_uses_one_immutable_ref_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = initialized_repo(tmp_path)
    first_commit = git_output(tmp_path, "rev-parse", "HEAD")
    source.write_text("value = 2\n", encoding="utf-8")
    git(tmp_path, "add", "app.py")
    git(tmp_path, "commit", "-qm", "second")
    second_commit = git_output(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "branch", "review-target", first_commit)
    request = DiffRequest("commit", commit="review-target")
    stable = diff_fingerprint(tmp_path, ".", request)

    original_stream = fingerprints_module._stream_git_stdout
    commands: list[tuple[str, ...]] = []
    moved = False

    def move_ref_before_diff(
        root: Path,
        args: list[str],
        consume_stdout: Callable[[bytes], None],
        *,
        max_stdout_bytes: int | None = None,
    ) -> None:
        nonlocal moved
        commands.append(tuple(args))
        if args[0] == "show" and not moved:
            moved = True
            git(tmp_path, "branch", "-f", "review-target", second_commit)
        original_stream(
            root,
            args,
            consume_stdout,
            max_stdout_bytes=max_stdout_bytes,
        )

    monkeypatch.setattr(
        fingerprints_module,
        "_stream_git_stdout",
        move_ref_before_diff,
    )
    raced = diff_fingerprint(tmp_path, ".", request)

    assert moved is True
    assert git_output(tmp_path, "rev-parse", "review-target") == second_commit
    assert raced == stable
    show_command = next(command for command in commands if command[0] == "show")
    assert first_commit in show_command
    assert "review-target" not in show_command


def test_range_ref_identities_and_resolved_content_affect_fingerprint(
    tmp_path: Path,
) -> None:
    source = initialized_repo(tmp_path)
    first_commit = git_output(tmp_path, "rev-parse", "HEAD")
    source.write_text("value = 2\n", encoding="utf-8")
    git(tmp_path, "add", "app.py")
    git(tmp_path, "commit", "-qm", "second")
    second_commit = git_output(tmp_path, "rev-parse", "HEAD")
    source.write_text("value = 3\n", encoding="utf-8")
    git(tmp_path, "add", "app.py")
    git(tmp_path, "commit", "-qm", "third")
    third_commit = git_output(tmp_path, "rev-parse", "HEAD")

    git(tmp_path, "branch", "review-from", first_commit)
    git(tmp_path, "branch", "review-to", second_commit)
    first_range = diff_fingerprint(
        tmp_path,
        ".",
        DiffRequest(
            "range",
            from_ref="review-from",
            to_ref="review-to",
        ),
    )

    git(tmp_path, "branch", "-f", "review-to", third_commit)
    changed_to_ref_content = diff_fingerprint(
        tmp_path,
        ".",
        DiffRequest(
            "range",
            from_ref="review-from",
            to_ref="review-to",
        ),
    )

    git(tmp_path, "branch", "-f", "review-from", second_commit)
    changed_from_ref_content = diff_fingerprint(
        tmp_path,
        ".",
        DiffRequest(
            "range",
            from_ref="review-from",
            to_ref="review-to",
        ),
    )
    same_content_different_identities = diff_fingerprint(
        tmp_path,
        ".",
        DiffRequest(
            "range",
            from_ref=second_commit,
            to_ref=third_commit,
        ),
    )

    assert first_range != changed_to_ref_content
    assert changed_to_ref_content != changed_from_ref_content
    assert changed_from_ref_content != same_content_different_identities


def test_git_output_is_hashed_in_chunks_and_stderr_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"0123456789abcdef" * 32
    responses = [
        (payload, b"", 0),
        (b"", b"x" * 20_000, 1),
    ]
    popen_calls: list[tuple[list[str], dict[str, object]]] = []

    class FakeProcess:
        def __init__(
            self,
            stdout: bytes,
            stderr: bytes,
            returncode: int,
        ) -> None:
            self.stdout = io.BytesIO(stdout)
            self.stderr = io.BytesIO(stderr)
            self.returncode = returncode

        def poll(self) -> int:
            return self.returncode

        def wait(self) -> int:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    def fake_popen(
        args: list[str],
        **kwargs: object,
    ) -> FakeProcess:
        popen_calls.append((args, kwargs))
        return FakeProcess(*responses.pop(0))

    monkeypatch.setattr(fingerprints_module, "_STREAM_CHUNK_SIZE", 32)
    monkeypatch.setattr(
        fingerprints_module.subprocess,
        "Popen",
        fake_popen,
    )

    chunks: list[int] = []
    digest = hashlib.sha256()

    def hash_chunk(chunk: bytes) -> None:
        chunks.append(len(chunk))
        digest.update(chunk)

    fingerprints_module._stream_git_stdout(
        tmp_path,
        ["diff", "--binary", "immutable-oid"],
        hash_chunk,
    )
    assert digest.hexdigest() == hashlib.sha256(payload).hexdigest()
    assert len(chunks) > 1
    assert max(chunks) <= 32

    with pytest.raises(FingerprintError) as failed:
        fingerprints_module._stream_git_stdout(
            tmp_path,
            ["diff", "--binary", "immutable-oid"],
            lambda _chunk: None,
        )
    assert len(str(failed.value)) <= 1200
    assert str(failed.value).endswith("...")
    assert all(call[1]["shell"] is False for call in popen_calls)
    assert all(call[1]["stdout"] is subprocess.PIPE for call in popen_calls)
    assert all(call[1]["stderr"] is subprocess.PIPE for call in popen_calls)


def test_invalid_ref_and_non_git_errors_are_bounded(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    initialized_repo(repo)
    with pytest.raises(FingerprintError) as invalid:
        diff_fingerprint(
            repo,
            ".",
            DiffRequest("commit", commit="definitely-not-a-ref"),
        )
    assert "Git fingerprint" in str(invalid.value)
    assert len(str(invalid.value)) <= 1200

    non_git = tmp_path / "not-git"
    non_git.mkdir()
    with pytest.raises(FingerprintError) as outside:
        working_fingerprint(non_git, ".")
    assert "Git fingerprint" in str(outside.value)
    assert len(str(outside.value)) <= 1200


def test_stdin_fingerprint_is_rejected_clearly(tmp_path: Path) -> None:
    with pytest.raises(FingerprintError, match="stdin"):
        diff_fingerprint(tmp_path, ".", DiffRequest("stdin"))
