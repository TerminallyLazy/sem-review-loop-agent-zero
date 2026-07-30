from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


AGENT_ZERO_METADATA_EXCLUDE_PATHSPEC = ":(exclude,literal).a0proj"


class ProjectScopeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectScope:
    context_id: str
    project_name: str
    project_id: str
    project_root: Path
    watched_root: Path
    watched_relative: str


def literal_watched_pathspec(watched_relative: str) -> str:
    """Encode a project-relative watched directory as literal Git pathspec."""

    return f":(literal){watched_relative or '.'}"


def _resolved_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectScopeError(
            f"{label} does not exist or cannot be resolved."
        ) from exc
    if not resolved.is_dir():
        raise ProjectScopeError(f"{label} must be a directory.")
    return resolved


def make_scope(
    context_id: str,
    project_name: str,
    project_root: Path,
    watched_subdirectory: str,
) -> ProjectScope:
    root = _resolved_directory(Path(project_root), "Project root")
    watched_candidate = root / (watched_subdirectory or ".")
    try:
        non_strict_watched = watched_candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ProjectScopeError(
            "Watched subdirectory cannot be resolved."
        ) from exc
    if (
        non_strict_watched != root
        and root not in non_strict_watched.parents
    ):
        raise ProjectScopeError(
            "Watched subdirectory may not escape the project."
        )
    watched = _resolved_directory(
        watched_candidate,
        "Watched subdirectory",
    )
    if watched != root and root not in watched.parents:
        raise ProjectScopeError(
            "Watched subdirectory may not escape the project."
        )

    watched_relative = (
        "." if watched == root else watched.relative_to(root).as_posix()
    )
    stable_identity = (
        f"{project_name}\0{root}".encode("utf-8")
    )
    project_id = hashlib.sha256(stable_identity).hexdigest()[:24]
    return ProjectScope(
        context_id=str(context_id),
        project_name=str(project_name),
        project_id=project_id,
        project_root=root,
        watched_root=watched,
        watched_relative=watched_relative,
    )


def scope_for_agent(
    agent: object,
    watched_subdirectory: str,
) -> ProjectScope:
    context = getattr(agent, "context", None)
    if context is None:
        raise ProjectScopeError(
            "Semantic Review requires an active Agent Zero project."
        )

    from helpers import projects

    project_name = projects.get_context_project_name(context)
    if not project_name:
        raise ProjectScopeError(
            "Semantic Review requires an active Agent Zero project."
        )
    project_root = Path(projects.get_project_folder(project_name))
    return make_scope(
        context.id,
        project_name,
        project_root,
        watched_subdirectory,
    )
