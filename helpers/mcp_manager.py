from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Protocol

from helpers import projects
from helpers.mcp_handler import MCPConfig, normalize_name
from usr.plugins.sem_review_loop.helpers.config import PluginConfig
from usr.plugins.sem_review_loop.helpers.mcp_launcher import (
    CUSTOM_BINARY_ENV,
    PROJECT_ID_ENV,
    PROJECT_ROOT_ENV,
)
from usr.plugins.sem_review_loop.helpers.paths import (
    CACHE_ROOT,
    DATA_ROOT,
    MCP_RECEIPTS_PATH,
    PLUGIN_ROOT,
    ensure_data_dirs,
)
from usr.plugins.sem_review_loop.helpers.project_scope import (
    ProjectScope,
    ProjectScopeError,
    make_scope,
)


SERVER_NAME = "sem_review_loop"
LAUNCHER_MODULE = "usr.plugins.sem_review_loop.helpers.mcp_launcher"
NATIVE_TOOLS = frozenset(
    {
        "sem_entities",
        "sem_diff",
        "sem_blame",
        "sem_impact",
        "sem_log",
        "sem_context",
    }
)
FOCUSED_TOOL_NAMES = frozenset({"sem_diff", "sem_context", "sem_impact"})
FOCUSED_TOOLS = frozenset(
    f"{SERVER_NAME}.{name}" for name in FOCUSED_TOOL_NAMES
)
DISABLED_TOOLS = ["sem_entities", "sem_blame", "sem_log"]
MAX_CONFIG_BYTES = 1024 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_RECEIPTS = 1024
PREVIEW_TOKEN_TTL_SECONDS = 300.0
MAX_PREVIEW_TOKENS = 256
MAX_READINESS_ENTRIES = 1024
MAX_ERROR_CHARS = 500
PROJECT_GATE_POLL_SECONDS = 0.005
HASH_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")
IDENTIFIER_PATTERN = re.compile(r"\A[A-Za-z0-9._-]{1,128}\Z")
RECEIPT_STATES = frozenset(
    {"enabling", "verifying", "enabled", "disabling"}
)
MISSING_DIGEST = hashlib.sha256(b"\x00").hexdigest()
_DIR_FD_FUNCTIONS = getattr(os, "supports_dir_fd", set())
_FOLLOW_SYMLINK_FUNCTIONS = getattr(
    os,
    "supports_follow_symlinks",
    set(),
)
_HAS_SECURE_DIR_FD_OPERATIONS = (
    os.open in _DIR_FD_FUNCTIONS
    and os.stat in _DIR_FD_FUNCTIONS
    and os.stat in _FOLLOW_SYMLINK_FUNCTIONS
    and os.unlink in _DIR_FD_FUNCTIONS
    and os.rename in _DIR_FD_FUNCTIONS
)


class MCPManagerError(RuntimeError):
    """A bounded MCP lifecycle failure."""


class MCPConflictError(MCPManagerError):
    """The requested mutation would overwrite user-owned state."""


class MCPVerificationError(MCPManagerError):
    """The focused native tools did not become available."""


class MCPStalePreviewError(MCPConflictError):
    """The explicit preview no longer matches current state."""


class MCPConcurrentModificationError(MCPConflictError):
    """The project configuration changed during a transaction."""


class MCPDriftError(MCPConflictError):
    """A managed entry or project identity has drifted."""


class MCPRollbackError(MCPManagerError):
    """A transaction could not be restored safely."""


def _bounded_error(value: object) -> str:
    return " ".join(str(value).split())[:MAX_ERROR_CHARS]


def canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_digest(exists: bool, raw: bytes) -> str:
    return hashlib.sha256((b"\x01" if exists else b"\x00") + raw).hexdigest()


