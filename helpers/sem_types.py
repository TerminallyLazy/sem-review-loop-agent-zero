from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Literal, Mapping


SEM_VERSION = "0.21.0"
MAX_CHANGES = 10_000
MAX_CONTENT_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_SUMMARY_VALUE = MAX_CHANGES
MAX_LINE_NUMBER = 10_000_000
MAX_ENTITY_ID_BYTES = 2_048
MAX_ENTITY_NAME_BYTES = 1_024
MAX_ENTITY_TYPE_BYTES = 256
MAX_PATH_BYTES = 4_096
MAX_METADATA_BYTES = 4_096

SUPPORTED_CHANGE_TYPES = frozenset(
    {"added", "modified", "deleted", "moved", "renamed", "reordered"}
)
CONTENT_OVERLAP_CHANGE_TYPES = frozenset({"moved", "renamed", "reordered"})
SUPPORTED_FILE_STATUSES = frozenset({"added", "modified", "deleted", "renamed"})

SUMMARY_FIELDS = (
    ("fileCount", "file_count"),
    ("added", "added"),
    ("modified", "modified"),
    ("deleted", "deleted"),
    ("moved", "moved"),
    ("renamed", "renamed"),
    ("reordered", "reordered"),
    ("binary", "binary"),
    ("orphan", "orphan"),
    ("total", "total"),
)
ROOT_FIELDS = ("summary", "changes", "binaryChanges")
CHANGE_FIELDS = (
    "entityId",
    "changeType",
    "entityType",
    "entityName",
    "startLine",
    "endLine",
    "oldStartLine",
    "oldEndLine",
    "oldEntityName",
    "filePath",
    "oldFilePath",
    "oldParentId",
    "beforeContent",
    "afterContent",
    "commitSha",
    "author",
    "structuralChange",
)
BINARY_CHANGE_FIELDS = (
    "changeType",
    "filePath",
    "oldFilePath",
    "fileStatus",
)


class SemParseError(RuntimeError):
    """Raised when SEM output does not match the pinned, bounded schema."""


# Kept as a compatibility alias for the accepted implementation plan.
SemPayloadError = SemParseError


@dataclass(frozen=True)
class DiffRequest:
    mode: Literal["working", "staged", "commit", "range", "stdin"]
    commit: str = ""
    from_ref: str = ""
    to_ref: str = ""


@dataclass(frozen=True)
class DiffSummary:
    file_count: int
    added: int
    modified: int
    deleted: int
    moved: int
    renamed: int
    reordered: int
    binary: int
    orphan: int
    total: int


@dataclass(frozen=True)
class EntityRef:
    entity_id: str
    entity_name: str
    entity_type: str
    file_path: str


@dataclass(frozen=True)
class EntityDetail:
    before_content: str
    after_content: str


@dataclass(frozen=True)
class EntityChange:
    entity: EntityRef
    change_type: str
    start_line: int | None
    end_line: int | None
    old_start_line: int | None
    old_end_line: int | None
    old_entity_name: str
    old_file_path: str
    structural: bool

    def to_card(self) -> dict[str, object]:
        """Return bounded review metadata without before/after source."""

        return {
            "entity_id": self.entity.entity_id,
            "entity_name": self.entity.entity_name,
            "entity_type": self.entity.entity_type,
            "file_path": self.entity.file_path,
            "change_type": self.change_type,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "structural": self.structural,
        }


@dataclass(frozen=True)
class ParsedDiff:
    summary: DiffSummary
    changes: tuple[EntityChange, ...]
    details: Mapping[str, EntityDetail]

    @property
    def has_structural_changes(self) -> bool:
        return any(change.structural for change in self.changes)


@dataclass(frozen=True)
class DiffSnapshot:
    request: DiffRequest
    fingerprint: str
    revision: int
    summary: DiffSummary
    changes: tuple[EntityChange, ...]
    details: Mapping[str, EntityDetail]
    sem_version: str
    completed_at: str
    stale: bool = False
    error: str = ""


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemParseError(f"{field} must be an object.")
    if any(not isinstance(key, str) for key in value):
        raise SemParseError(f"{field} keys must be strings.")
    return value


