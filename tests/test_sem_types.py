from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from usr.plugins.sem_review_loop.helpers.sem_types import (
    MAX_CHANGES,
    MAX_CONTENT_BYTES,
    MAX_SUMMARY_VALUE,
    DiffRequest,
    SemParseError,
    parse_diff,
    snapshot_from_parsed,
)


FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def structural_payload() -> dict[str, object]:
    payload = load("structural_diff.json")
    assert isinstance(payload, dict)
    return payload


def structural_change(payload: dict[str, object]) -> dict[str, object]:
    changes = payload["changes"]
    assert isinstance(changes, list)
    change = changes[0]
    assert isinstance(change, dict)
    return change


def test_structural_payload_keeps_source_only_in_detail() -> None:
    parsed = parse_diff(structural_payload())
    change = parsed.changes[0]

    assert change.structural is True
    assert change.entity.entity_name == "validate_token"
    assert change.start_line == 10
    assert change.old_end_line == 12
    assert parsed.has_structural_changes is True
    assert parsed.details[change.entity.entity_id].before_content.startswith(
        "def validate_token"
    )

    encoded_card = json.dumps(change.to_card())
    assert "before_content" not in encoded_card
    assert "after_content" not in encoded_card
    assert "return bool" not in encoded_card
    with pytest.raises(TypeError):
        parsed.details["new"] = parsed.details[change.entity.entity_id]  # type: ignore[index]


def test_cosmetic_payload_is_supported_but_not_structural() -> None:
    parsed = parse_diff(load("cosmetic_diff.json"))

    assert parsed.has_structural_changes is False
    assert parsed.changes[0].structural is False


@pytest.mark.parametrize(
    ("fixture", "change_type"),
    [
        ("deleted_diff.json", "deleted"),
        ("reordered_diff.json", "reordered"),
    ],
)
def test_pinned_null_structural_classification_is_conservatively_structural(
    fixture: str,
    change_type: str,
) -> None:
    parsed = parse_diff(load(fixture))

    assert parsed.changes[0].change_type == change_type
    assert parsed.changes[0].structural is True
    assert parsed.has_structural_changes is True


@pytest.mark.parametrize(
    ("fixture", "bucket"),
    [
        ("renamed_edited_diff.json", "renamed"),
        ("moved_edited_diff.json", "moved"),
    ],
)
def test_edited_rename_and_move_use_pinned_overlapping_modified_bucket(
    fixture: str,
    bucket: str,
) -> None:
    payload = load(fixture)
    parsed = parse_diff(payload)

    assert parsed.summary.modified == 1
    assert getattr(parsed.summary, bucket) == 1
    assert parsed.summary.total == 1

    assert isinstance(payload, dict)
    summary = payload["summary"]
    assert isinstance(summary, dict)
    summary["modified"] = 0
    with pytest.raises(SemParseError, match="semantic counters"):
        parse_diff(payload)


def test_malformed_fixture_fails_closed_on_missing_required_summary_field() -> None:
    with pytest.raises(SemParseError, match=r"summary\.total is required"):
        parse_diff(load("malformed_diff.json"))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("payload", [], "payload must be an object"),
        ("summary", [], "summary must be an object"),
        ("changes", {}, "changes must be an array"),
        ("binaryChanges", {}, "binaryChanges must be an array"),
    ],
)
def test_required_container_shapes_are_strict(
    field: str,
    value: object,
    message: str,
) -> None:
    payload: object = structural_payload()
    if field == "payload":
        payload = value
    else:
        assert isinstance(payload, dict)
        payload[field] = value

    with pytest.raises(SemParseError, match=message):
        parse_diff(payload)


def test_unknown_top_level_summary_and_change_fields_are_rejected() -> None:
    for field, target in (
        ("future", None),
        ("summaryFuture", "summary"),
        ("changeFuture", "change"),
    ):
        payload = structural_payload()
        if target is None:
            payload[field] = True
        elif target == "summary":
            summary = payload["summary"]
            assert isinstance(summary, dict)
            summary[field] = 1
        else:
            structural_change(payload)[field] = True

        with pytest.raises(SemParseError, match="unknown field"):
            parse_diff(payload)


@pytest.mark.parametrize("value", [True, -1, MAX_SUMMARY_VALUE + 1])
def test_summary_counters_are_exact_bounded_integers(value: object) -> None:
    payload = structural_payload()
    summary = payload["summary"]
    assert isinstance(summary, dict)
    summary["modified"] = value

    with pytest.raises(SemParseError, match=r"summary\.modified"):
        parse_diff(payload)


def test_summary_must_match_semantic_and_binary_arrays() -> None:
    payload = structural_payload()
    summary = payload["summary"]
    assert isinstance(summary, dict)
    summary["total"] = 0

    with pytest.raises(SemParseError, match=r"summary\.total"):
        parse_diff(payload)

    payload = structural_payload()
    summary = payload["summary"]
    assert isinstance(summary, dict)
    summary["modified"] = 0
    with pytest.raises(SemParseError, match="semantic counters"):
        parse_diff(payload)


def test_change_count_is_bounded_before_change_records_are_parsed() -> None:
    payload = structural_payload()
    payload["changes"] = [{}] * (MAX_CHANGES + 1)

    with pytest.raises(SemParseError, match=f"more than {MAX_CHANGES}"):
        parse_diff(payload)


