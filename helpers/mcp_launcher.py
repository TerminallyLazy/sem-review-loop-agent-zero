from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from types import FrameType
from typing import Protocol

from usr.plugins.sem_review_loop.helpers.installer import lease_binary
from usr.plugins.sem_review_loop.helpers.paths import (
    CACHE_ROOT,
    DATA_ROOT,
    ensure_data_dirs,
)


PROJECT_ROOT_ENV = "SEM_REVIEW_LOOP_PROJECT_ROOT"
PROJECT_ID_ENV = "SEM_REVIEW_LOOP_PROJECT_ID"
CUSTOM_BINARY_ENV = "SEM_REVIEW_LOOP_CUSTOM_BINARY"
TERMINATION_GRACE_SECONDS = 3.0
POLL_INTERVAL_SECONDS = 0.05
PROJECT_ID_PATTERN = re.compile(r"\A[A-Za-z0-9._-]{1,128}\Z")


class LauncherError(RuntimeError):
    """A bounded, user-actionable launcher failure."""


class ProcessLike(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


def _bounded(value: object, limit: int = 300) -> str:
    return " ".join(str(value).split())[:limit]


def _validated_project_id(value: object) -> str:
    project_id = str(value or "").strip()
    if PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise LauncherError("The MCP launcher received an invalid project identity.")
    return project_id


def _resolved_project_root(value: object) -> Path:
    raw = str(value or "")
    if not raw or len(raw) > 4096:
        raise LauncherError("The MCP launcher requires a bounded project path.")
    try:
        root = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LauncherError("The MCP project path is unavailable.") from exc
    if not root.is_dir():
        raise LauncherError("The MCP project path is not a directory.")
    return root


def _plugin_owned(path: Path) -> Path:
    try:
        data_root = DATA_ROOT.resolve(strict=True)
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise LauncherError("The plugin data path is unavailable.") from exc
    if resolved == data_root or data_root not in resolved.parents:
        raise LauncherError("The MCP runtime path escaped plugin-owned data.")
    return resolved


def _private_directory(path: Path) -> Path:
    resolved = _plugin_owned(path)
    current = DATA_ROOT.resolve(strict=True)
    for part in resolved.relative_to(current).parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            metadata = current.lstat()
        except OSError as exc:
            raise LauncherError("The MCP runtime directory is unavailable.") from exc
        if current.is_symlink() or not current.is_dir():
            raise LauncherError("The MCP runtime path contains an unsafe component.")
        try:
            current.chmod(0o700)
        except OSError:
            # Windows and some mounted filesystems do not implement POSIX modes.
            if os.name != "nt":
                raise
    return resolved


def runtime_directories(project_id: str) -> dict[str, Path]:
    project_id = _validated_project_id(project_id)
    ensure_data_dirs()
    runtime_root = _private_directory(DATA_ROOT / "mcp-runtime" / project_id)
    directories = {
        "home": runtime_root / "home",
        "tmp": runtime_root / "tmp",
        "xdg_cache": runtime_root / "xdg-cache",
        "xdg_config": runtime_root / "xdg-config",
        "xdg_data": runtime_root / "xdg-data",
        "xdg_state": runtime_root / "xdg-state",
        "appdata": runtime_root / "appdata",
        "localappdata": runtime_root / "localappdata",
        "sem_cache": CACHE_ROOT / project_id / "mcp",
    }
    return {name: _private_directory(path) for name, path in directories.items()}


def child_environment(
    *,
    project_root: Path,
    project_id: str,
) -> dict[str, str]:
    """Build a closed environment; inherited secrets and proxies are excluded."""

    root = _resolved_project_root(project_root)
    directories = runtime_directories(project_id)
    safe_path_parts = [
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    environment = {
        "PATH": os.pathsep.join(safe_path_parts),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(directories["home"]),
        "USERPROFILE": str(directories["home"]),
        "TMPDIR": str(directories["tmp"]),
        "TMP": str(directories["tmp"]),
        "TEMP": str(directories["tmp"]),
        "XDG_CACHE_HOME": str(directories["xdg_cache"]),
        "XDG_CONFIG_HOME": str(directories["xdg_config"]),
        "XDG_DATA_HOME": str(directories["xdg_data"]),
        "XDG_STATE_HOME": str(directories["xdg_state"]),
        "APPDATA": str(directories["appdata"]),
        "LOCALAPPDATA": str(directories["localappdata"]),
        "SEM_REPO": str(root),
        "SEM_CACHE_DIR": str(directories["sem_cache"]),
        "SEM_LOCAL": "1",
        "SEM_NO_TELEMETRY": "1",
        "SEM_NO_NETWORK": "1",
        "SEM_NO_UPDATE_CHECK": "1",
        "SEM_NO_AUTOWARM": "1",
        "SEM_NO_SIDECAR": "1",
        "DO_NOT_TRACK": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "NO_COLOR": "1",
        "CLICOLOR": "0",
    }
    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        if system_root.is_absolute():
            environment["SystemRoot"] = str(system_root)
            environment["COMSPEC"] = str(system_root / "System32" / "cmd.exe")
    return environment


def _signal_process_tree(process: ProcessLike, signum: int) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            sender = getattr(process, "send_signal", None)
            ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
            if not callable(sender) or ctrl_break is None:
                raise OSError("Windows process-group signaling is unavailable.")
            sender(ctrl_break)
        else:
            os.killpg(process.pid, signum)
    except (OSError, ProcessLookupError):
        pass


def _force_process_tree(process: ProcessLike) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                [
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
                timeout=TERMINATION_GRACE_SECONDS,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        pass
    try:
        if process.poll() is None:
            process.kill()
    except (OSError, ProcessLookupError):
        pass


def _wait_for_exit(
    process: ProcessLike,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    forwarded_at: float | None = None
    received_signal: int | None = None
    previous_handlers: dict[int, object] = {}

    def forward(signum: int, _frame: FrameType | None) -> None:
        nonlocal forwarded_at, received_signal
        received_signal = signum
        if forwarded_at is None:
            forwarded_at = monotonic()
        _signal_process_tree(process, signum)

    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        signals.append(signal.SIGHUP)
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)

    for signum in signals:
        try:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, forward)
        except (OSError, ValueError):
            continue

    try:
        while True:
            result = process.poll()
            if result is not None:
                return int(result)
            if (
                received_signal is not None
                and forwarded_at is not None
                and monotonic() - forwarded_at >= TERMINATION_GRACE_SECONDS
            ):
                _force_process_tree(process)
            sleep(POLL_INTERVAL_SECONDS)
    finally:
        for signum, previous in previous_handlers.items():
            try:
                signal.signal(signum, previous)
            except (OSError, ValueError):
                pass


def _terminate_and_reap(
    process: ProcessLike,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Keep the binary lease until the child has definitely terminated."""

    if process.poll() is not None:
        return
    _signal_process_tree(process, signal.SIGTERM)
    deadline = monotonic() + TERMINATION_GRACE_SECONDS
    while process.poll() is None and monotonic() < deadline:
        sleep(POLL_INTERVAL_SECONDS)
    if process.poll() is None:
        _force_process_tree(process)
    while process.poll() is None:
        sleep(POLL_INTERVAL_SECONDS)


def launch_sem_mcp(
    *,
    environ: Mapping[str, str] | None = None,
    lease: Callable[[str], AbstractContextManager[Path]] = lease_binary,
    popen: Callable[..., ProcessLike] = subprocess.Popen,
) -> int:
    source = dict(environ if environ is not None else os.environ)
    project_id = _validated_project_id(source.get(PROJECT_ID_ENV))
    project_root = _resolved_project_root(source.get(PROJECT_ROOT_ENV))
    custom_binary = str(source.get(CUSTOM_BINARY_ENV) or "")
    if len(custom_binary) > 4096:
        raise LauncherError("The custom sem binary path is too long.")

    environment = child_environment(
        project_root=project_root,
        project_id=project_id,
    )
    creation: dict[str, object] = {}
    if os.name == "nt":
        creation["creationflags"] = getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0,
        )
    else:
        creation["start_new_session"] = True

    with lease(custom_binary) as binary:
        process = popen(
            [str(binary), "mcp"],
            cwd=str(project_root),
            env=environment,
            stdin=None,
            stdout=None,
            stderr=None,
            close_fds=os.name != "nt",
            **creation,
        )
        try:
            return _wait_for_exit(process)
        finally:
            _terminate_and_reap(process)


def main() -> int:
    try:
        return launch_sem_mcp()
    except Exception as exc:
        print(f"sem_review_loop launcher error: {_bounded(exc)}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
