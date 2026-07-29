from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import BinaryIO


FRAMEWORK_ROOT = Path(__file__).resolve().parents[3]
MAINTENANCE_MODULE = "usr.plugins.sem_review_loop.helpers.maintenance"
MAINTENANCE_TIMEOUT_SECONDS = 115
PROCESS_TERMINATION_GRACE_SECONDS = 0.5
MAX_CAPTURE_BYTES = 16 * 1024
MAX_ERROR_CHARS = 500
MAX_JSON_BYTES = 4 * 1024


class MaintenanceProcessError(RuntimeError):
    pass


def _maintenance_command() -> list[str]:
    return [sys.executable, "-m", MAINTENANCE_MODULE]


def _drain_bounded(
    stream: BinaryIO,
    output: bytearray,
    state: dict[str, bool],
) -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            remaining = max(0, MAX_CAPTURE_BYTES - len(output))
            output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                state["truncated"] = True
    except (OSError, ValueError):
        state["read_error"] = True
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _signal_process_tree(
    process: subprocess.Popen[bytes],
    *,
    force: bool,
) -> None:
    if os.name == "nt":
        try:
            killer = subprocess.Popen(
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
            )
            try:
                killer.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                killer.kill()
                killer.wait()
        except OSError:
            if process.poll() is None:
                process.kill()
        return

    try:
        os.killpg(
            process.pid,
            signal.SIGKILL if force else signal.SIGTERM,
        )
    except ProcessLookupError:
        pass
    except OSError:
        if process.poll() is None:
            process.kill() if force else process.terminate()


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    _signal_process_tree(process, force=False)
    try:
        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    _signal_process_tree(process, force=True)
    if process.poll() is None:
        try:
            process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    else:
        process.wait()


def _run_maintenance(
    args: list[str],
    cwd: Path,
) -> tuple[int, bytes, bytes, bool]:
    options: dict[str, object] = {
        "cwd": cwd,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
    }
    if os.name == "nt":
        options["creationflags"] = getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0,
        )
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen(args, **options)
    except OSError as exc:
        raise MaintenanceProcessError(
            f"Unable to start maintenance: {exc}"
        ) from exc
    if process.stdout is None or process.stderr is None:
        _terminate_and_reap(process)
        raise MaintenanceProcessError(
            "Unable to capture maintenance output."
        )

    stdout = bytearray()
    stderr = bytearray()
    stdout_state = {"truncated": False, "read_error": False}
    stderr_state = {"truncated": False, "read_error": False}
    readers = [
        threading.Thread(
            target=_drain_bounded,
            args=(process.stdout, stdout, stdout_state),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_bounded,
            args=(process.stderr, stderr, stderr_state),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        process.wait(timeout=MAINTENANCE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_and_reap(process)

    for reader in readers:
        reader.join(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
    if any(reader.is_alive() for reader in readers):
        _signal_process_tree(process, force=True)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
        for reader in readers:
            reader.join(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
    if process.poll() is None:
        _terminate_and_reap(process)
    else:
        process.wait()

    if timed_out:
        raise MaintenanceProcessError("SEM maintenance timed out.")
    if any(reader.is_alive() for reader in readers):
        raise MaintenanceProcessError(
            "Unable to reap maintenance output readers."
        )
    if stdout_state["read_error"] or stderr_state["read_error"]:
        raise MaintenanceProcessError(
            "Unable to read maintenance output safely."
        )
    return (
        process.returncode,
        bytes(stdout),
        bytes(stderr),
        stdout_state["truncated"] or stderr_state["truncated"],
    )


def _bounded_error(exc: Exception) -> str:
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__
    message = message.encode("utf-8", errors="replace").decode("utf-8")
    message = " ".join(message.replace("\x00", "").split())
    return message[:MAX_ERROR_CHARS] or exc.__class__.__name__


def _decode_result(stdout: bytes, truncated: bool) -> dict[str, object]:
    if truncated:
        raise MaintenanceProcessError(
            "Maintenance output exceeded its byte limit."
        )
    try:
        value = stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise MaintenanceProcessError(
            "Maintenance output was not valid UTF-8."
        ) from exc
    lines = value.splitlines()
    if len(lines) != 1:
        raise MaintenanceProcessError(
            "Maintenance did not return exactly one JSON line."
        )
    try:
        payload = json.loads(lines[0])
    except (json.JSONDecodeError, ValueError) as exc:
        raise MaintenanceProcessError(
            "Maintenance returned invalid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise MaintenanceProcessError(
            "Maintenance returned an invalid JSON result."
        )
    return payload


def _emit(payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        encoded = json.dumps(
            {
                "ok": False,
                "error": "Maintenance result exceeded its byte limit.",
            },
            separators=(",", ":"),
        )
    print(encoded)


def main() -> int:
    try:
        returncode, stdout, _stderr, truncated = _run_maintenance(
            _maintenance_command(),
            FRAMEWORK_ROOT,
        )
        payload = _decode_result(stdout, truncated)
        status = 0 if returncode == 0 else 1
    except Exception as exc:
        payload = {"ok": False, "error": _bounded_error(exc)}
        status = 1
    _emit(payload)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