def test_duplicate_entity_ids_are_rejected() -> None:
    payload = structural_payload()
    changes = payload["changes"]
    summary = payload["summary"]
    assert isinstance(changes, list)
    assert isinstance(summary, dict)
    changes.append(deepcopy(changes[0]))
    summary["modified"] = 2
    summary["total"] = 2

    with pytest.raises(SemParseError, match="duplicates"):
        parse_diff(payload)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/etc/passwd",
        "../secret.py",
        "src/../../secret.py",
        ".a0proj/review.json",
        "src/\x00secret.py",
        "src\\secret.py",
        "C:/secret.py",
        "./src/file.py",
        "src//file.py",
    ],
)
def test_semantic_paths_must_be_safe_relative_posix_paths(
    unsafe_path: str,
) -> None:
    payload = structural_payload()
    structural_change(payload)["filePath"] = unsafe_path

    with pytest.raises(
        SemParseError,
        match="POSIX path|outside semantic review scope|control",
    ):
        parse_diff(payload)


def test_old_and_binary_paths_receive_the_same_scope_validation() -> None:
    payload = structural_payload()
    structural_change(payload)["oldFilePath"] = ".a0proj/private.py"
    with pytest.raises(SemParseError, match="outside semantic review scope"):
        parse_diff(payload)

    payload = structural_payload()
    summary = payload["summary"]
    assert isinstance(summary, dict)
    summary["binary"] = 1
    summary["total"] = 2
    payload["binaryChanges"] = [
        {
            "changeType": "binary",
            "filePath": "../image.png",
            "oldFilePath": None,
            "fileStatus": "modified",
        }
    ]
    with pytest.raises(SemParseError, match="outside semantic review scope"):
        parse_diff(payload)


def test_binary_change_schema_and_status_are_strict() -> None:
    payload = structural_payload()
    summary = payload["summary"]
    assert isinstance(summary, dict)
    summary["binary"] = 1
    summary["total"] = 2
    payload["binaryChanges"] = [
        {
            "changeType": "binary",
            "filePath": "image.png",
            "oldFilePath": None,
            "fileStatus": "future-status",
        }
    ]

    with pytest.raises(SemParseError, match="fileStatus is unsupported"):
        parse_diff(payload)


@pytest.mark.parametrize("value", ["false", 0, 1, [], {}])
def test_unknown_or_malformed_structural_classification_is_rejected(
    value: object,
) -> None:
    payload = structural_payload()
    structural_change(payload)["structuralChange"] = value

    with pytest.raises(SemParseError, match="must be a boolean or null"):
        parse_diff(payload)


def test_only_pinned_change_types_are_supported() -> None:
    payload = structural_payload()
    structural_change(payload)["changeType"] = "copied"

    with pytest.raises(SemParseError, match="changeType is unsupported"):
        parse_diff(payload)

    payload = structural_payload()
    structural_change(payload)["changeType"] = ["modified"]
    with pytest.raises(SemParseError, match="changeType is unsupported"):
        parse_diff(payload)


@pytest.mark.parametrize("value", [True, -1, 10_000_001, None])
def test_current_line_numbers_are_bounded_required_integers(
    value: object,
) -> None:
    payload = structural_payload()
    structural_change(payload)["startLine"] = value

    with pytest.raises(SemParseError, match=r"startLine must be an integer"):
        parse_diff(payload)


def test_line_spans_cannot_run_backwards() -> None:
    payload = structural_payload()
    structural_change(payload)["startLine"] = 20

    with pytest.raises(SemParseError, match="start must not exceed"):
        parse_diff(payload)


def test_each_source_content_is_limited_by_utf8_bytes() -> None:
    payload = structural_payload()
    structural_change(payload)["beforeContent"] = "x" * (MAX_CONTENT_BYTES + 1)
    with pytest.raises(SemParseError, match="2 MiB"):
        parse_diff(payload)

    payload = structural_payload()
    structural_change(payload)["afterContent"] = "é" * (
        MAX_CONTENT_BYTES // 2 + 1
    )
    with pytest.raises(SemParseError, match="2 MiB"):
        parse_diff(payload)


def test_required_change_shape_and_source_types_fail_closed() -> None:
    payload = structural_payload()
    del structural_change(payload)["entityName"]
    with pytest.raises(SemParseError, match=r"entityName is required"):
        parse_diff(payload)

    payload = structural_payload()
    structural_change(payload)["beforeContent"] = ["not", "source"]
    with pytest.raises(SemParseError, match="must be a string or null"):
        parse_diff(payload)


def test_snapshot_uses_pinned_version_and_utc_iso_timestamp() -> None:
    parsed = parse_diff(structural_payload())
    snapshot = snapshot_from_parsed(
        parsed,
        DiffRequest("working"),
        "fingerprint",
    )

    assert snapshot.sem_version == "0.21.0"
    assert snapshot.revision == 0
    assert snapshot.details is parsed.details
    completed = datetime.fromisoformat(snapshot.completed_at)
    assert completed.utcoffset() is not None
    assert completed.utcoffset().total_seconds() == 0
