from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from usr.plugins.sem_review_loop.helpers.project_scope import ProjectScope
from usr.plugins.sem_review_loop.helpers.sanitization import (
    SanitizationError,
    sanitize_plain_line,
)
from usr.plugins.sem_review_loop.helpers.sem_types import DiffSnapshot


LESSON_SCHEMA_VERSION = 1
MAX_LESSONS = 512
MAX_FILE_PATTERNS = 128
MAX_ENTITY_VALUES = 200
MAX_TAGS = 64
MAX_RELATIONS = 128
MAX_LESSON_FILE_BYTES = 2 * 1024 * 1024
MAX_PROPOSAL_ID_CHARS = 80
MAX_METADATA_CHARS = 2048
MAX_TAG_CHARS = 128
MAX_TIMESTAMP_CHARS = 64
PROJECT_ID_PATTERN = re.compile(r"\A[a-f0-9]{8,64}\Z")
STATUS_VALUES = frozenset({"pending", "approved"})


class LessonError(ValueError):
    """Raised when a project lesson cannot be safely read or written."""


class LessonStaleError(LessonError):
    """Raised when a pending proposal no longer matches reviewed state."""


@dataclass(frozen=True)
class LessonProposal:
    proposal_id: str
    project_id: str
    status: str
    fingerprint: str
    revision: int
    problem: str
    resolution: str
    entity_ids: tuple[str, ...]
    entity_types: tuple[str, ...]
    file_patterns: tuple[str, ...]
    change_types: tuple[str, ...]
    impact_relations: tuple[str, ...]
    verification_summary: str
    verification_result: str
    applicability_tags: tuple[str, ...]
    created_at: str
    approved_at: str = ""


