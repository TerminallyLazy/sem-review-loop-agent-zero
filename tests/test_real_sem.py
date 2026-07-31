from __future__ import annotations

import os
import subprocess
from contextlib import nullcontext
from pathlib import Path

import pytest

from usr.plugins.sem_review_loop.helpers.project_scope import make_scope
from usr.plugins.sem_review_loop.helpers.sem_runner import SemRunner
from usr.plugins.sem_review_loop.helpers.sem_types import DiffRequest


def test_real_pinned_sem_working_and_staged_diff(tmp_path: Path) -> None:
    binary_value = os.environ.get("SEM_TEST_BINARY", "").strip()
    if not binary_value:
        pytest.skip("set SEM_TEST_BINARY to run the real sem integration test")
    binary = Path(binary_value).resolve(strict=True)
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "-C", str(repository), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "sem@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "sem-test"],
        check=True,
    )
    source = repository / "src" / "example.py"
    source.parent.mkdir()
    source.write_text(
        'def greet(name):\n    return "hello " + name\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repository), "add", "src/example.py"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "baseline"], check=True)
    source.write_text(
        'def greet(name):\n    prefix = "hello "\n    return prefix + name\n',
        encoding="utf-8",
    )
    scope = make_scope("ctx", "repo", repository, ".")
    runner = SemRunner(
        tmp_path / "cache",
        binary_lease=lambda: nullcontext(binary),
    )
    working = runner.diff(scope, DiffRequest("working"), "a" * 64)
    assert working.summary.total == 1
    assert working.changes[0].entity.entity_name == "greet"
    subprocess.run(["git", "-C", str(repository), "add", "src/example.py"], check=True)
    staged = runner.diff(scope, DiffRequest("staged"), "b" * 64)
    assert staged.summary.total == 1
    assert staged.request.mode == "staged"
    entity = staged.changes[0].entity
    context = runner.context(scope, entity, 1000)
    impact = runner.impact(scope, entity)
    assert isinstance(context, dict)
    assert isinstance(impact, dict)