def _array(value: object, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise SemParseError(f"{field} must be an array.")
    return value


def _exact_fields(
    record: Mapping[str, Any],
    expected: tuple[str, ...],
    field: str,
) -> None:
    expected_set = frozenset(expected)
    for key in expected:
        if key not in record:
            raise SemParseError(f"{field}.{key} is required.")
    unknown = sorted(set(record) - expected_set)
    if unknown:
        raise SemParseError(
            f"{field} contains unknown field: {unknown[0]}."
        )


def _utf8_size(value: str, field: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise SemParseError(f"{field} must be valid UTF-8 text.") from exc


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _metadata_text(
    value: object,
    field: str,
    *,
    maximum: int,
    required: bool = True,
    nullable: bool = False,
) -> str:
    if value is None and nullable:
        return ""
    if not isinstance(value, str):
        suffix = " or null" if nullable else ""
        raise SemParseError(f"{field} must be a string{suffix}.")
    if required and not value.strip():
        raise SemParseError(f"{field} must be a non-empty string.")
    if _has_control(value):
        raise SemParseError(f"{field} contains control characters.")
    if _utf8_size(value, field) > maximum:
        raise SemParseError(f"{field} exceeds its byte limit.")
    return value


def _source_text(value: object, field: str) -> tuple[str, int]:
    if value is None:
        return "", 0
    if not isinstance(value, str):
        raise SemParseError(f"{field} must be a string or null.")
    if any(
        (ord(character) < 32 and character not in "\t\n\r")
        or ord(character) == 127
        for character in value
    ):
        raise SemParseError(f"{field} contains unsupported control characters.")
    size = _utf8_size(value, field)
    if size > MAX_CONTENT_BYTES:
        raise SemParseError(f"{field} exceeds the 2 MiB limit.")
    return value, size


def validate_relative_posix_path(value: object, field: str) -> str:
    text = _metadata_text(
        value,
        field,
        maximum=MAX_PATH_BYTES,
        required=True,
    )
    if "\\" in text:
        raise SemParseError(f"{field} must be a relative POSIX path.")

    posix_path = PurePosixPath(text)
    windows_path = PureWindowsPath(text)
    raw_parts = text.split("/")
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or any(part in {"", ".", ".."} for part in raw_parts)
        or raw_parts[0] == ".a0proj"
        or posix_path.as_posix() != text
    ):
        raise SemParseError(f"{field} is outside semantic review scope.")
    return text


def _optional_path(value: object, field: str) -> str:
    if value is None:
        return ""
    return validate_relative_posix_path(value, field)


def _summary_counter(record: Mapping[str, Any], key: str) -> int:
    value = record[key]
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_SUMMARY_VALUE
    ):
        raise SemParseError(
            f"summary.{key} must be an integer from 0 to "
            f"{MAX_SUMMARY_VALUE}."
        )
    return value


def _line(
    record: Mapping[str, Any],
    key: str,
    index: int,
    *,
    nullable: bool,
) -> int | None:
    value = record[key]
    field = f"changes[{index}].{key}"
    if value is None and nullable:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_LINE_NUMBER
    ):
        null_suffix = " or null" if nullable else ""
        raise SemParseError(
            f"{field} must be an integer from 0 to {MAX_LINE_NUMBER}"
            f"{null_suffix}."
        )
    return value


def _validate_span(
    start: int | None,
    end: int | None,
    field: str,
) -> None:
    if start is not None and end is not None and start > end:
        raise SemParseError(f"{field} start must not exceed its end.")


def _parse_binary_changes(values: list[Any]) -> None:
    for index, value in enumerate(values):
        field = f"binaryChanges[{index}]"
        record = _mapping(value, field)
        _exact_fields(record, BINARY_CHANGE_FIELDS, field)
        if record["changeType"] != "binary":
            raise SemParseError(f"{field}.changeType must be binary.")
        status = record["fileStatus"]
        if (
            not isinstance(status, str)
            or status not in SUPPORTED_FILE_STATUSES
        ):
            raise SemParseError(f"{field}.fileStatus is unsupported.")
        validate_relative_posix_path(record["filePath"], f"{field}.filePath")
        _optional_path(record["oldFilePath"], f"{field}.oldFilePath")


def sem_v021_change_counters(
    changes: list[Any],
) -> dict[str, int]:
    """Reproduce sem v0.21.0's overlapping semantic summary buckets.

    The pinned implementation additionally increments ``modified`` for an
    edited move, rename, or reorder when both content values are present.
    Callers use this only after the strict record parser has validated the
    payload shape and change types.
    """

    counters = {
        "added": 0,
        "modified": 0,
        "deleted": 0,
        "moved": 0,
        "renamed": 0,
        "reordered": 0,
    }
    for value in changes:
        if not isinstance(value, dict):
            continue
        change_type = value.get("changeType")
        if change_type not in counters:
            continue
        counters[change_type] += 1
        before_content = value.get("beforeContent")
        after_content = value.get("afterContent")
        if (
            change_type in CONTENT_OVERLAP_CHANGE_TYPES
            and isinstance(before_content, str)
            and isinstance(after_content, str)
            and before_content != after_content
        ):
            counters["modified"] += 1
    return counters