_FIELDS = frozenset(LessonProposal.__dataclass_fields__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strict_text(
    value: object,
    field: str,
    *,
    maximum: int = MAX_METADATA_CHARS,
    empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise LessonError(f"{field} must be text.")
    if len(value.encode("utf-8", errors="replace")) > maximum:
        raise LessonError(f"{field} exceeds its limit.")
    if not value and not empty:
        raise LessonError(f"{field} may not be empty.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise LessonError(f"{field} contains control characters.")
    return value


def _safe_prose(value: object, field: str, *, empty: bool = False) -> str:
    try:
        return sanitize_plain_line(
            value,
            maximum=MAX_METADATA_CHARS,
            allow_empty=empty,
        )
    except SanitizationError as exc:
        raise LessonError(f"{field} is not safe plain text.") from exc


def _safe_sequence(
    value: object,
    field: str,
    *,
    maximum_items: int,
    maximum_chars: int,
    prose: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise LessonError(f"{field} must be an array.")
    if len(value) > maximum_items:
        raise LessonError(f"{field} exceeds its item limit.")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(
            _safe_prose(item, f"{field}[{index}]")
            if prose
            else _strict_text(
                item,
                f"{field}[{index}]",
                maximum=maximum_chars,
            )
        )
    if sum(len(item.encode("utf-8")) for item in result) > maximum_chars * 4:
        raise LessonError(f"{field} exceeds its byte limit.")
    if len(set(result)) != len(result):
        raise LessonError(f"{field} contains duplicate values.")
    return tuple(result)


def _safe_path(value: object, field: str) -> str:
    text = _strict_text(value, field, maximum=4096)
    if (
        text.startswith(("/", "\\", "~"))
        or "\\" in text
        or any(part in {"", ".", ".."} for part in text.split("/"))
        or text.startswith(".a0proj/")
        or "://" in text
    ):
        raise LessonError(f"{field} must be a project-relative path.")
    return text


def _safe_timestamp(value: object, field: str, *, empty: bool = False) -> str:
    text = _strict_text(value, field, maximum=MAX_TIMESTAMP_CHARS, empty=empty)
    if not text:
        return ""
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LessonError(f"{field} must be an ISO timestamp.") from exc
    return text


def _validate_project_id(project_id: object) -> str:
    value = _strict_text(project_id, "project_id", maximum=64)
    if PROJECT_ID_PATTERN.fullmatch(value) is None:
        raise LessonError("project_id is invalid.")
    return value


def _record_to_dict(proposal: LessonProposal) -> dict[str, object]:
    return asdict(proposal)


def _validate_record(value: object, *, project_id: str) -> LessonProposal:
    if not isinstance(value, dict) or frozenset(value) != _FIELDS:
        raise LessonError("lesson record has an invalid schema.")
    record_project = _validate_project_id(value.get("project_id"))
    if record_project != project_id:
        raise LessonError("lesson record belongs to another project.")
    proposal_id = _strict_text(
        value.get("proposal_id"),
        "proposal_id",
        maximum=MAX_PROPOSAL_ID_CHARS,
    )
    if not proposal_id.startswith("lesson-"):
        raise LessonError("proposal_id is invalid.")
    status = _strict_text(value.get("status"), "status", maximum=16)
    if status not in STATUS_VALUES:
        raise LessonError("status is invalid.")
    fingerprint = _strict_text(value.get("fingerprint"), "fingerprint", maximum=256)
    if not re.fullmatch(r"[a-fA-F0-9]{16,128}", fingerprint):
        raise LessonError("fingerprint is invalid.")
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise LessonError("revision is invalid.")
    entity_ids = _safe_sequence(
        value.get("entity_ids"), "entity_ids", maximum_items=MAX_ENTITY_VALUES,
        maximum_chars=MAX_METADATA_CHARS,
    )
    entity_types = _safe_sequence(
        value.get("entity_types"), "entity_types", maximum_items=MAX_ENTITY_VALUES,
        maximum_chars=MAX_METADATA_CHARS,
    )
    raw_file_patterns = value.get("file_patterns")
    if not isinstance(raw_file_patterns, (list, tuple)):
        raise LessonError("file_patterns must be an array.")
    file_patterns = tuple(
        _safe_path(item, "file_patterns[]") for item in raw_file_patterns
    )
    if len(file_patterns) > MAX_FILE_PATTERNS or len(set(file_patterns)) != len(file_patterns):
        raise LessonError("file_patterns is invalid.")
    change_types = _safe_sequence(
        value.get("change_types"), "change_types", maximum_items=32,
        maximum_chars=2048,
    )
    impact_relations = _safe_sequence(
        value.get("impact_relations"), "impact_relations", maximum_items=MAX_RELATIONS,
        maximum_chars=MAX_METADATA_CHARS,
    )
    tags = _safe_sequence(
        value.get("applicability_tags"), "applicability_tags", maximum_items=MAX_TAGS,
        maximum_chars=MAX_TAG_CHARS,
    )
    return LessonProposal(
        proposal_id=proposal_id,
        project_id=record_project,
        status=status,
        fingerprint=fingerprint,
        revision=revision,
        problem=_safe_prose(value.get("problem"), "problem"),
        resolution=_safe_prose(value.get("resolution"), "resolution"),
        entity_ids=entity_ids,
        entity_types=entity_types,
        file_patterns=file_patterns,
        change_types=change_types,
        impact_relations=impact_relations,
        verification_summary=_safe_prose(
            value.get("verification_summary"), "verification_summary", empty=True
        ),
        verification_result=_safe_prose(
            value.get("verification_result"), "verification_result", empty=True
        ),
        applicability_tags=tags,
        created_at=_safe_timestamp(value.get("created_at"), "created_at"),
        approved_at=_safe_timestamp(value.get("approved_at"), "approved_at", empty=True),
    )


def _dedup_key(proposal: LessonProposal) -> str:
    payload = json.dumps(
        {
            "problem": proposal.problem,
            "resolution": proposal.resolution,
            "entity_types": proposal.entity_types,
            "file_patterns": proposal.file_patterns,
            "change_types": proposal.change_types,
            "impact_relations": proposal.impact_relations,
            "tags": proposal.applicability_tags,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LessonStore:
    """Bounded, project-owned JSON lessons with atomic durable writes."""

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)

    def _path(self, scope: ProjectScope) -> Path:
        project_id = _validate_project_id(scope.project_id)
        root = self.data_root.resolve(strict=False)
        root.mkdir(parents=True, exist_ok=True)
        path = (root / project_id / "lessons.json").resolve(strict=False)
        if root not in path.parents:
            raise LessonError("lesson storage escaped plugin data.")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.is_symlink() or path.is_symlink():
            raise LessonError("lesson storage is unsafe.")
        return path

    def _lock(self, project_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(project_id, threading.RLock())

    def _read(self, scope: ProjectScope) -> tuple[list[LessonProposal], list[LessonProposal]]:
        path = self._path(scope)
        if not path.exists():
            return [], []
        if path.stat().st_size > MAX_LESSON_FILE_BYTES:
            raise LessonError("lesson store exceeds its size limit.")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LessonError("lesson store is unreadable.") from exc
        if (
            not isinstance(value, dict)
            or frozenset(value) != {"schema_version", "pending", "approved"}
            or value.get("schema_version") != LESSON_SCHEMA_VERSION
            or not isinstance(value.get("pending"), list)
            or not isinstance(value.get("approved"), list)
            or len(value["pending"]) > MAX_LESSONS
            or len(value["approved"]) > MAX_LESSONS
        ):
            raise LessonError("lesson store has an invalid schema.")
        pending = [_validate_record(item, project_id=scope.project_id) for item in value["pending"]]
        approved = [_validate_record(item, project_id=scope.project_id) for item in value["approved"]]
        if any(item.status != "pending" for item in pending) or any(item.status != "approved" for item in approved):
            raise LessonError("lesson store has an invalid status.")
        return pending, approved

    def _write(
        self,
        scope: ProjectScope,
        pending: Iterable[LessonProposal],
        approved: Iterable[LessonProposal],
    ) -> None:
        pending_values = list(pending)
        approved_values = list(approved)
        if len(pending_values) > MAX_LESSONS or len(approved_values) > MAX_LESSONS:
            raise LessonError("lesson store is full.")
        path = self._path(scope)
        document = {
            "schema_version": LESSON_SCHEMA_VERSION,
            "pending": [_record_to_dict(item) for item in pending_values],
            "approved": [_record_to_dict(item) for item in approved_values],
        }
        encoded = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(encoded) > MAX_LESSON_FILE_BYTES:
            raise LessonError("lesson store exceeds its size limit.")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except OSError as exc:
            raise LessonError("lesson store could not be written.") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def list(self, scope: ProjectScope, *, current_fingerprint: str = "") -> dict[str, list[dict[str, object]]]:
        with self._lock(scope.project_id):
            pending, approved = self._read(scope)
        return {
            "pending": [
                {**_record_to_dict(item), "stale": bool(current_fingerprint and item.fingerprint != current_fingerprint)}
                for item in pending
            ],
            "approved": [_record_to_dict(item) for item in approved],
        }

    def propose_from_checkpoint(
        self,
        scope: ProjectScope,
        snapshot: DiffSnapshot,
        *,
        problem: str,
        resolution: str,
        verification_summary: str = "",
        verification_result: str = "",
        applicability_tags: Iterable[str] = (),
        impact_relations: Iterable[str] = (),
    ) -> LessonProposal | None:
        if snapshot.request.mode != "working" or snapshot.stale or snapshot.error:
            return None
        entity_ids = tuple(sorted(change.entity.entity_id for change in snapshot.changes if change.structural))
        if not entity_ids:
            return None
        proposal_seed = LessonProposal(
            proposal_id="lesson-pending",
            project_id=scope.project_id,
            status="pending",
            fingerprint=snapshot.fingerprint,
            revision=snapshot.revision,
            problem=_safe_prose(problem, "problem"),
            resolution=_safe_prose(resolution, "resolution"),
            entity_ids=entity_ids,
            entity_types=tuple(sorted({change.entity.entity_type for change in snapshot.changes if change.structural})),
            file_patterns=tuple(sorted({change.entity.file_path for change in snapshot.changes if change.structural})),
            change_types=tuple(sorted({change.change_type for change in snapshot.changes if change.structural})),
            impact_relations=tuple(impact_relations),
            verification_summary=_safe_prose(verification_summary, "verification_summary", empty=True),
            verification_result=_safe_prose(verification_result, "verification_result", empty=True),
            applicability_tags=tuple(applicability_tags),
            created_at=_now(),
        )
        proposal = LessonProposal(
            **{
                **asdict(proposal_seed),
                "entity_ids": proposal_seed.entity_ids,
                "entity_types": proposal_seed.entity_types,
                "file_patterns": proposal_seed.file_patterns,
                "change_types": proposal_seed.change_types,
                "impact_relations": proposal_seed.impact_relations,
                "applicability_tags": proposal_seed.applicability_tags,
                "proposal_id": "lesson-" + _dedup_key(proposal_seed)[:24],
            }
        )
        _validate_record(_record_to_dict(proposal), project_id=scope.project_id)
        with self._lock(scope.project_id):
            pending, approved = self._read(scope)
            key = _dedup_key(proposal)
            for existing in (*pending, *approved):
                if _dedup_key(existing) == key:
                    return existing
            self._write(scope, [*pending, proposal], approved)
        return proposal

    def approve(
        self,
        scope: ProjectScope,
        proposal_id: str,
        *,
        current_fingerprint: str,
    ) -> LessonProposal:
        with self._lock(scope.project_id):
            pending, approved = self._read(scope)
            for index, item in enumerate(pending):
                if item.proposal_id != proposal_id:
                    continue
                if not current_fingerprint or item.fingerprint != current_fingerprint:
                    raise LessonStaleError("lesson proposal is stale; refresh before approving.")
                promoted = LessonProposal(
                    **{
                        **asdict(item),
                        "status": "approved",
                        "approved_at": _now(),
                    }
                )
                pending[index] = item
                pending.pop(index)
                self._write(scope, pending, [*approved, promoted])
                return promoted
        raise LessonError("lesson proposal was not found.")

    def discard(self, scope: ProjectScope, proposal_id: str) -> bool:
        with self._lock(scope.project_id):
            pending, approved = self._read(scope)
            kept = [item for item in pending if item.proposal_id != proposal_id]
            if len(kept) == len(pending):
                return False
            self._write(scope, kept, approved)
            return True

    def delete(self, scope: ProjectScope, proposal_id: str) -> bool:
        with self._lock(scope.project_id):
            pending, approved = self._read(scope)
            kept = [item for item in approved if item.proposal_id != proposal_id]
            if len(kept) == len(approved):
                return False
            self._write(scope, pending, kept)
            return True

    def forget_all(self, scope: ProjectScope) -> int:
        with self._lock(scope.project_id):
            pending, approved = self._read(scope)
            self._write(scope, [], [])
            return len(pending) + len(approved)

    def match(self, scope: ProjectScope, snapshot: DiffSnapshot, *, limit: int = 5) -> tuple[LessonProposal, ...]:
        if snapshot.request.mode != "working" or snapshot.stale or snapshot.error:
            return ()
        changed_paths = {change.entity.file_path for change in snapshot.changes}
        changed_types = {change.entity.entity_type for change in snapshot.changes}
        changed_categories = {change.change_type for change in snapshot.changes}
        with self._lock(scope.project_id):
            _pending, approved = self._read(scope)
        matches = [
            item for item in approved
            if (
                bool(changed_paths & set(item.file_patterns))
                and bool(changed_types & set(item.entity_types))
                and bool(changed_categories & set(item.change_types))
            )
        ]
        return tuple(matches[: max(0, min(limit, 5))])