def _reject_duplicate_pairs(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise MCPManagerError(
                f"Project MCP configuration contains duplicate key {key!r}."
            )
        value[key] = item
    return value


def parse_document(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise MCPManagerError(
                "Project MCP configuration is not valid UTF-8."
            ) from exc
    else:
        text = raw
    if not text.strip():
        raise MCPManagerError("Project MCP configuration is empty.")
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_pairs)
    except MCPManagerError:
        raise
    except json.JSONDecodeError as exc:
        raise MCPManagerError(
            "Project MCP configuration is not valid JSON."
        ) from exc
    if not isinstance(value, dict):
        raise MCPManagerError(
            "Project MCP configuration must be a JSON object."
        )
    servers = value.get("mcpServers")
    if servers is None:
        value["mcpServers"] = {}
    elif not isinstance(servers, dict):
        raise MCPManagerError("mcpServers must be a JSON object.")
    return value


def serialize_document(document: Mapping[str, Any]) -> bytes:
    try:
        encoded = (
            json.dumps(
                dict(document),
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MCPManagerError(
            "Project MCP configuration contains unsupported values."
        ) from exc
    if len(encoded) > MAX_CONFIG_BYTES:
        raise MCPManagerError("Project MCP configuration exceeds the size limit.")
    return encoded


@dataclass(frozen=True)
class ConfigSnapshot:
    path: Path
    exists: bool
    raw: bytes
    digest: str
    mode: int = 0o600
    parent_identity: tuple[int, int] | None = None


class MCPConfigStore(Protocol):
    def read(self, scope: ProjectScope) -> ConfigSnapshot: ...

    def compare_and_swap(
        self,
        scope: ProjectScope,
        expected: ConfigSnapshot,
        replacement: bytes | None,
    ) -> ConfigSnapshot: ...


@dataclass
class _PathLockState:
    lock: threading.RLock
    references: int = 0


_PATH_LOCKS: dict[str, _PathLockState] = {}
_PATH_LOCKS_GUARD = threading.Lock()


@contextmanager
def _path_gate(path: Path) -> Iterator[None]:
    key = os.path.normcase(str(path))
    with _PATH_LOCKS_GUARD:
        state = _PATH_LOCKS.get(key)
        if state is None:
            state = _PathLockState(lock=threading.RLock())
            _PATH_LOCKS[key] = state
        state.references += 1
    acquired = False
    try:
        state.lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            state.lock.release()
        with _PATH_LOCKS_GUARD:
            state.references -= 1
            if state.references == 0 and _PATH_LOCKS.get(key) is state:
                _PATH_LOCKS.pop(key, None)


@dataclass(frozen=True)
class _DirectoryTarget:
    path: Path
    identity: tuple[int, int]


@dataclass(frozen=True)
class _PinnedDirectory:
    path: Path
    identity: tuple[int, int]
    descriptor: int | None = None
    windows_handle: int | None = None

    @property
    def config_path(self) -> Path:
        return self.path / projects.PROJECT_MCP_SERVERS_FILE


class DirectProjectMCPStore:
    """Exact-byte MCP storage pinned to the validated metadata directory.

    Mutations atomically capture the namespace value present at the final
    boundary, compare that captured value, and restore it on conflict. This
    prevents a boundary edit from being overwritten, but it cannot make
    uncoordinated in-place writers participate in a linearizable byte CAS
    across the later comparison/restoration interval. Platforms or filesystems
    without atomic exchange and no-replace primitives fail before changing the
    configured path.
    """

    @staticmethod
    def _is_link_like(path_metadata: os.stat_result) -> bool:
        if stat.S_ISLNK(path_metadata.st_mode):
            return True
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(path_metadata, "st_file_attributes", 0)
        return bool(reparse and attributes & reparse)

    @staticmethod
    def _identity(path_metadata: os.stat_result) -> tuple[int, int]:
        identity = (int(path_metadata.st_dev), int(path_metadata.st_ino))
        if identity == (0, 0):
            raise MCPManagerError(
                "The platform cannot identify the project metadata directory."
            )
        return identity

    @classmethod
    def _metadata_directory(cls, scope: ProjectScope) -> _DirectoryTarget:
        try:
            project_name = projects.validate_project_name(scope.project_name)
        except ValueError as exc:
            raise MCPManagerError("The Agent Zero project name is invalid.") from exc

        expected_root = Path(projects.get_project_folder(project_name))
        try:
            expected_metadata = expected_root.lstat()
            resolved_root = expected_root.resolve(strict=True)
            scoped_root = Path(scope.project_root).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise MCPManagerError("The Agent Zero project is unavailable.") from exc
        if cls._is_link_like(expected_metadata):
            raise MCPManagerError("The Agent Zero project root may not be a symlink.")
        if not stat.S_ISDIR(expected_metadata.st_mode) or resolved_root != scoped_root:
            raise MCPManagerError("The MCP scope no longer matches this project.")

        try:
            projects_parent = Path(
                projects.get_projects_parent_folder()
            ).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise MCPManagerError("The Agent Zero project root is unavailable.") from exc
        if resolved_root.parent != projects_parent:
            raise MCPManagerError(
                "The MCP configuration path escaped the projects directory."
            )

        metadata_dir = resolved_root / projects.PROJECT_META_DIR
        try:
            metadata = metadata_dir.lstat()
            resolved_metadata = metadata_dir.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise MCPManagerError(
                "The Agent Zero project metadata directory is unavailable."
            ) from exc
        if (
            cls._is_link_like(metadata)
            or not stat.S_ISDIR(metadata.st_mode)
            or resolved_metadata.parent != resolved_root
        ):
            raise MCPManagerError(
                "The Agent Zero project metadata path is unsafe."
            )
        return _DirectoryTarget(
            path=resolved_metadata,
            identity=cls._identity(metadata),
        )

    @classmethod
    def _assert_directory_identity(
        cls,
        target: _DirectoryTarget,
    ) -> os.stat_result:
        try:
            metadata = target.path.lstat()
            resolved = target.path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise MCPManagerError(
                "The Agent Zero project metadata directory changed."
            ) from exc
        if (
            cls._is_link_like(metadata)
            or not stat.S_ISDIR(metadata.st_mode)
            or resolved != target.path
            or cls._identity(metadata) != target.identity
        ):
            raise MCPManagerError(
                "The Agent Zero project metadata directory changed."
            )
        return metadata

    @staticmethod
    def _supports_secure_dir_fd() -> bool:
        return (
            os.name != "nt"
            and bool(getattr(os, "O_NOFOLLOW", 0))
            and _HAS_SECURE_DIR_FD_OPERATIONS
            and callable(getattr(os, "fchmod", None))
        )

    @staticmethod
    def _open_windows_handle(
        path: Path,
        *,
        directory: bool,
    ) -> int:
        if os.name != "nt":
            raise MCPManagerError(
                "Windows handle access is unavailable on this platform."
            )
        try:
            import ctypes
            from ctypes import wintypes

            create_file = ctypes.WinDLL(
                "kernel32",
                use_last_error=True,
            ).CreateFileW
            create_file.argtypes = (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            )
            create_file.restype = wintypes.HANDLE
            desired_access = 0x00000080 if directory else 0x80000000
            share_without_delete = 0x00000001 | 0x00000002
            flags = 0x00200000
            if directory:
                flags |= 0x02000000
            raw_handle = create_file(
                str(path),
                desired_access,
                share_without_delete,
                None,
                3,
                flags,
                None,
            )
            handle = ctypes.cast(
                raw_handle,
                ctypes.c_void_p,
            ).value
            invalid_handle = ctypes.c_void_p(-1).value
            if handle is None or handle == invalid_handle:
                error = ctypes.get_last_error()
                raise ctypes.WinError(error)
            return int(handle)
        except OSError:
            raise
        except Exception as exc:
            raise MCPManagerError(
                "Secure Windows file access is unavailable."
            ) from exc

    @staticmethod
    def _close_windows_handle(handle: int) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            close_handle = ctypes.WinDLL(
                "kernel32",
                use_last_error=True,
            ).CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            close_handle(handle)
        except Exception:
            pass

    @classmethod
    @contextmanager
    def _pinned_directory(
        cls,
        target: _DirectoryTarget,
    ) -> Iterator[_PinnedDirectory]:
        cls._assert_directory_identity(target)
        if os.name == "nt":
            try:
                handle = cls._open_windows_handle(
                    target.path,
                    directory=True,
                )
            except OSError as exc:
                raise MCPManagerError(
                    "Unable to pin the project metadata directory."
                ) from exc
            try:
                cls._assert_directory_identity(target)
                try:
                    yield _PinnedDirectory(
                        path=target.path,
                        identity=target.identity,
                        windows_handle=handle,
                    )
                finally:
                    cls._assert_directory_identity(target)
            finally:
                cls._close_windows_handle(handle)
            return

        if not cls._supports_secure_dir_fd():
            raise MCPManagerError(
                "Secure no-symlink directory-relative MCP access is "
                "unavailable on this platform."
            )
        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target.path, flags)
        except OSError as exc:
            raise MCPManagerError(
                "Unable to pin the project metadata directory."
            ) from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or cls._identity(opened) != target.identity
            ):
                raise MCPManagerError(
                    "The Agent Zero project metadata directory changed."
                )
            cls._assert_directory_identity(target)
            try:
                yield _PinnedDirectory(
                    path=target.path,
                    identity=target.identity,
                    descriptor=descriptor,
                )
            finally:
                cls._assert_directory_identity(target)
        finally:
            os.close(descriptor)

    @classmethod
    def _read_relative(
        cls,
        pinned: _PinnedDirectory,
        name: str = projects.PROJECT_MCP_SERVERS_FILE,
    ) -> ConfigSnapshot:
        descriptor = pinned.descriptor
        if descriptor is None:
            raise MCPManagerError(
                "Secure directory-relative MCP access is unavailable."
            )
        path = pinned.path / name
        try:
            path_metadata = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return ConfigSnapshot(
                path=path,
                exists=False,
                raw=b"",
                digest=MISSING_DIGEST,
                parent_identity=pinned.identity,
            )
        except OSError as exc:
            raise MCPManagerError(
                "Unable to inspect the project MCP configuration."
            ) from exc
        if cls._is_link_like(path_metadata):
            raise MCPManagerError(
                "Project MCP configuration may not be a symlink."
            )
        if not stat.S_ISREG(path_metadata.st_mode):
            raise MCPManagerError(
                "Project MCP configuration must be a regular file."
            )

        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_BINARY", 0)
        try:
            opened_descriptor = os.open(
                name,
                flags,
                dir_fd=descriptor,
            )
        except OSError as exc:
            raise MCPManagerError(
                "Unable to read the project MCP configuration."
            ) from exc
        try:
            with os.fdopen(opened_descriptor, "rb", closefd=True) as handle:
                metadata = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or not os.path.samestat(path_metadata, metadata)
                ):
                    raise MCPManagerError(
                        "Project MCP configuration changed while being opened."
                    )
                try:
                    current_path_metadata = os.stat(
                        name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise MCPManagerError(
                        "Project MCP configuration changed while being opened."
                    ) from exc
                if (
                    cls._is_link_like(current_path_metadata)
                    or not stat.S_ISREG(current_path_metadata.st_mode)
                    or not os.path.samestat(current_path_metadata, metadata)
                ):
                    raise MCPManagerError(
                        "Project MCP configuration changed while being opened."
                    )
                if metadata.st_size > MAX_CONFIG_BYTES:
                    raise MCPManagerError(
                        "Project MCP configuration exceeds the size limit."
                    )
                raw = handle.read(MAX_CONFIG_BYTES + 1)
        except MCPManagerError:
            raise
        except OSError as exc:
            raise MCPManagerError(
                "Unable to read the project MCP configuration."
            ) from exc
        if len(raw) > MAX_CONFIG_BYTES:
            raise MCPManagerError(
                "Project MCP configuration exceeds the size limit."
            )
        return ConfigSnapshot(
            path=path,
            exists=True,
            raw=raw,
            digest=_snapshot_digest(True, raw),
            mode=stat.S_IMODE(metadata.st_mode),
            parent_identity=pinned.identity,
        )

    @classmethod
    def _read_windows(
        cls,
        pinned: _PinnedDirectory,
        name: str = projects.PROJECT_MCP_SERVERS_FILE,
    ) -> ConfigSnapshot:
        path = pinned.path / name
        try:
            path_metadata = path.lstat()
        except FileNotFoundError:
            return ConfigSnapshot(
                path=path,
                exists=False,
                raw=b"",
                digest=MISSING_DIGEST,
                parent_identity=pinned.identity,
            )
        except OSError as exc:
            raise MCPManagerError(
                "Unable to inspect the project MCP configuration."
            ) from exc
        if cls._is_link_like(path_metadata):
            raise MCPManagerError(
                "Project MCP configuration may not be a symlink."
            )
        if not stat.S_ISREG(path_metadata.st_mode):
            raise MCPManagerError(
                "Project MCP configuration must be a regular file."
            )

        try:
            handle = cls._open_windows_handle(path, directory=False)
        except OSError as exc:
            raise MCPManagerError(
                "Unable to read the project MCP configuration."
            ) from exc
        descriptor: int | None = None
        try:
            import msvcrt

            descriptor = msvcrt.open_osfhandle(
                handle,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
            handle = -1
            with os.fdopen(descriptor, "rb", closefd=True) as file_handle:
                descriptor = None
                metadata = os.fstat(file_handle.fileno())
                try:
                    current_path_metadata = path.lstat()
                except OSError as exc:
                    raise MCPManagerError(
                        "Project MCP configuration changed while being opened."
                    ) from exc
                if (
                    cls._is_link_like(metadata)
                    or cls._is_link_like(current_path_metadata)
                    or not stat.S_ISREG(metadata.st_mode)
                    or not stat.S_ISREG(current_path_metadata.st_mode)
                    or not os.path.samestat(path_metadata, metadata)
                    or not os.path.samestat(current_path_metadata, metadata)
                ):
                    raise MCPManagerError(
                        "Project MCP configuration changed while being opened."
                    )
                if metadata.st_size > MAX_CONFIG_BYTES:
                    raise MCPManagerError(
                        "Project MCP configuration exceeds the size limit."
                    )
                raw = file_handle.read(MAX_CONFIG_BYTES + 1)
        except MCPManagerError:
            raise
        except (OSError, ImportError) as exc:
            raise MCPManagerError(
                "Unable to read the project MCP configuration."
            ) from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if handle != -1:
                cls._close_windows_handle(handle)
        if len(raw) > MAX_CONFIG_BYTES:
            raise MCPManagerError(
                "Project MCP configuration exceeds the size limit."
            )
        return ConfigSnapshot(
            path=path,
            exists=True,
            raw=raw,
            digest=_snapshot_digest(True, raw),
            mode=stat.S_IMODE(metadata.st_mode),
            parent_identity=pinned.identity,
        )

    @classmethod
    def _read_pinned(
        cls,
        pinned: _PinnedDirectory,
        name: str = projects.PROJECT_MCP_SERVERS_FILE,
    ) -> ConfigSnapshot:
        if pinned.descriptor is not None:
            return cls._read_relative(pinned, name)
        if pinned.windows_handle is not None:
            return cls._read_windows(pinned, name)
        raise MCPManagerError("The project metadata directory is not pinned.")

    def read(self, scope: ProjectScope) -> ConfigSnapshot:
        target = self._metadata_directory(scope)
        path = target.path / projects.PROJECT_MCP_SERVERS_FILE
        with _path_gate(path):
            with self._pinned_directory(target) as pinned:
                return self._read_pinned(pinned)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _atomic_replace(
        cls,
        path: Path,
        raw: bytes,
        mode: int,
    ) -> None:
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                dir=path.parent,
                prefix=".sem-review-mcp-",
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                fchmod = getattr(os, "fchmod", None)
                if os.name != "nt" and callable(fchmod):
                    fchmod(handle.fileno(), mode & 0o777)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if temporary.is_symlink():
                raise MCPManagerError(
                    "The temporary MCP configuration path is unsafe."
                )
            os.replace(temporary, path)
            cls._fsync_directory(path.parent)
        except MCPManagerError:
            raise
        except OSError as exc:
            raise MCPManagerError(
                "Unable to atomically update the project MCP configuration."
            ) from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _atomic_primitive_unavailable(error: OSError) -> bool:
        unsupported = {
            errno.ENOSYS,
            errno.EINVAL,
            errno.ENOTSUP,
            errno.EOPNOTSUPP,
        }
        return error.errno in unsupported

    @classmethod
    def _rename_with_flags_relative(
        cls,
        pinned: _PinnedDirectory,
        source_name: str,
        destination_name: str,
        *,
        exchange: bool,
    ) -> None:
        directory_descriptor = pinned.descriptor
        if directory_descriptor is None:
            raise MCPManagerError(
                "Secure directory-relative MCP access is unavailable."
            )
        try:
            import ctypes

            library = ctypes.CDLL(None, use_errno=True)
            if sys.platform.startswith("linux"):
                rename = getattr(library, "renameat2", None)
                flag = 2 if exchange else 1
            elif sys.platform == "darwin":
                rename = getattr(library, "renameatx_np", None)
                flag = 0x00000002 if exchange else 0x00000004
            else:
                rename = None
                flag = 0
            if rename is None:
                raise MCPManagerError(
                    "Atomic filesystem compare-and-swap is unavailable on "
                    "this platform."
                )
            rename.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            rename.restype = ctypes.c_int
            result = rename(
                directory_descriptor,
                os.fsencode(source_name),
                directory_descriptor,
                os.fsencode(destination_name),
                flag,
            )
            if result != 0:
                error_number = ctypes.get_errno()
                raise OSError(
                    error_number,
                    os.strerror(error_number),
                    destination_name,
                )
        except MCPManagerError:
            raise
        except OSError as exc:
            if cls._atomic_primitive_unavailable(exc):
                raise MCPManagerError(
                    "Atomic filesystem compare-and-swap is unavailable on "
                    "this filesystem."
                ) from exc
            raise
        except Exception as exc:
            raise MCPManagerError(
                "Atomic filesystem compare-and-swap is unavailable on this "
                "platform."
            ) from exc

    @staticmethod
    def _windows_move_no_replace(source: Path, destination: Path) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            move_file = ctypes.WinDLL(
                "kernel32",
                use_last_error=True,
            ).MoveFileExW
            move_file.argtypes = (
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
            )
            move_file.restype = wintypes.BOOL
            if not move_file(str(source), str(destination), 0x00000008):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError:
            raise
        except Exception as exc:
            raise MCPManagerError(
                "Atomic filesystem compare-and-swap is unavailable on "
                "Windows."
            ) from exc

    @classmethod
    def _windows_replace_with_backup(
        cls,
        destination: Path,
        replacement: Path,
        captured: Path,
    ) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            replace_file = ctypes.WinDLL(
                "kernel32",
                use_last_error=True,
            ).ReplaceFileW
            replace_file.argtypes = (
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.LPVOID,
            )
            replace_file.restype = wintypes.BOOL
            if not replace_file(
                str(destination),
                str(replacement),
                str(captured),
                0,
                None,
                None,
            ):
                error_number = ctypes.get_last_error()
                if error_number == 1177 and captured.exists():
                    try:
                        cls._windows_move_no_replace(
                            captured,
                            destination,
                        )
                    except BaseException as rollback_error:
                        raise MCPRollbackError(
                            "Windows replacement failed after capturing the "
                            "user file, and restoration could not complete."
                        ) from rollback_error
                raise ctypes.WinError(error_number)
        except OSError:
            raise
        except Exception as exc:
            raise MCPManagerError(
                "Atomic filesystem compare-and-swap is unavailable on "
                "Windows."
            ) from exc

    @classmethod
    def _unused_name_pinned(
        cls,
        pinned: _PinnedDirectory,
        *,
        conflict: bool = False,
    ) -> str:
        for _attempt in range(32):
            if conflict:
                name = (
                    ".sem-review-mcp-conflict-"
                    f"{secrets.token_hex(16)}.json"
                )
            else:
                name = f".sem-review-mcp-{secrets.token_hex(16)}"
            try:
                if pinned.descriptor is not None:
                    os.stat(
                        name,
                        dir_fd=pinned.descriptor,
                        follow_symlinks=False,
                    )
                elif pinned.windows_handle is not None:
                    (pinned.path / name).lstat()
                else:
                    raise MCPManagerError(
                        "The project metadata directory is not pinned."
                    )
            except FileNotFoundError:
                return name
            except MCPManagerError:
                raise
            except OSError as exc:
                raise MCPManagerError(
                    "Unable to allocate an MCP transaction path."
                ) from exc
        raise MCPManagerError(
            "Unable to allocate an MCP transaction path."
        )

    @classmethod
    def _retain_conflict_pinned(
        cls,
        pinned: _PinnedDirectory,
        captured_name: str,
    ) -> str:
        if captured_name.startswith(".sem-review-mcp-conflict-"):
            cls._fsync_pinned(pinned)
            return captured_name
        retained_name = cls._unused_name_pinned(
            pinned,
            conflict=True,
        )
        cls._rename_no_replace_pinned(
            pinned,
            captured_name,
            retained_name,
        )
        cls._fsync_pinned(pinned)
        return retained_name

    @classmethod
    def _prepare_pinned(
        cls,
        pinned: _PinnedDirectory,
        raw: bytes,
        mode: int,
        *,
        conflict_capture: bool = False,
    ) -> str:
        if pinned.descriptor is not None:
            name = cls._unused_name_pinned(
                pinned,
                conflict=conflict_capture,
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            try:
                descriptor = os.open(
                    name,
                    flags,
                    mode & 0o777,
                    dir_fd=pinned.descriptor,
                )
            except OSError as exc:
                raise MCPManagerError(
                    "Unable to allocate a temporary MCP configuration."
                ) from exc
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    os.fchmod(handle.fileno(), mode & 0o777)
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                    written = os.fstat(handle.fileno())
                metadata = os.stat(
                    name,
                    dir_fd=pinned.descriptor,
                    follow_symlinks=False,
                )
                if (
                    cls._is_link_like(metadata)
                    or not stat.S_ISREG(metadata.st_mode)
                    or not os.path.samestat(metadata, written)
                ):
                    raise MCPManagerError(
                        "The temporary MCP configuration path is unsafe."
                    )
                return name
            except BaseException:
                try:
                    os.unlink(name, dir_fd=pinned.descriptor)
                except OSError:
                    pass
                raise

        if pinned.windows_handle is not None:
            temporary: Path | None = None
            try:
                descriptor, raw_name = tempfile.mkstemp(
                    dir=pinned.path,
                    prefix=(
                        ".sem-review-mcp-conflict-"
                        if conflict_capture
                        else ".sem-review-mcp-"
                    ),
                    suffix=".json" if conflict_capture else "",
                )
                temporary = Path(raw_name)
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                    written = os.fstat(handle.fileno())
                metadata = temporary.lstat()
                if (
                    cls._is_link_like(metadata)
                    or not stat.S_ISREG(metadata.st_mode)
                    or not os.path.samestat(metadata, written)
                ):
                    raise MCPManagerError(
                        "The temporary MCP configuration path is unsafe."
                    )
                return temporary.name
            except BaseException:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
        raise MCPManagerError("The project metadata directory is not pinned.")

    @staticmethod
    def _unlink_named_pinned(
        pinned: _PinnedDirectory,
        name: str,
    ) -> None:
        try:
            if pinned.descriptor is not None:
                os.unlink(name, dir_fd=pinned.descriptor)
                return
            if pinned.windows_handle is not None:
                (pinned.path / name).unlink()
                return
            raise MCPManagerError(
                "The project metadata directory is not pinned."
            )
        except FileNotFoundError:
            return
        except MCPManagerError:
            raise
        except OSError as exc:
            raise MCPManagerError(
                "Unable to clean up an MCP transaction path."
            ) from exc

    @staticmethod
    def _fsync_pinned(pinned: _PinnedDirectory) -> None:
        if pinned.descriptor is not None:
            os.fsync(pinned.descriptor)
        elif pinned.windows_handle is None:
            raise MCPManagerError(
                "The project metadata directory is not pinned."
            )

    @classmethod
    def _rename_no_replace_pinned(
        cls,
        pinned: _PinnedDirectory,
        source_name: str,
        destination_name: str,
    ) -> None:
        if pinned.descriptor is not None:
            cls._rename_with_flags_relative(
                pinned,
                source_name,
                destination_name,
                exchange=False,
            )
            return
        if pinned.windows_handle is not None:
            cls._windows_move_no_replace(
                pinned.path / source_name,
                pinned.path / destination_name,
            )
            return
        raise MCPManagerError("The project metadata directory is not pinned.")

    @classmethod
    def _install_if_missing_pinned(
        cls,
        pinned: _PinnedDirectory,
        prepared_name: str,
    ) -> None:
        cls._rename_no_replace_pinned(
            pinned,
            prepared_name,
            projects.PROJECT_MCP_SERVERS_FILE,
        )
        cls._fsync_pinned(pinned)

    @classmethod
    def _capture_replace_pinned(
        cls,
        pinned: _PinnedDirectory,
        prepared_name: str,
    ) -> str:
        if pinned.descriptor is not None:
            cls._rename_with_flags_relative(
                pinned,
                prepared_name,
                projects.PROJECT_MCP_SERVERS_FILE,
                exchange=True,
            )
            cls._fsync_pinned(pinned)
            return prepared_name
        if pinned.windows_handle is not None:
            captured_name = cls._unused_name_pinned(
                pinned,
                conflict=True,
            )
            cls._windows_replace_with_backup(
                pinned.config_path,
                pinned.path / prepared_name,
                pinned.path / captured_name,
            )
            return captured_name
        raise MCPManagerError("The project metadata directory is not pinned.")

    @classmethod
    def _capture_delete_pinned(
        cls,
        pinned: _PinnedDirectory,
        captured_name: str,
    ) -> None:
        cls._rename_no_replace_pinned(
            pinned,
            projects.PROJECT_MCP_SERVERS_FILE,
            captured_name,
        )
        cls._fsync_pinned(pinned)

    @classmethod
    def _restore_replaced_pinned(
        cls,
        pinned: _PinnedDirectory,
        captured_name: str,
        expected_displaced: bytes,
    ) -> None:
        try:
            displaced_name = cls._capture_replace_pinned(
                pinned,
                captured_name,
            )
        except FileNotFoundError:
            cls._rename_no_replace_pinned(
                pinned,
                captured_name,
                projects.PROJECT_MCP_SERVERS_FILE,
            )
            cls._fsync_pinned(pinned)
            return
        try:
            displaced = cls._read_pinned(pinned, displaced_name)
        except BaseException as exc:
            raise MCPRollbackError(
                "Project MCP conflict restoration could not inspect the "
                "displaced configured file; it was retained in project "
                "metadata."
            ) from exc
        if (
            displaced.exists
            and displaced.raw == expected_displaced
            and displaced.digest
            == _snapshot_digest(True, expected_displaced)
        ):
            cls._unlink_named_pinned(pinned, displaced_name)
            cls._fsync_pinned(pinned)
            return

        try:
            conflict_name = cls._capture_replace_pinned(
                pinned,
                displaced_name,
            )
            retained_name = cls._retain_conflict_pinned(
                pinned,
                conflict_name,
            )
        except BaseException as exc:
            raise MCPRollbackError(
                "Project MCP conflict restoration encountered another "
                "writer; captured bytes were retained in project metadata."
            ) from exc
        raise MCPRollbackError(
            "Project MCP conflict restoration encountered another writer; "
            f"captured bytes were retained as {retained_name} in project "
            "metadata."
        )

    @classmethod
    def _restore_deleted_pinned(
        cls,
        pinned: _PinnedDirectory,
        captured_name: str,
    ) -> None:
        cls._rename_no_replace_pinned(
            pinned,
            captured_name,
            projects.PROJECT_MCP_SERVERS_FILE,
        )
        cls._fsync_pinned(pinned)

    def compare_and_swap(
        self,
        scope: ProjectScope,
        expected: ConfigSnapshot,
        replacement: bytes | None,
    ) -> ConfigSnapshot:
        target = self._metadata_directory(scope)
        path = target.path / projects.PROJECT_MCP_SERVERS_FILE
        if path != expected.path:
            raise MCPConcurrentModificationError(
                "The MCP configuration path changed during the operation."
            )
        with _path_gate(path):
            with self._pinned_directory(target) as pinned:
                if expected.parent_identity != pinned.identity:
                    raise MCPConcurrentModificationError(
                        "The project metadata directory changed; preview it "
                        "again."
                    )
                current = self._read_pinned(pinned)
                if (
                    current.exists != expected.exists
                    or current.digest != expected.digest
                    or current.raw != expected.raw
                ):
                    raise MCPConcurrentModificationError(
                        "Project MCP configuration changed; preview it again."
                    )
                if (
                    replacement is not None
                    and len(replacement) > MAX_CONFIG_BYTES
                ):
                    raise MCPManagerError(
                        "Project MCP configuration exceeds the size limit."
                    )

                if replacement is None:
                    if not current.exists:
                        return current
                    captured_name = self._unused_name_pinned(
                        pinned,
                        conflict=True,
                    )
                    try:
                        try:
                            self._capture_delete_pinned(
                                pinned,
                                captured_name,
                            )
                        except FileNotFoundError as exc:
                            raise MCPConcurrentModificationError(
                                "Project MCP configuration changed; preview it "
                                "again."
                            ) from exc
                        except OSError as exc:
                            raise MCPManagerError(
                                "Unable to atomically capture the project MCP "
                                "configuration."
                            ) from exc

                        try:
                            captured = self._read_pinned(
                                pinned,
                                captured_name,
                            )
                        except BaseException as compare_error:
                            try:
                                target_after_error = self._read_pinned(pinned)
                                if not target_after_error.exists:
                                    self._restore_deleted_pinned(
                                        pinned,
                                        captured_name,
                                    )
                                    captured_name = ""
                                else:
                                    retained_name = (
                                        self._retain_conflict_pinned(
                                            pinned,
                                            captured_name,
                                        )
                                    )
                                    captured_name = ""
                                    raise MCPRollbackError(
                                        "Project MCP deletion conflict could "
                                        "not inspect captured bytes while the "
                                        "configured path also changed; the "
                                        "bytes were retained as "
                                        f"{retained_name} in project "
                                        "metadata. Review both files before "
                                        "retrying."
                                    )
                            except BaseException as rollback_error:
                                captured_name = ""
                                if isinstance(
                                    rollback_error,
                                    MCPRollbackError,
                                ):
                                    raise
                                raise MCPRollbackError(
                                    "Project MCP deletion conflict could not be "
                                    "restored safely; the captured user file "
                                    "was retained in project metadata."
                                ) from rollback_error
                            raise MCPConcurrentModificationError(
                                "Project MCP configuration changed at the "
                                "deletion boundary."
                            ) from compare_error

                        captured_matches = (
                            captured.exists == expected.exists
                            and captured.digest == expected.digest
                            and captured.raw == expected.raw
                        )
                        try:
                            target_after_capture = self._read_pinned(pinned)
                        except BaseException as inspect_error:
                            retained_name = captured_name
                            captured_name = ""
                            raise MCPRollbackError(
                                "Project MCP deletion could not verify the "
                                "configured path after capture; the captured "
                                f"user file was retained as {retained_name}."
                            ) from inspect_error
                        if not captured_matches:
                            if not target_after_capture.exists:
                                try:
                                    self._restore_deleted_pinned(
                                        pinned,
                                        captured_name,
                                    )
                                    captured_name = ""
                                except BaseException as rollback_error:
                                    captured_name = ""
                                    raise MCPRollbackError(
                                        "Project MCP deletion conflict could "
                                        "not be restored safely; the captured "
                                        "user file was retained in project "
                                        "metadata."
                                    ) from rollback_error
                            else:
                                try:
                                    retained_name = (
                                        self._retain_conflict_pinned(
                                            pinned,
                                            captured_name,
                                        )
                                    )
                                    captured_name = ""
                                except BaseException as retain_error:
                                    captured_name = ""
                                    raise MCPRollbackError(
                                        "Project MCP deletion conflict "
                                        "captured user bytes that could not "
                                        "be conflict-marked; they were "
                                        "retained in project metadata."
                                    ) from retain_error
                                raise MCPRollbackError(
                                    "Project MCP deletion conflict captured "
                                    "external bytes while the configured path "
                                    "also changed; the bytes were retained as "
                                    f"{retained_name} in project metadata. "
                                    "Review both files before retrying."
                                )
                            raise MCPConcurrentModificationError(
                                "Project MCP configuration changed at the "
                                "deletion boundary."
                            )
                        if target_after_capture.exists:
                            raise MCPConcurrentModificationError(
                                "Project MCP configuration changed during "
                                "deletion."
                            )
                        self._unlink_named_pinned(pinned, captured_name)
                        captured_name = ""
                        self._fsync_pinned(pinned)
                        return target_after_capture
                    finally:
                        if captured_name:
                            self._unlink_named_pinned(pinned, captured_name)

                prepared_name = self._prepare_pinned(
                    pinned,
                    replacement,
                    current.mode if current.exists else 0o600,
                    conflict_capture=current.exists,
                )
                captured_name = ""
                try:
                    if not current.exists:
                        try:
                            self._install_if_missing_pinned(
                                pinned,
                                prepared_name,
                            )
                            prepared_name = ""
                        except FileExistsError as exc:
                            raise MCPConcurrentModificationError(
                                "Project MCP configuration changed at the "
                                "creation boundary."
                            ) from exc
                        except OSError as exc:
                            raise MCPManagerError(
                                "Unable to atomically create the project MCP "
                                "configuration."
                            ) from exc
                        installed = self._read_pinned(pinned)
                        if (
                            not installed.exists
                            or installed.raw != replacement
                            or installed.digest
                            != _snapshot_digest(True, replacement)
                        ):
                            raise MCPConcurrentModificationError(
                                "Project MCP configuration changed during "
                                "creation."
                            )
                        return installed

                    try:
                        captured_name = self._capture_replace_pinned(
                            pinned,
                            prepared_name,
                        )
                        prepared_name = ""
                    except FileNotFoundError as exc:
                        raise MCPConcurrentModificationError(
                            "Project MCP configuration changed at the "
                            "replacement boundary."
                        ) from exc
                    except OSError as exc:
                        raise MCPManagerError(
                            "Unable to atomically capture the project MCP "
                            "configuration."
                        ) from exc

                    try:
                        captured = self._read_pinned(
                            pinned,
                            captured_name,
                        )
                        captured_matches = (
                            captured.exists == expected.exists
                            and captured.digest == expected.digest
                            and captured.raw == expected.raw
                        )
                    except BaseException:
                        captured_matches = False

                    try:
                        installed = self._read_pinned(pinned)
                    except BaseException as inspect_error:
                        retained_name = captured_name
                        captured_name = ""
                        raise MCPRollbackError(
                            "Project MCP replacement could not verify the "
                            "configured path after capture; the captured user "
                            f"file was retained as {retained_name}."
                        ) from inspect_error
                    installed_matches = (
                        installed.exists
                        and installed.raw == replacement
                        and installed.digest
                        == _snapshot_digest(True, replacement)
                    )
                    if not captured_matches:
                        if installed_matches:
                            try:
                                self._restore_replaced_pinned(
                                    pinned,
                                    captured_name,
                                    replacement,
                                )
                                captured_name = ""
                            except BaseException as rollback_error:
                                captured_name = ""
                                if isinstance(
                                    rollback_error,
                                    MCPRollbackError,
                                ):
                                    raise
                                raise MCPRollbackError(
                                    "Project MCP replacement conflict could "
                                    "not be restored safely; the captured user "
                                    "file was retained in project metadata."
                                ) from rollback_error
                        else:
                            try:
                                retained_name = self._retain_conflict_pinned(
                                    pinned,
                                    captured_name,
                                )
                                captured_name = ""
                            except BaseException as retain_error:
                                captured_name = ""
                                raise MCPRollbackError(
                                    "Project MCP replacement conflict "
                                    "captured user bytes that could not be "
                                    "conflict-marked; they were retained in "
                                    "project metadata."
                                ) from retain_error
                            raise MCPRollbackError(
                                "Project MCP replacement conflict captured "
                                "external bytes while the configured path "
                                "also changed; the bytes were retained as "
                                f"{retained_name} in project metadata. "
                                "Review both files before retrying."
                            )
                        raise MCPConcurrentModificationError(
                            "Project MCP configuration changed at the "
                            "replacement boundary."
                        )
                    if not installed_matches:
                        raise MCPConcurrentModificationError(
                            "Project MCP configuration changed during "
                            "replacement."
                        )

                    self._unlink_named_pinned(pinned, captured_name)
                    captured_name = ""
                    self._fsync_pinned(pinned)
                    return installed
                finally:
                    if prepared_name:
                        self._unlink_named_pinned(pinned, prepared_name)
                    if captured_name:
                        self._unlink_named_pinned(pinned, captured_name)


class _CallbackStore:
    """Compatibility adapter for isolated tests; production never uses it."""

    def __init__(
        self,
        load: Callable[[str], str],
        save: Callable[[str, str], None],
    ) -> None:
        self.load = load
        self.save = save

    def read(self, scope: ProjectScope) -> ConfigSnapshot:
        raw = self.load(scope.project_name).encode("utf-8")
        snapshot = ConfigSnapshot(
            path=Path(f"/callback/{scope.project_name}/mcp_servers.json"),
            exists=True,
            raw=raw,
            digest=_snapshot_digest(True, raw),
        )
        return snapshot

    def compare_and_swap(
        self,
        scope: ProjectScope,
        expected: ConfigSnapshot,
        replacement: bytes | None,
    ) -> ConfigSnapshot:
        current = self.read(scope)
        if current.digest != expected.digest or current.raw != expected.raw:
            raise MCPConcurrentModificationError(
                "Project MCP configuration changed; preview it again."
            )
        raw = replacement if replacement is not None else b""
        self.save(scope.project_name, raw.decode("utf-8"))
        return ConfigSnapshot(
            path=current.path,
            exists=replacement is not None,
            raw=raw,
            digest=_snapshot_digest(replacement is not None, raw),
        )


def project_root_identity(root: Path) -> str:
    try:
        resolved = Path(root).resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError) as exc:
        raise MCPManagerError("The Agent Zero project is unavailable.") from exc
    if not resolved.is_dir():
        raise MCPManagerError("The Agent Zero project is not a directory.")
    return hashlib.sha256(
        (
            f"{resolved}\0{metadata.st_dev}\0{metadata.st_ino}"
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class MCPReceipt:
    project_name: str
    project_id: str
    entry_hash: str
    project_root_identity: str = ""
    state: str = "enabled"
    config_before_hash: str = MISSING_DIGEST
    config_after_hash: str = MISSING_DIGEST
    previous_receipt: MCPReceipt | None = None


def _validate_receipt(
    receipt: MCPReceipt,
    *,
    nested: bool = False,
) -> None:
    try:
        projects.validate_project_name(receipt.project_name)
    except ValueError as exc:
        raise MCPManagerError("MCP receipt has an invalid project name.") from exc
    if len(receipt.project_name.encode("utf-8")) > 128:
        raise MCPManagerError("MCP receipt project name exceeds the limit.")
    if IDENTIFIER_PATTERN.fullmatch(receipt.project_id) is None:
        raise MCPManagerError("MCP receipt has an invalid project identity.")
    for value, label in (
        (receipt.entry_hash, "entry"),
        (receipt.project_root_identity, "project root"),
        (receipt.config_before_hash, "before-config"),
        (receipt.config_after_hash, "after-config"),
    ):
        if HASH_PATTERN.fullmatch(value) is None:
            raise MCPManagerError(f"MCP receipt has an invalid {label} hash.")
    if receipt.state not in RECEIPT_STATES:
        raise MCPManagerError("MCP receipt has an invalid transaction state.")
    previous = receipt.previous_receipt
    if previous is None:
        return
    if nested or receipt.state not in {"enabling", "verifying"}:
        raise MCPManagerError("MCP receipt has an invalid prior transaction.")
    _validate_receipt(previous, nested=True)
    if (
        previous.state != "enabled"
        or previous.previous_receipt is not None
        or previous.project_name != receipt.project_name
        or previous.project_id != receipt.project_id
        or previous.project_root_identity != receipt.project_root_identity
    ):
        raise MCPManagerError("MCP receipt has invalid prior ownership.")


class ReceiptStore:
    def __init__(self, path: Path = MCP_RECEIPTS_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _safe_path(self) -> Path:
        ensure_data_dirs()
        try:
            data_root = DATA_ROOT.resolve(strict=True)
            resolved = self.path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise MCPManagerError("MCP receipt storage is unavailable.") from exc
        if resolved == data_root or data_root not in resolved.parents:
            raise MCPManagerError("MCP receipt storage escaped plugin-owned data.")
        parent = resolved.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if parent.is_symlink() or not parent.is_dir():
                raise MCPManagerError("MCP receipt directory is unsafe.")
        except OSError as exc:
            raise MCPManagerError("MCP receipt directory is unavailable.") from exc
        return resolved

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"schema_version": 2, "receipts": {}}

    def _read(self) -> dict[str, Any]:
        path = self._safe_path()
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return self._empty()
        except OSError as exc:
            raise MCPManagerError("MCP receipt store is unreadable.") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise MCPManagerError("MCP receipt store is unsafe.")
        if metadata.st_size > MAX_RECEIPT_BYTES:
            raise MCPManagerError("MCP receipt store exceeds the size limit.")
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    raise MCPManagerError("MCP receipt store is unsafe.")
                if opened.st_size > MAX_RECEIPT_BYTES:
                    raise MCPManagerError(
                        "MCP receipt store exceeds the size limit."
                    )
                raw = handle.read(MAX_RECEIPT_BYTES + 1)
            if len(raw) > MAX_RECEIPT_BYTES:
                raise MCPManagerError(
                    "MCP receipt store exceeds the size limit."
                )
            value = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_reject_duplicate_pairs,
            )
        except MCPManagerError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MCPManagerError("MCP receipt store is unreadable.") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "receipts"}
            or value.get("schema_version") not in {1, 2}
            or not isinstance(value.get("receipts"), dict)
            or len(value["receipts"]) > MAX_RECEIPTS
        ):
            raise MCPManagerError("MCP receipt store is invalid.")
        return value

    def _write(self, value: dict[str, Any]) -> None:
        path = self._safe_path()
        encoded = (
            json.dumps(
                value,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_RECEIPT_BYTES:
            raise MCPManagerError("MCP receipt store exceeds the size limit.")
        DirectProjectMCPStore._atomic_replace(path, encoded, 0o600)

    @staticmethod
    def _decode(
        value: object,
        *,
        key: str | None = None,
        nested: bool = False,
    ) -> MCPReceipt:
        legacy_keys = {
            "project_name",
            "project_id",
            "entry_hash",
            "project_root_identity",
            "state",
            "config_before_hash",
            "config_after_hash",
        }
        if not isinstance(value, dict):
            raise MCPManagerError("MCP receipt record is invalid.")
        record_keys = frozenset(value)
        if record_keys not in {
            frozenset(legacy_keys),
            frozenset({*legacy_keys, "previous_receipt"}),
        }:
            raise MCPManagerError("MCP receipt record is invalid.")
        previous_value = value.get("previous_receipt")
        previous = (
            None
            if previous_value is None
            else ReceiptStore._decode(
                previous_value,
                nested=True,
            )
        )
        receipt = MCPReceipt(
            project_name=str(value["project_name"]),
            project_id=str(value["project_id"]),
            entry_hash=str(value["entry_hash"]),
            project_root_identity=str(value["project_root_identity"]),
            state=str(value["state"]),
            config_before_hash=str(value["config_before_hash"]),
            config_after_hash=str(value["config_after_hash"]),
            previous_receipt=previous,
        )
        _validate_receipt(receipt, nested=nested)
        if key is not None and key != receipt.project_name:
            raise MCPManagerError("MCP receipt ownership key is invalid.")
        return receipt

    @staticmethod
    def _encode(receipt: MCPReceipt) -> dict[str, object]:
        return {
            "project_name": receipt.project_name,
            "project_id": receipt.project_id,
            "entry_hash": receipt.entry_hash,
            "project_root_identity": receipt.project_root_identity,
            "state": receipt.state,
            "config_before_hash": receipt.config_before_hash,
            "config_after_hash": receipt.config_after_hash,
            "previous_receipt": (
                None
                if receipt.previous_receipt is None
                else ReceiptStore._encode(receipt.previous_receipt)
            ),
        }

    def get(self, project_name: str) -> MCPReceipt | None:
        with self._lock:
            value = self._read()["receipts"].get(project_name)
            return (
                None
                if value is None
                else self._decode(value, key=project_name)
            )

    def put(self, receipt: MCPReceipt) -> None:
        _validate_receipt(receipt)
        with self._lock:
            document = self._read()
            document["schema_version"] = 2
            document["receipts"][receipt.project_name] = self._encode(receipt)
            if len(document["receipts"]) > MAX_RECEIPTS:
                raise MCPManagerError("MCP receipt store is full.")
            self._write(document)

    def delete(self, project_name: str) -> None:
        with self._lock:
            document = self._read()
            if document["receipts"].pop(project_name, None) is not None:
                document["schema_version"] = 2
                self._write(document)

    def all(self) -> tuple[MCPReceipt, ...]:
        with self._lock:
            document = self._read()
            receipts = [
                self._decode(value, key=str(key))
                for key, value in document["receipts"].items()
            ]
            return tuple(
                sorted(receipts, key=lambda item: item.project_name)
            )


def _global_config_text() -> str:
    from helpers.settings import get_settings

    value = get_settings().get("mcp_servers", '{"mcpServers": {}}')
    if not isinstance(value, str):
        raise MCPManagerError("Global MCP configuration must be JSON text.")
    return value


def _global_state(
    supplier: Callable[[], str],
) -> tuple[str, frozenset[str]]:
    raw = supplier()
    if not isinstance(raw, str):
        raise MCPManagerError("Global MCP configuration must be JSON text.")
    encoded = raw.encode("utf-8")
    if len(encoded) > MAX_CONFIG_BYTES:
        raise MCPManagerError("Global MCP configuration exceeds the size limit.")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    except MCPManagerError:
        raise
    except json.JSONDecodeError as exc:
        raise MCPManagerError(
            "Global MCP configuration is not valid JSON."
        ) from exc
    names: set[str] = set()
    if isinstance(value, dict) and "mcpServers" in value:
        servers = value["mcpServers"]
        if isinstance(servers, dict):
            names.update(normalize_name(str(name)) for name in servers)
        elif isinstance(servers, list):
            names.update(
                normalize_name(str(item.get("name") or ""))
                for item in servers
                if isinstance(item, dict) and item.get("name")
            )
        else:
            raise MCPManagerError(
                "Global mcpServers must be an object or array."
            )
    elif isinstance(value, dict) and value.get("name"):
        names.add(normalize_name(str(value["name"])))
    elif isinstance(value, list):
        names.update(
            normalize_name(str(item.get("name") or ""))
            for item in value
            if isinstance(item, dict) and item.get("name")
        )
    elif not isinstance(value, (dict, list)):
        raise MCPManagerError("Global MCP configuration is invalid.")
    return _snapshot_digest(True, encoded), frozenset(names)


def _launcher_paths(scope: ProjectScope) -> dict[str, Path]:
    runtime = DATA_ROOT / "mcp-runtime" / scope.project_id
    return {
        "home": runtime / "home",
        "tmp": runtime / "tmp",
        "xdg_cache": runtime / "xdg-cache",
        "xdg_config": runtime / "xdg-config",
        "xdg_data": runtime / "xdg-data",
        "xdg_state": runtime / "xdg-state",
        "appdata": runtime / "appdata",
        "localappdata": runtime / "localappdata",
        "sem_cache": CACHE_ROOT / scope.project_id / "mcp",
    }


def managed_entry(
    scope: ProjectScope,
    config: PluginConfig,
) -> dict[str, Any]:
    project_id = str(scope.project_id)
    if IDENTIFIER_PATTERN.fullmatch(project_id) is None:
        raise MCPManagerError("The project identity is invalid.")
    root = Path(scope.project_root).resolve(strict=True)
    custom_binary = str(config.custom_sem_binary or "")
    if len(custom_binary) > 4096:
        raise MCPManagerError("The custom sem binary path is too long.")
    if custom_binary and not Path(custom_binary).expanduser().is_absolute():
        raise MCPManagerError("Custom sem binary path must be absolute.")
    launcher_paths = _launcher_paths(scope)
    agent_zero_root = PLUGIN_ROOT.parents[2].resolve(strict=True)
    executable = Path(sys.executable).resolve(strict=True)
    environment = {
        PROJECT_ROOT_ENV: str(root),
        PROJECT_ID_ENV: project_id,
        CUSTOM_BINARY_ENV: custom_binary,
        "PYTHONPATH": str(agent_zero_root),
        "HOME": str(launcher_paths["home"]),
        "USERPROFILE": str(launcher_paths["home"]),
        "TMPDIR": str(launcher_paths["tmp"]),
        "TMP": str(launcher_paths["tmp"]),
        "TEMP": str(launcher_paths["tmp"]),
        "XDG_CACHE_HOME": str(launcher_paths["xdg_cache"]),
        "XDG_CONFIG_HOME": str(launcher_paths["xdg_config"]),
        "XDG_DATA_HOME": str(launcher_paths["xdg_data"]),
        "XDG_STATE_HOME": str(launcher_paths["xdg_state"]),
        "APPDATA": str(launcher_paths["appdata"]),
        "LOCALAPPDATA": str(launcher_paths["localappdata"]),
        "SEM_REPO": str(root),
        "SEM_CACHE_DIR": str(launcher_paths["sem_cache"]),
        "SEM_LOCAL": "1",
        "SEM_NO_TELEMETRY": "1",
        "SEM_NO_NETWORK": "1",
        "SEM_NO_UPDATE_CHECK": "1",
        "SEM_NO_AUTOWARM": "1",
        "SEM_NO_SIDECAR": "1",
        "DO_NOT_TRACK": "1",
    }
    return {
        "description": "Managed by the sem_review_loop plugin",
        "type": "stdio",
        "command": str(executable),
        "args": ["-s", "-m", LAUNCHER_MODULE],
        "env": environment,
        "disabled_tools": list(DISABLED_TOOLS),
        "init_timeout": 30,
        "tool_timeout": 30,
        "scope": "project",
    }


@dataclass(frozen=True)
class _PreviewBinding:
    project_id: str
    project_root_identity: str
    config_digest: str
    config_parent_identity: tuple[int, int] | None
    global_digest: str
    entry_hash: str
    created_at: float


@dataclass(frozen=True)
class _Readiness:
    project_id: str
    project_root_identity: str
    config_digest: str
    config_parent_identity: tuple[int, int] | None
    entry_hash: str
    tools: tuple[str, ...]


@dataclass
class _ProjectGateState:
    lock: Any
    references: int = 0


class MCPManager:
    def __init__(
        self,
        *,
        registry: Any,
        store: MCPConfigStore | None = None,
        refresh: Callable[[str], Any] = MCPConfig.refresh_project,
        inspect: Callable[[str], Any] = MCPConfig.get_project_instance,
        receipts: Any | None = None,
        global_config: Callable[[], str] = _global_config_text,
        load: Callable[[str], str] | None = None,
        save: Callable[[str, str], None] | None = None,
    ) -> None:
        if store is not None and (load is not None or save is not None):
            raise ValueError("Use either store or load/save, not both.")
        if (load is None) != (save is None):
            raise ValueError("load and save must be supplied together.")
        if store is None and load is not None and save is not None:
            store = _CallbackStore(load, save)
        self.registry = registry
        self.store = store or DirectProjectMCPStore()
        self.refresh = refresh
        self.inspect = inspect
        self.receipts = receipts or ReceiptStore()
        self.global_config = global_config
        self._locks: dict[str, _ProjectGateState] = {}
        self._locks_guard = threading.Lock()
        self._tokens: dict[str, _PreviewBinding] = {}
        self._tokens_guard = threading.Lock()
        self._readiness: dict[str, _Readiness] = {}
        self._readiness_guard = threading.Lock()

    def _get_receipt(self, project_name: str) -> MCPReceipt | None:
        receipt = self.receipts.get(project_name)
        if receipt is not None:
            _validate_receipt(receipt)
        return receipt

    def _put_receipt(self, receipt: MCPReceipt) -> None:
        _validate_receipt(receipt)
        self.receipts.put(receipt)

    @asynccontextmanager
    async def _project_gate(
        self,
        project_id: str,
    ) -> AsyncIterator[None]:
        with self._locks_guard:
            gate = self._locks.get(project_id)
            if gate is None:
                gate = _ProjectGateState(lock=threading.Lock())
                self._locks[project_id] = gate
            gate.references += 1

        acquired = False
        try:
            while not acquired:
                acquired = gate.lock.acquire(blocking=False)
                if not acquired:
                    await asyncio.sleep(PROJECT_GATE_POLL_SECONDS)
            yield
        finally:
            if acquired:
                gate.lock.release()
            with self._locks_guard:
                gate.references -= 1
                if (
                    gate.references == 0
                    and self._locks.get(project_id) is gate
                ):
                    self._locks.pop(project_id, None)

    def _set_registry(self, scope: ProjectScope, enabled: bool) -> None:
        setter = getattr(self.registry, "set_mcp_enabled", None)
        if callable(setter):
            setter(scope, enabled)

    def _invalidate(self, scope: ProjectScope) -> None:
        with self._readiness_guard:
            self._readiness.pop(scope.project_id, None)
        self._set_registry(scope, False)

    def _cached_readiness(self, project_id: str) -> _Readiness | None:
        with self._readiness_guard:
            return self._readiness.get(project_id)

    def _cache_readiness(self, readiness: _Readiness) -> None:
        with self._readiness_guard:
            self._readiness.pop(readiness.project_id, None)
            while len(self._readiness) >= MAX_READINESS_ENTRIES:
                oldest = next(iter(self._readiness))
                self._readiness.pop(oldest, None)
            self._readiness[readiness.project_id] = readiness

    @staticmethod
    def _entry(
        scope: ProjectScope,
        config: PluginConfig,
    ) -> dict[str, Any]:
        return managed_entry(scope, config)

    @staticmethod
    def _document(snapshot: ConfigSnapshot) -> dict[str, Any]:
        if not snapshot.exists:
            return {"mcpServers": {}}
        return parse_document(snapshot.raw)

    @staticmethod
    def _available_tools(config_instance: Any) -> set[str]:
        try:
            raw_tools = config_instance.get_tools()
        except Exception as exc:
            raise MCPVerificationError(
                "Unable to inspect focused sem MCP tools."
            ) from exc
        if not isinstance(raw_tools, list):
            raise MCPVerificationError(
                "Focused sem MCP tool inventory was unavailable."
            )
        return {
            str(name)
            for item in raw_tools
            if isinstance(item, Mapping)
            for name in item
        }

    @classmethod
    def _verify_tools(cls, config_instance: Any) -> tuple[str, ...]:
        available = cls._available_tools(config_instance)
        sem_enabled = {
            name for name in available if name.startswith(f"{SERVER_NAME}.")
        }
        missing = sorted(FOCUSED_TOOLS - sem_enabled)
        unexpected = sorted(sem_enabled - FOCUSED_TOOLS)
        if missing or unexpected:
            details: list[str] = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if unexpected:
                details.append("unexpected enabled " + ", ".join(unexpected))
            raise MCPVerificationError(
                "Focused sem MCP tools were not available: "
                + "; ".join(details)
            )

        detail = getattr(config_instance, "get_server_detail", None)
        if not callable(detail):
            raise MCPVerificationError(
                "Native sem MCP tool inventory was unavailable."
            )
        try:
            server_detail = detail(SERVER_NAME)
            if not isinstance(server_detail, Mapping):
                raise MCPVerificationError(
                    "Native sem MCP tool inventory was unavailable."
                )
            all_tools = server_detail.get("tools")
            if not isinstance(all_tools, list) or len(all_tools) != len(
                NATIVE_TOOLS
            ):
                raise MCPVerificationError(
                    "Native sem MCP tool inventory did not match the pinned "
                    "six-tool contract."
                )
            native_states: dict[str, bool] = {}
            for item in all_tools:
                if not isinstance(item, Mapping):
                    raise MCPVerificationError(
                        "Native sem MCP tool inventory was invalid."
                    )
                name = item.get("name")
                disabled = item.get("disabled", False)
                if (
                    not isinstance(name, str)
                    or name not in NATIVE_TOOLS
                    or name in native_states
                    or not isinstance(disabled, bool)
                ):
                    raise MCPVerificationError(
                        "Native sem MCP tool inventory was invalid."
                    )
                native_states[name] = disabled
        except MCPVerificationError:
            raise
        except Exception as exc:
            raise MCPVerificationError(
                "Unable to inspect native sem MCP tools."
            ) from exc
        if set(native_states) != NATIVE_TOOLS:
            raise MCPVerificationError(
                "Native sem MCP tool inventory did not match the pinned "
                "six-tool contract."
            )
        native_enabled = {
            name for name, disabled in native_states.items() if not disabled
        }
        if native_enabled != FOCUSED_TOOL_NAMES:
            raise MCPVerificationError(
                "Native sem MCP tool enablement did not match the focused "
                "three-tool contract."
            )
        return tuple(sorted(FOCUSED_TOOLS))

    def _global_binding(self) -> str:
        digest, names = _global_state(self.global_config)
        if SERVER_NAME in names:
            raise MCPConflictError(
                "A global MCP server already uses the sem_review_loop name."
            )
        return digest

    @staticmethod
    def _owned(
        receipt: MCPReceipt,
        scope: ProjectScope,
        root_identity: str,
    ) -> bool:
        return (
            receipt.project_name == scope.project_name
            and receipt.project_id == scope.project_id
            and receipt.project_root_identity == root_identity
        )

    def _recover_enable_receipt(
        self,
        *,
        scope: ProjectScope,
        root_identity: str,
        document: Mapping[str, Any],
        receipt: MCPReceipt | None,
    ) -> MCPReceipt | None:
        if (
            receipt is None
            or receipt.state not in {"enabling", "verifying"}
            or not self._owned(receipt, scope, root_identity)
        ):
            return receipt
        servers = document.get("mcpServers")
        if not isinstance(servers, Mapping):
            return receipt
        present = SERVER_NAME in servers
        current = servers.get(SERVER_NAME)
        current_hash = (
            canonical_hash(current)
            if isinstance(current, Mapping)
            else None
        )
        previous = receipt.previous_receipt
        if previous is not None:
            if current_hash == previous.entry_hash:
                self._put_receipt(previous)
                self._invalidate(scope)
                return previous
            if current_hash != receipt.entry_hash:
                return receipt
        elif not present:
            self.receipts.delete(scope.project_name)
            self._invalidate(scope)
            return None
        elif current_hash != receipt.entry_hash:
            return receipt

        if receipt.state == "enabling":
            receipt = replace(receipt, state="verifying")
            self._put_receipt(receipt)
            self._invalidate(scope)
        return receipt

    @staticmethod
    def _final_receipt(receipt: MCPReceipt) -> MCPReceipt:
        return replace(
            receipt,
            state="enabled",
            previous_receipt=None,
        )

    def _prune_tokens(self, now: float) -> None:
        expired = [
            token
            for token, binding in self._tokens.items()
            if now - binding.created_at > PREVIEW_TOKEN_TTL_SECONDS
        ]
        for token in expired:
            self._tokens.pop(token, None)
        if len(self._tokens) >= MAX_PREVIEW_TOKENS:
            oldest = sorted(
                self._tokens,
                key=lambda token: self._tokens[token].created_at,
            )
            for token in oldest[: len(self._tokens) - MAX_PREVIEW_TOKENS + 1]:
                self._tokens.pop(token, None)

    def preview(
        self,
        scope: ProjectScope,
        config: PluginConfig,
    ) -> dict[str, object]:
        snapshot = self.store.read(scope)
        document = self._document(snapshot)
        global_digest = self._global_binding()
        entry = self._entry(scope, config)
        root_identity = project_root_identity(scope.project_root)
        receipt = self._get_receipt(scope.project_name)
        receipt = self._recover_enable_receipt(
            scope=scope,
            root_identity=root_identity,
            document=document,
            receipt=receipt,
        )
        if receipt is not None and receipt.state != "enabled":
            raise MCPConflictError(
                "The managed sem MCP transaction requires readiness recovery "
                "before another enable."
            )
        servers = document["mcpServers"]
        present = SERVER_NAME in servers
        current = servers.get(SERVER_NAME)
        if present:
            if (
                not isinstance(current, dict)
                or receipt is None
                or not self._owned(receipt, scope, root_identity)
                or canonical_hash(current) != receipt.entry_hash
            ):
                raise MCPConflictError(
                    "An unrelated MCP server already uses the "
                    "sem_review_loop name."
                )

        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._tokens_guard:
            self._prune_tokens(now)
            self._tokens[token] = _PreviewBinding(
                project_id=scope.project_id,
                project_root_identity=root_identity,
                config_digest=snapshot.digest,
                config_parent_identity=snapshot.parent_identity,
                global_digest=global_digest,
                entry_hash=canonical_hash(entry),
                created_at=now,
            )
        return {
            "entry": entry,
            "preview_token": token,
            "confirmation_required": True,
            "local_paths": {
                "project": str(scope.project_root),
                "cache": str(_launcher_paths(scope)["sem_cache"]),
                "plugin_data": str(DATA_ROOT),
            },
            "writes": [
                f"{scope.project_name}/.a0proj/mcp_servers.json"
            ],
        }

    def _consume_preview(
        self,
        *,
        scope: ProjectScope,
        config: PluginConfig,
        snapshot: ConfigSnapshot,
        confirmed: object,
        preview_token: object,
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise MCPManagerError(
                "Enable MCP requires an explicit reviewed confirmation."
            )
        token = str(preview_token or "")
        if not token or len(token) > 256:
            raise MCPStalePreviewError(
                "Enable MCP requires a valid preview token."
            )
        with self._tokens_guard:
            binding = self._tokens.pop(token, None)
        if binding is None:
            raise MCPStalePreviewError(
                "The MCP preview expired or was already used."
            )
        now = time.monotonic()
        entry = self._entry(scope, config)
        expected = _PreviewBinding(
            project_id=scope.project_id,
            project_root_identity=project_root_identity(scope.project_root),
            config_digest=snapshot.digest,
            config_parent_identity=snapshot.parent_identity,
            global_digest=self._global_binding(),
            entry_hash=canonical_hash(entry),
            created_at=binding.created_at,
        )
        if (
            now - binding.created_at > PREVIEW_TOKEN_TTL_SECONDS
            or binding != expected
        ):
            raise MCPStalePreviewError(
                "Project MCP state changed; preview it again."
            )
        return entry

    def status(self, scope: ProjectScope) -> dict[str, object]:
        snapshot = self.store.read(scope)
        document = self._document(snapshot)
        try:
            _global_digest, global_names = _global_state(self.global_config)
        except Exception:
            self._invalidate(scope)
            raise
        receipt = self._get_receipt(scope.project_name)
        servers = document["mcpServers"]
        present = SERVER_NAME in servers
        current = servers.get(SERVER_NAME)
        configured = isinstance(current, dict)
        root_identity = project_root_identity(scope.project_root)
        receipt = self._recover_enable_receipt(
            scope=scope,
            root_identity=root_identity,
            document=document,
            receipt=receipt,
        )
        global_conflict = SERVER_NAME in global_names
        ownership = bool(
            receipt and self._owned(receipt, scope, root_identity)
        )
        exact = bool(
            receipt
            and ownership
            and configured
            and canonical_hash(current) == receipt.entry_hash
        )
        drifted = bool(receipt and not exact)
        conflict = bool(global_conflict or (present and receipt is None))
        cached = self._cached_readiness(scope.project_id)
        if global_conflict:
            self._invalidate(scope)
            cached = None
        ready = bool(
            not global_conflict
            and exact
            and receipt
            and receipt.state == "enabled"
            and cached
            and cached.project_id == scope.project_id
            and cached.project_root_identity == root_identity
            and cached.config_digest == snapshot.digest
            and cached.config_parent_identity == snapshot.parent_identity
            and cached.entry_hash == receipt.entry_hash
        )
        error = ""
        if global_conflict:
            error = "A global MCP server uses the sem_review_loop name."
        elif receipt and not ownership:
            error = "Managed MCP receipt belongs to a different project identity."
        elif drifted:
            error = "Managed sem MCP entry changed or is missing."
        elif present and receipt is None:
            error = "An unrelated MCP server uses the sem_review_loop name."
        elif receipt and receipt.state != "enabled":
            error = "Managed sem MCP transaction requires recovery."
        return {
            "configured": configured,
            "armed": receipt is not None,
            "enabled": ready,
            "drifted": drifted,
            "conflict": conflict,
            "tools": list(cached.tools) if ready and cached else [],
            "error": error,
        }

    @staticmethod
    async def _run_provider_coordinated(
        provider: Callable[[str], Any],
        project_name: str,
    ) -> Any:
        task = asyncio.create_task(
            asyncio.to_thread(provider, project_name)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if task.done() and not task.cancelled():
                try:
                    task.result()
                except BaseException:
                    pass
            raise cancellation

    async def _inspect_and_cache(
        self,
        scope: ProjectScope,
        receipt: MCPReceipt,
        snapshot: ConfigSnapshot,
        *,
        refresh: bool,
    ) -> tuple[str, ...]:
        provider = self.refresh if refresh else self.inspect
        config_instance = await self._run_provider_coordinated(
            provider,
            scope.project_name,
        )
        tools = self._verify_tools(config_instance)
        root_identity = project_root_identity(scope.project_root)
        self._cache_readiness(
            _Readiness(
                project_id=scope.project_id,
                project_root_identity=root_identity,
                config_digest=snapshot.digest,
                config_parent_identity=snapshot.parent_identity,
                entry_hash=receipt.entry_hash,
                tools=tools,
            )
        )
        self._set_registry(scope, True)
        return tools

    async def readiness(
        self,
        scope: ProjectScope,
    ) -> dict[str, object]:
        async with self._project_gate(scope.project_id):
            snapshot = self.store.read(scope)
            document = self._document(snapshot)
            receipt = self._get_receipt(scope.project_name)
            if receipt is None:
                self._invalidate(scope)
                return self.status(scope)
            servers = document["mcpServers"]
            present = SERVER_NAME in servers
            current = servers.get(SERVER_NAME)
            root_identity = project_root_identity(scope.project_root)
            if not self._owned(receipt, scope, root_identity):
                self._invalidate(scope)
                return self.status(scope)
            receipt = self._recover_enable_receipt(
                scope=scope,
                root_identity=root_identity,
                document=document,
                receipt=receipt,
            )
            if receipt is None:
                self._invalidate(scope)
                return self.status(scope)

            if receipt.state == "disabling":
                if not present:
                    self.receipts.delete(scope.project_name)
                    self._invalidate(scope)
                    return self.status(scope)
                if canonical_hash(current) == receipt.entry_hash:
                    receipt = self._final_receipt(receipt)
                    self._put_receipt(receipt)
                else:
                    self._invalidate(scope)
                    return self.status(scope)

            status = self.status(scope)
            if status["drifted"] or status["conflict"]:
                self._invalidate(scope)
                return status

            cached = self._cached_readiness(scope.project_id)
            if (
                cached
                and cached.project_root_identity == root_identity
                and cached.config_digest == snapshot.digest
                and cached.entry_hash == receipt.entry_hash
            ):
                if receipt.state != "enabled":
                    receipt = self._final_receipt(receipt)
                    self._put_receipt(receipt)
                self._set_registry(scope, True)
                final_status = self.status(scope)
                if not final_status["enabled"]:
                    self._invalidate(scope)
                    return {
                        **final_status,
                        "enabled": False,
                        "tools": [],
                    }
                return final_status

            try:
                tools = await self._inspect_and_cache(
                    scope,
                    receipt,
                    snapshot,
                    refresh=False,
                )
            except Exception as exc:
                self._invalidate(scope)
                return {
                    **self.status(scope),
                    "enabled": False,
                    "error": _bounded_error(exc),
                }
            if receipt.state != "enabled":
                self._put_receipt(self._final_receipt(receipt))
            final_status = self.status(scope)
            if not final_status["enabled"]:
                self._invalidate(scope)
                return {
                    **final_status,
                    "enabled": False,
                    "tools": [],
                }
            return final_status

    @staticmethod
    async def _shielded(coro: Any) -> Any:
        task = asyncio.create_task(coro)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Finish restoring durable state before propagating cancellation.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            return task.result()

    async def _refresh_only(self, project_name: str) -> None:
        await self._run_provider_coordinated(
            self.refresh,
            project_name,
        )

    async def _rollback_enable(
        self,
        *,
        scope: ProjectScope,
        original: ConfigSnapshot,
        written: ConfigSnapshot,
        entry_hash: str,
        previous_receipt: MCPReceipt | None,
    ) -> None:
        current = self.store.read(scope)
        if not (
            current.parent_identity
            == written.parent_identity
            == original.parent_identity
        ):
            raise MCPRollbackError(
                "Project metadata directory changed during enable rollback."
            )
        if (
            current.exists == written.exists
            and current.digest == written.digest
            and current.raw == written.raw
        ):
            restored = self.store.compare_and_swap(
                scope,
                current,
                original.raw if original.exists else None,
            )
        else:
            document = self._document(current)
            entry = document["mcpServers"].get(SERVER_NAME)
            if entry is None:
                if previous_receipt is not None:
                    raise MCPRollbackError(
                        "Managed MCP entry was removed during re-enable "
                        "rollback; the external edit was preserved."
                    )
                restored = current
            elif canonical_hash(entry) == entry_hash:
                if previous_receipt is None:
                    del document["mcpServers"][SERVER_NAME]
                else:
                    original_document = self._document(original)
                    previous_entry = original_document["mcpServers"].get(
                        SERVER_NAME
                    )
                    if (
                        not isinstance(previous_entry, dict)
                        or canonical_hash(previous_entry)
                        != previous_receipt.entry_hash
                    ):
                        raise MCPRollbackError(
                            "The prior managed MCP entry was unavailable "
                            "during enable rollback."
                        )
                    document["mcpServers"][SERVER_NAME] = previous_entry
                restored = self.store.compare_and_swap(
                    scope,
                    current,
                    serialize_document(document),
                )
            else:
                raise MCPRollbackError(
                    "Managed MCP entry drifted during enable rollback."
                )
        if previous_receipt is None:
            self.receipts.delete(scope.project_name)
        else:
            self._put_receipt(previous_receipt)
        self._invalidate(scope)
        await self._refresh_only(scope.project_name)
        del restored

    async def enable(
        self,
        scope: ProjectScope,
        config: PluginConfig,
        *,
        confirmed: object = False,
        preview_token: object = "",
    ) -> dict[str, object]:
        async with self._project_gate(scope.project_id):
            original = self.store.read(scope)
            document = self._document(original)
            entry = self._consume_preview(
                scope=scope,
                config=config,
                snapshot=original,
                confirmed=confirmed,
                preview_token=preview_token,
            )
            root_identity = project_root_identity(scope.project_root)
            servers = document["mcpServers"]
            present = SERVER_NAME in servers
            current = servers.get(SERVER_NAME)
            receipt = self._get_receipt(scope.project_name)
            if receipt is not None and receipt.state != "enabled":
                raise MCPConflictError(
                    "The managed sem MCP transaction requires readiness "
                    "recovery before another enable."
                )
            if present and (
                not isinstance(current, dict)
                or receipt is None
                or not self._owned(receipt, scope, root_identity)
                or canonical_hash(current) != receipt.entry_hash
            ):
                raise MCPConflictError(
                    "An unrelated MCP server already uses the "
                    "sem_review_loop name."
                )

            entry_hash = canonical_hash(entry)
            document["mcpServers"][SERVER_NAME] = entry
            replacement = serialize_document(document)
            pending = MCPReceipt(
                project_name=scope.project_name,
                project_id=scope.project_id,
                entry_hash=entry_hash,
                project_root_identity=root_identity,
                state="enabling",
                config_before_hash=original.digest,
                config_after_hash=_snapshot_digest(True, replacement),
                previous_receipt=receipt,
            )
            self._put_receipt(pending)
            written: ConfigSnapshot | None = None
            try:
                written = self.store.compare_and_swap(
                    scope,
                    original,
                    replacement,
                )
                self._put_receipt(replace(pending, state="verifying"))
                tools = await self._inspect_and_cache(
                    scope,
                    pending,
                    written,
                    refresh=True,
                )
                current_after_refresh = self.store.read(scope)
                if (
                    current_after_refresh.parent_identity
                    != written.parent_identity
                    or current_after_refresh.exists != written.exists
                    or current_after_refresh.digest != written.digest
                    or current_after_refresh.raw != written.raw
                ):
                    raise MCPConcurrentModificationError(
                        "Project MCP configuration changed during refresh."
                    )
                self._global_binding()
                self._put_receipt(self._final_receipt(pending))
            except BaseException as original_error:
                try:
                    if written is not None:
                        await self._shielded(
                            self._rollback_enable(
                                scope=scope,
                                original=original,
                                written=written,
                                entry_hash=entry_hash,
                                previous_receipt=receipt,
                            )
                        )
                    else:
                        if receipt is None:
                            self.receipts.delete(scope.project_name)
                        else:
                            self._put_receipt(receipt)
                        self._invalidate(scope)
                except BaseException as rollback_error:
                    raise MCPRollbackError(
                        "Enable MCP failed and rollback could not complete: "
                        + _bounded_error(rollback_error)
                    ) from original_error
                raise
            return {
                "configured": True,
                "armed": True,
                "enabled": True,
                "drifted": False,
                "conflict": False,
                "tools": list(tools),
            }

    async def _rollback_disable(
        self,
        *,
        scope: ProjectScope,
        original: ConfigSnapshot,
        written: ConfigSnapshot,
        receipt: MCPReceipt,
        original_entry: dict[str, Any],
    ) -> None:
        current = self.store.read(scope)
        if not (
            current.parent_identity
            == written.parent_identity
            == original.parent_identity
        ):
            raise MCPRollbackError(
                "Project metadata directory changed during disable rollback."
            )
        if (
            current.exists == written.exists
            and current.digest == written.digest
            and current.raw == written.raw
        ):
            self.store.compare_and_swap(
                scope,
                current,
                original.raw if original.exists else None,
            )
        else:
            document = self._document(current)
            entry = document["mcpServers"].get(SERVER_NAME)
            if entry is not None:
                raise MCPRollbackError(
                    "The sem MCP server name was reused during disable rollback."
                )
            document["mcpServers"][SERVER_NAME] = original_entry
            self.store.compare_and_swap(
                scope,
                current,
                serialize_document(document),
            )
        self._put_receipt(self._final_receipt(receipt))
        await self._refresh_only(scope.project_name)

    async def disable(
        self,
        scope: ProjectScope,
    ) -> dict[str, object]:
        async with self._project_gate(scope.project_id):
            original = self.store.read(scope)
            document = self._document(original)
            servers = document["mcpServers"]
            present = SERVER_NAME in servers
            current = servers.get(SERVER_NAME)
            receipt = self._get_receipt(scope.project_name)
            if receipt is None:
                self._invalidate(scope)
                return {
                    "configured": present,
                    "armed": False,
                    "enabled": False,
                    "drifted": False,
                    "conflict": present,
                }
            root_identity = project_root_identity(scope.project_root)
            if not self._owned(receipt, scope, root_identity):
                self._invalidate(scope)
                return {
                    "configured": present,
                    "armed": True,
                    "enabled": False,
                    "drifted": True,
                    "conflict": False,
                }
            receipt = self._recover_enable_receipt(
                scope=scope,
                root_identity=root_identity,
                document=document,
                receipt=receipt,
            )
            if receipt is None:
                self._invalidate(scope)
                return {
                    "configured": present,
                    "armed": False,
                    "enabled": False,
                    "drifted": False,
                    "conflict": present,
                }
            if receipt.state == "disabling" and not present:
                self.receipts.delete(scope.project_name)
                self._invalidate(scope)
                return {
                    "configured": False,
                    "armed": False,
                    "enabled": False,
                    "drifted": False,
                    "conflict": False,
                }
            if not isinstance(current, dict):
                self._invalidate(scope)
                return {
                    "configured": False,
                    "armed": True,
                    "enabled": False,
                    "drifted": True,
                    "conflict": False,
                }
            if canonical_hash(current) != receipt.entry_hash:
                self._invalidate(scope)
                return {
                    "configured": True,
                    "armed": True,
                    "enabled": False,
                    "drifted": True,
                    "conflict": False,
                }
            original_entry = dict(current)
            receipt = self._final_receipt(receipt)
            del document["mcpServers"][SERVER_NAME]
            replacement = serialize_document(document)
            pending = replace(
                receipt,
                state="disabling",
                config_before_hash=original.digest,
                config_after_hash=_snapshot_digest(True, replacement),
                previous_receipt=None,
            )
            self._put_receipt(pending)
            written: ConfigSnapshot | None = None
            try:
                written = self.store.compare_and_swap(
                    scope,
                    original,
                    replacement,
                )
                await self._refresh_only(scope.project_name)
            except BaseException as original_error:
                try:
                    if written is not None:
                        await self._shielded(
                            self._rollback_disable(
                                scope=scope,
                                original=original,
                                written=written,
                                receipt=receipt,
                                original_entry=original_entry,
                            )
                        )
                    else:
                        self._put_receipt(receipt)
                except BaseException as rollback_error:
                    raise MCPRollbackError(
                        "Disable MCP failed and rollback could not complete: "
                        + _bounded_error(rollback_error)
                    ) from original_error
                raise
            self.receipts.delete(scope.project_name)
            self._invalidate(scope)
            return {
                "configured": False,
                "armed": False,
                "enabled": False,
                "drifted": False,
                "conflict": False,
            }

    @staticmethod
    def _scope_for_receipt(receipt: MCPReceipt) -> ProjectScope | None:
        root = Path(projects.get_project_folder(receipt.project_name))
        try:
            metadata = root.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise MCPManagerError(
                "Unable to inspect a managed MCP project during cleanup."
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return None
        try:
            return make_scope("", receipt.project_name, root, ".")
        except ProjectScopeError as exc:
            raise MCPManagerError(
                "Unable to resolve a managed MCP project during cleanup."
            ) from exc

    async def disable_all_managed(self) -> list[str]:
        drifted: list[str] = []
        for receipt in self.receipts.all():
            _validate_receipt(receipt)
            scope = self._scope_for_receipt(receipt)
            if scope is None:
                drifted.append(receipt.project_name)
                continue
            if (
                scope.project_id != receipt.project_id
                or project_root_identity(scope.project_root)
                != receipt.project_root_identity
            ):
                drifted.append(receipt.project_name)
                continue
            result = await self.disable(scope)
            if result["drifted"]:
                drifted.append(receipt.project_name)
        return sorted(set(drifted))