def parse_diff(payload: object) -> ParsedDiff:
    root = _mapping(payload, "payload")
    _exact_fields(root, ROOT_FIELDS, "payload")

    summary_record = _mapping(root["summary"], "summary")
    _exact_fields(
        summary_record,
        tuple(source for source, _target in SUMMARY_FIELDS),
        "summary",
    )
    summary = DiffSummary(
        **{
            target: _summary_counter(summary_record, source)
            for source, target in SUMMARY_FIELDS
        }
    )

    raw_changes = _array(root["changes"], "changes")
    raw_binary_changes = _array(root["binaryChanges"], "binaryChanges")
    total_change_count = len(raw_changes) + len(raw_binary_changes)
    if total_change_count > MAX_CHANGES:
        raise SemParseError(
            f"payload contains more than {MAX_CHANGES} changes."
        )
    _parse_binary_changes(raw_binary_changes)

    if summary.binary != len(raw_binary_changes):
        raise SemParseError("summary.binary does not match binaryChanges.")
    if summary.total != total_change_count:
        raise SemParseError("summary.total does not match all changes.")

    changes: list[EntityChange] = []
    details: dict[str, EntityDetail] = {}
    total_source_bytes = 0
    for index, value in enumerate(raw_changes):
        field = f"changes[{index}]"
        record = _mapping(value, field)
        _exact_fields(record, CHANGE_FIELDS, field)

        entity_id = _metadata_text(
            record["entityId"],
            f"{field}.entityId",
            maximum=MAX_ENTITY_ID_BYTES,
        )
        if entity_id in details:
            raise SemParseError(
                f"{field}.entityId duplicates an earlier entity."
            )
        entity = EntityRef(
            entity_id=entity_id,
            entity_name=_metadata_text(
                record["entityName"],
                f"{field}.entityName",
                maximum=MAX_ENTITY_NAME_BYTES,
            ),
            entity_type=_metadata_text(
                record["entityType"],
                f"{field}.entityType",
                maximum=MAX_ENTITY_TYPE_BYTES,
            ),
            file_path=validate_relative_posix_path(
                record["filePath"],
                f"{field}.filePath",
            ),
        )

        change_type = record["changeType"]
        if (
            not isinstance(change_type, str)
            or change_type not in SUPPORTED_CHANGE_TYPES
        ):
            raise SemParseError(f"{field}.changeType is unsupported.")
        structural = record["structuralChange"]
        if structural is None:
            # sem v0.21.0 uses Option<bool>. Missing structural hashes and
            # inherently unavailable classifications (including deletion and
            # reorder records) serialize as null. Completion gates must treat
            # that uncertainty conservatively as structural.
            structural = True
        elif not isinstance(structural, bool):
            raise SemParseError(
                f"{field}.structuralChange must be a boolean or null."
            )

        start_line = _line(record, "startLine", index, nullable=False)
        end_line = _line(record, "endLine", index, nullable=False)
        old_start_line = _line(
            record,
            "oldStartLine",
            index,
            nullable=True,
        )
        old_end_line = _line(record, "oldEndLine", index, nullable=True)
        _validate_span(start_line, end_line, f"{field} current span")
        _validate_span(old_start_line, old_end_line, f"{field} old span")

        old_entity_name = _metadata_text(
            record["oldEntityName"],
            f"{field}.oldEntityName",
            maximum=MAX_ENTITY_NAME_BYTES,
            required=False,
            nullable=True,
        )
        old_file_path = _optional_path(
            record["oldFilePath"],
            f"{field}.oldFilePath",
        )
        _metadata_text(
            record["oldParentId"],
            f"{field}.oldParentId",
            maximum=MAX_ENTITY_ID_BYTES,
            required=False,
            nullable=True,
        )
        _metadata_text(
            record["commitSha"],
            f"{field}.commitSha",
            maximum=MAX_METADATA_BYTES,
            required=False,
            nullable=True,
        )
        _metadata_text(
            record["author"],
            f"{field}.author",
            maximum=MAX_METADATA_BYTES,
            required=False,
            nullable=True,
        )
        before_content, before_bytes = _source_text(
            record["beforeContent"],
            f"{field}.beforeContent",
        )
        after_content, after_bytes = _source_text(
            record["afterContent"],
            f"{field}.afterContent",
        )
        total_source_bytes += before_bytes + after_bytes
        if total_source_bytes > MAX_OUTPUT_BYTES:
            raise SemParseError(
                "Combined entity detail exceeds the 16 MiB output limit."
            )

        changes.append(
            EntityChange(
                entity=entity,
                change_type=change_type,
                start_line=start_line,
                end_line=end_line,
                old_start_line=old_start_line,
                old_end_line=old_end_line,
                old_entity_name=old_entity_name,
                old_file_path=old_file_path,
                structural=structural,
            )
        )
        details[entity_id] = EntityDetail(
            before_content=before_content,
            after_content=after_content,
        )

    expected_counters = sem_v021_change_counters(raw_changes)
    if any(
        getattr(summary, key) != expected
        for key, expected in expected_counters.items()
    ):
        raise SemParseError(
            "summary semantic counters do not match changes."
        )
    expected_orphan = sum(
        1
        for value in raw_changes
        if isinstance(value, dict) and value.get("entityType") == "orphan"
    )
    if summary.orphan != expected_orphan:
        raise SemParseError(
            "summary.orphan does not match semantic changes."
        )

    return ParsedDiff(
        summary=summary,
        changes=tuple(changes),
        details=MappingProxyType(details),
    )


def snapshot_from_parsed(
    parsed: ParsedDiff,
    request: DiffRequest,
    fingerprint: str,
) -> DiffSnapshot:
    return DiffSnapshot(
        request=request,
        fingerprint=fingerprint,
        revision=0,
        summary=parsed.summary,
        changes=parsed.changes,
        details=parsed.details,
        sem_version=SEM_VERSION,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
