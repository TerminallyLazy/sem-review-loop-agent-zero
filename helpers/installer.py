from __future__ import annotations

import gzip
import hashlib
import hmac
import io
import json
import os
import platform
import re
import signal
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator

from usr.plugins.sem_review_loop.helpers.paths import (
    DATA_ROOT,
    RELEASE_MANIFEST_PATH,
    ensure_data_dirs,
)


DOWNLOAD_TIMEOUT_SECONDS = 30
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_BINARY_BYTES = MAX_ARCHIVE_BYTES
MAX_ARCHIVE_MEMBERS = 256
MAX_ARCHIVE_METADATA_BYTES = 1024 * 1024
MAX_EXPANDED_ARCHIVE_BYTES = MAX_ARCHIVE_BYTES
MAX_REDIRECT_HOPS = 5
MAX_VERSION_OUTPUT_BYTES = 16 * 1024
PROCESS_TERMINATION_GRACE_SECONDS = 0.5
VERSION_TIMEOUT_SECONDS = 10
SEM_VERSION = "0.21.0"
SEM_SOURCE_COMMIT = "a4e8b53521034536dbe26067f948085870d59658"
OFFICIAL_BASE_URL = (
    "https://github.com/Ataraxy-Labs/sem/releases/download/v0.21.0"
)
PINNED_PLATFORM_RELEASES = {
    "darwin-arm64": (
        "sem-darwin-arm64.tar.gz",
        "7e17372ffdf6477a2b711e173fb783ecd82a4559ee3747985e2397c128d1e6f7",
        "818c7af64e71b71c37dee84ad5096b05ea09c9b0401828f818c685d3da13b81d",
        "tar.gz",
        "sem",
    ),
    "darwin-x86_64": (
        "sem-darwin-x86_64.tar.gz",
        "b179b996cf6060d74873fc117b2dd94104af9835ff48097c6e9c2923a374dee1",
        "25dfe641dd348f1fe153fe0177ad97bd4334e83d148ac18b2bb9e5e697bae1f0",
        "tar.gz",
        "sem",
    ),
    "linux-arm64": (
        "sem-linux-arm64.tar.gz",
        "0480663055d3d7c386dabee6e57766205984ac151bd691540bde0b3be64af27b",
        "c69626bb9e99fd5de5c7f9de29567e675155941e36559fe205a750946d350680",
        "tar.gz",
        "sem",
    ),
    "linux-x86_64": (
        "sem-linux-x86_64.tar.gz",
        "4a06f019552add37b4b0693309daaf529eae7f291217d20c291294c790b16b4b",
        "23206983bacf23f613452a1fdd97df8e6dfedfd3dce9fff1c03f02672fac2ef7",
        "tar.gz",
        "sem",
    ),
    "windows-x86_64": (
        "sem-windows-x86_64.zip",
        "8ead28b095b829dba340e77d8d184394918223bd62a3fcd7cd634df50246fc9b",
        "4a5c4a666d022aed6574b00ea384dd433625817ecfaa20e2052c375f19bdf2ed",
        "zip",
        "sem.exe",
    ),
}
SUPPORTED_PLATFORM_KEYS = frozenset(PINNED_PLATFORM_RELEASES)
RELEASE_ASSET_HOSTS = frozenset({"release-assets.githubusercontent.com"})
VERSION_PATTERN = re.compile(r"\Asem 0\.21\.0(?:\r?\n)?\Z")
SEM_IDENTITY_PATTERN = re.compile(
    r"\Asem ([0-9]+\.[0-9]+\.[0-9]+)(?:\r?\n)?\Z"
)
SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")


class InstallError(RuntimeError):
    """A safe, user-actionable installation failure."""


class UnsupportedPlatformError(InstallError):
    """The managed installer does not publish a binary for this platform."""


def platform_key() -> str:
    system = platform.system().strip().lower()
    machine = platform.machine().strip().lower()
    system_name = {
        "darwin": "darwin",
        "linux": "linux",
        "windows": "windows",
    }.get(system)
    machine_name = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "x86_64",
        "amd64": "x86_64",
    }.get(machine)
    key = (
        f"{system_name}-{machine_name}"
        if system_name is not None and machine_name is not None
        else ""
    )
    if key not in SUPPORTED_PLATFORM_KEYS:
        label = f"{system or 'unknown'}-{machine or 'unknown'}"
        raise UnsupportedPlatformError(
            f"Unsupported platform {label}; configure an absolute custom "
            f"sem {SEM_VERSION} binary."
        )
    return key


def _manifest() -> dict[str, Any]:
    try:
        payload = json.loads(
            RELEASE_MANIFEST_PATH.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"Unable to load the sem release manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise InstallError("The sem release manifest must be a JSON object.")
    if payload.get("version") != SEM_VERSION:
        raise InstallError(
            "Release manifest version does not match the plugin contract."
        )
    if payload.get("source_commit") != SEM_SOURCE_COMMIT:
        raise InstallError(
            "Release manifest source commit does not match the plugin contract."
        )
    if payload.get("base_url") != OFFICIAL_BASE_URL:
        raise InstallError(
            "Release manifest must use the pinned official HTTPS base URL."
        )

    platforms = payload.get("platforms")
    if not isinstance(platforms, dict) or not platforms:
        raise InstallError("Release manifest has no platform mappings.")
    if set(platforms) != SUPPORTED_PLATFORM_KEYS:
        raise InstallError(
            "Release manifest must contain the exact five pinned platform "
            "mappings."
        )
    for key, expected in PINNED_PLATFORM_RELEASES.items():
        entry = platforms.get(key)
        if not isinstance(entry, dict):
            raise InstallError(f"Invalid release mapping for {key!r}.")
        _validate_release_entry(key, entry)
        actual = (
            entry["asset"],
            entry["sha256"],
            entry["binary_sha256"],
            entry["archive"],
            entry["binary"],
        )
        if actual != expected:
            raise InstallError(
                f"Release mapping for {key} does not match its pinned "
                "release tuple."
            )
    return payload


def _validate_release_entry(key: str, entry: dict[str, Any]) -> None:
    required = {
        "asset",
        "sha256",
        "binary_sha256",
        "archive",
        "binary",
    }
    if set(entry) != required:
        raise InstallError(f"Release mapping for {key} has invalid fields.")

    asset = entry.get("asset")
    digest = entry.get("sha256")
    binary_digest = entry.get("binary_sha256")
    archive_type = entry.get("archive")
    binary = entry.get("binary")
    if (
        not isinstance(asset, str)
        or not asset
        or PurePosixPath(asset).name != asset
        or "\\" in asset
    ):
        raise InstallError(f"Release mapping for {key} has an unsafe asset name.")
    if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
        raise InstallError(f"Release mapping for {key} has an invalid SHA-256.")
    if (
        not isinstance(binary_digest, str)
        or SHA256_PATTERN.fullmatch(binary_digest) is None
    ):
        raise InstallError(
            f"Release mapping for {key} has an invalid binary SHA-256."
        )
    if archive_type not in {"tar.gz", "zip"}:
        raise InstallError(f"Release mapping for {key} has an invalid archive type.")
    if (
        not isinstance(binary, str)
        or not binary
        or PurePosixPath(binary).name != binary
        or "\\" in binary
    ):
        raise InstallError(f"Release mapping for {key} has an unsafe binary name.")
    if archive_type == "tar.gz" and not asset.endswith(".tar.gz"):
        raise InstallError(f"Release mapping for {key} has a mismatched archive.")
    if archive_type == "zip" and not asset.endswith(".zip"):
        raise InstallError(f"Release mapping for {key} has a mismatched archive.")


def _release_entry() -> tuple[dict[str, Any], dict[str, Any], str]:
    manifest = _manifest()
    key = platform_key()
    entry = manifest["platforms"].get(key)
    if not isinstance(entry, dict):
        raise InstallError(
            f"No bundled installer mapping for {key}; configure a custom binary."
        )
    return manifest, entry, key


def _proven_data_root() -> Path:
    try:
        plugin_root = RELEASE_MANIFEST_PATH.parent.resolve(strict=True)
    except OSError as exc:
        raise InstallError(f"Unable to resolve the plugin root: {exc}") from exc
    data_root = DATA_ROOT.resolve(strict=False)
    if data_root.name != ".data" or data_root.parent != plugin_root:
        raise InstallError("Plugin data path is outside the plugin root.")
    return data_root


def _destination_for(entry: dict[str, Any], key: str) -> Path:
    data_root = _proven_data_root()
    destination = data_root / "bin" / key / str(entry["binary"])
    resolved = destination.resolve(strict=False)
    if data_root not in resolved.parents:
        raise InstallError("sem binary destination is outside plugin-owned data.")
    return destination


def installed_binary_path() -> Path:
    _manifest_payload, entry, key = _release_entry()
    return _destination_for(entry, key)


def _drain_bounded_pipe(
    stream: BinaryIO,
    output: bytearray,
    state: dict[str, bool],
) -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            remaining = max(0, MAX_VERSION_OUTPUT_BYTES - len(output))
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
        command = [
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
        ]
        try:
            killer = subprocess.Popen(
                command,
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
        _signal_process_tree(process, force=True)
        try:
            process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _run_version(path: Path) -> tuple[int, bytes, bytes, bool]:
    popen_options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
    }
    if os.name == "nt":
        popen_options["creationflags"] = getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0,
        )
    else:
        popen_options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            [str(path), "--version"],
            **popen_options,
        )
    except OSError as exc:
        raise InstallError(f"Unable to execute sem --version: {exc}") from exc
    if process.stdout is None or process.stderr is None:
        _terminate_and_reap(process)
        raise InstallError("Unable to capture bounded sem version output.")

    stdout = bytearray()
    stderr = bytearray()
    stdout_state = {"truncated": False, "read_error": False}
    stderr_state = {"truncated": False, "read_error": False}
    readers = [
        threading.Thread(
            target=_drain_bounded_pipe,
            args=(process.stdout, stdout, stdout_state),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_bounded_pipe,
            args=(process.stderr, stderr, stderr_state),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        process.wait(timeout=VERSION_TIMEOUT_SECONDS)
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
        raise InstallError("sem --version timed out.")
    if any(reader.is_alive() for reader in readers):
        raise InstallError("Unable to reap sem version output readers.")
    if stdout_state["read_error"] or stderr_state["read_error"]:
        raise InstallError("Unable to read sem version output safely.")
    truncated = stdout_state["truncated"] or stderr_state["truncated"]
    return process.returncode, bytes(stdout), bytes(stderr), truncated


def validate_binary(path: Path) -> str:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise InstallError("Custom sem binary path must be absolute.")
    try:
        metadata = candidate.lstat()
    except FileNotFoundError as exc:
        raise InstallError(f"sem binary does not exist: {candidate}") from exc
    except OSError as exc:
        raise InstallError(f"Unable to inspect sem binary: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise InstallError("sem binary must be a regular, non-symlink file.")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise InstallError(f"Unable to resolve sem binary: {exc}") from exc
    if os.name != "nt" and not os.access(resolved, os.X_OK):
        raise InstallError("sem binary is not executable.")

    returncode, stdout_bytes, stderr_bytes, truncated = _run_version(resolved)
    if truncated:
        raise InstallError("sem --version output exceeded the output limit.")
    try:
        stdout = stdout_bytes.decode("utf-8", errors="strict")
        stderr = stderr_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise InstallError("sem --version returned invalid UTF-8 output.") from exc
    output = stdout if stdout else stderr
    if returncode == 0 and VERSION_PATTERN.fullmatch(output) is not None:
        return SEM_VERSION

    identity = SEM_IDENTITY_PATTERN.fullmatch(output)
    if identity is not None and identity.group(1) != SEM_VERSION:
        raise InstallError(
            f"sem {SEM_VERSION} is required; found {identity.group(1)}."
        )
    raise InstallError(
        "Binary is not the supported Ataraxy sem CLI; GNU Parallel's sem "
        "is rejected."
    )


def _validate_initial_download_url(url: str) -> None:
    expected_prefix = f"{OFFICIAL_BASE_URL}/"
    if not url.startswith(expected_prefix):
        raise InstallError("sem downloads must use the pinned official HTTPS URL.")
    suffix = url.removeprefix(expected_prefix)
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or not suffix
        or "/" in suffix
        or "\\" in suffix
    ):
        raise InstallError("sem downloads must use the pinned official HTTPS URL.")


def _validate_final_download_url(url: str, initial_url: str) -> None:
    if url == initial_url:
        return
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise InstallError("sem release redirect URL is malformed.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in RELEASE_ASSET_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or not parsed.path.startswith("/github-production-release-asset/")
    ):
        raise InstallError(
            "sem release redirect must use an approved HTTPS GitHub "
            "release-asset host."
        )


def _remaining_download_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise InstallError("sem release download timed out.")
    return remaining


def _close_response(response: object) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


class _PinnedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(
        self,
        initial_url: str,
        *,
        deadline: float,
        max_hops: int = MAX_REDIRECT_HOPS,
    ) -> None:
        super().__init__()
        self.initial_url = initial_url
        self.deadline = deadline
        self.max_hops = max_hops
        self.hops = 0

    def http_error_302(
        self,
        request: urllib.request.Request,
        response: object,
        code: int,
        message: str,
        headers: Any,
    ) -> object | None:
        location = headers.get("location") or headers.get("Location")
        if not isinstance(location, str) or not location:
            _close_response(response)
            raise InstallError("sem release redirect has no valid location.")
        if self.hops >= self.max_hops:
            _close_response(response)
            raise InstallError("sem release redirect limit exceeded.")

        target = urllib.parse.urljoin(request.full_url, location)
        try:
            _validate_final_download_url(target, self.initial_url)
        except Exception:
            _close_response(response)
            raise
        remaining = _remaining_download_time(self.deadline)
        redirected = super().redirect_request(
            request,
            response,
            code,
            message,
            headers,
            target,
        )
        if redirected is None:
            _close_response(response)
            raise InstallError("sem release redirect could not be followed.")
        self.hops += 1
        _close_response(response)
        return self.parent.open(redirected, timeout=remaining)

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


def _build_download_opener(
    initial_url: str,
    deadline: float,
) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        _PinnedRedirectHandler(
            initial_url,
            deadline=deadline,
            max_hops=MAX_REDIRECT_HOPS,
        )
    )


def _set_response_timeout(response: object, timeout: float) -> None:
    current = response
    seen: set[int] = set()
    for _ in range(8):
        identity = id(current)
        if identity in seen:
            return
        seen.add(identity)
        setter = getattr(current, "settimeout", None)
        if callable(setter):
            setter(timeout)
            return
        next_value = None
        for attribute in ("fp", "raw", "_sock"):
            candidate = getattr(current, attribute, None)
            if candidate is not None:
                next_value = candidate
                break
        if next_value is None:
            return
        current = next_value


def _download(url: str, timeout: int = DOWNLOAD_TIMEOUT_SECONDS) -> bytes:
    _validate_initial_download_url(url)
    if timeout <= 0:
        raise InstallError("sem release download timed out.")
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Agent-Zero-sem-review-loop/1.2.1"},
    )
    opener = _build_download_opener(url, deadline)
    try:
        with opener.open(
            request,
            timeout=_remaining_download_time(deadline),
        ) as response:
            get_url = getattr(response, "geturl", None)
            if callable(get_url):
                _validate_final_download_url(str(get_url()), url)
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except (TypeError, ValueError) as exc:
                    raise InstallError(
                        "sem release returned an invalid Content-Length."
                    ) from exc
                if declared_size < 0 or declared_size > MAX_ARCHIVE_BYTES:
                    raise InstallError(
                        "sem release archive exceeds the 128 MiB size limit."
                    )

            output = bytearray()
            while True:
                remaining = _remaining_download_time(deadline)
                _set_response_timeout(response, remaining)
                chunk = response.read(1024 * 1024)
                _remaining_download_time(deadline)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_ARCHIVE_BYTES:
                    raise InstallError(
                        "sem release archive exceeds the 128 MiB size limit."
                    )
            return bytes(output)
    except InstallError:
        raise
    except TimeoutError as exc:
        raise InstallError("sem release download timed out.") from exc
    except (OSError, ValueError, urllib.error.URLError) as exc:
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            raise InstallError("sem release download timed out.") from exc
        raise InstallError(f"Unable to download the sem release: {exc}") from exc


def _archive_member_path(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or (path.parts and ":" in path.parts[0])
    ):
        raise InstallError("Unsafe path in sem release archive.")
    return path


def _read_bounded(stream: BinaryIO, limit: int) -> bytes:
    output = bytearray()
    while True:
        chunk = stream.read(min(1024 * 1024, limit + 1 - len(output)))
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        if len(output) > limit:
            raise InstallError("Extracted sem binary exceeds the size limit.")


def _metadata_bytes(*values: str) -> int:
    return sum(len(value.encode("utf-8", errors="replace")) for value in values)


class _BoundedExpandedReader:
    def __init__(self, stream: BinaryIO, limit: int) -> None:
        self.stream = stream
        self.limit = limit
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self.limit + 1 - self.total
        request_size = remaining if size < 0 else min(size, remaining)
        data = self.stream.read(request_size)
        self.total += len(data)
        if self.total > self.limit:
            raise InstallError(
                "sem release archive exceeds the expanded size limit."
            )
        return data


def _extract_tar_binary(archive: bytes, binary_name: str) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(archive), mode="rb") as compressed:
            expanded = _BoundedExpandedReader(
                compressed,
                MAX_EXPANDED_ARCHIVE_BYTES,
            )
            bundle = tarfile.open(fileobj=expanded, mode="r|")
            with bundle:
                member_count = 0
                metadata_size = 0
                expanded_size = 0
                matching_count = 0
                binary: bytes | None = None
                for member in bundle:
                    member_count += 1
                    if member_count > MAX_ARCHIVE_MEMBERS:
                        raise InstallError(
                            "sem release archive exceeds the member count "
                            "limit."
                        )
                    metadata_size += 512 + _metadata_bytes(
                        member.name,
                        member.linkname or "",
                    )
                    if metadata_size > MAX_ARCHIVE_METADATA_BYTES:
                        raise InstallError(
                            "sem release archive exceeds the metadata limit."
                        )
                    if member.size < 0:
                        raise InstallError(
                            "Unsafe sem binary archive member."
                        )
                    expanded_size += member.size
                    if expanded_size > MAX_EXPANDED_ARCHIVE_BYTES:
                        raise InstallError(
                            "sem release archive exceeds the expanded size "
                            "limit."
                        )

                    path = _archive_member_path(member.name)
                    if path.name != binary_name:
                        continue
                    matching_count += 1
                    if (
                        member.issym()
                        or member.islnk()
                        or not member.isfile()
                    ):
                        raise InstallError(
                            "Unsafe sem binary archive member."
                        )
                    if member.size > MAX_BINARY_BYTES:
                        raise InstallError(
                            "Extracted sem binary exceeds the size limit."
                        )
                    stream = bundle.extractfile(member)
                    if stream is None:
                        raise InstallError(
                            "Unable to read sem binary from the release "
                            "archive."
                        )
                    with stream:
                        binary = _read_bounded(
                            stream,
                            MAX_BINARY_BYTES,
                        )
                if matching_count != 1 or binary is None:
                    raise InstallError(
                        "Release archive must contain exactly one sem binary."
                    )
                return binary
    except InstallError:
        raise
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise InstallError(f"Unable to read sem tar archive: {exc}") from exc


def _zip_member_is_link(member: zipfile.ZipInfo) -> bool:
    mode = (member.external_attr >> 16) & 0xFFFF
    return member.create_system == 3 and stat.S_ISLNK(mode)


def _zip_member_is_special(member: zipfile.ZipInfo) -> bool:
    if member.create_system != 3:
        return False
    mode = (member.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    return file_type not in {0, stat.S_IFREG}


def _extract_zip_binary(archive: bytes, binary_name: str) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise InstallError(
                    "sem release archive exceeds the member count limit."
                )
            metadata_size = sum(
                46
                + _metadata_bytes(member.filename)
                + len(member.extra)
                + len(member.comment)
                for member in members
            )
            if metadata_size > MAX_ARCHIVE_METADATA_BYTES:
                raise InstallError(
                    "sem release archive exceeds the metadata limit."
                )
            expanded_size = sum(member.file_size for member in members)
            if expanded_size > MAX_EXPANDED_ARCHIVE_BYTES:
                raise InstallError(
                    "sem release archive exceeds the expanded size limit."
                )
            paths = [_archive_member_path(member.filename) for member in members]
            matches = [
                member
                for member, path in zip(members, paths, strict=True)
                if path.name == binary_name
            ]
            if len(matches) != 1:
                raise InstallError(
                    "Release archive must contain exactly one sem binary."
                )
            member = matches[0]
            if (
                member.is_dir()
                or _zip_member_is_link(member)
                or _zip_member_is_special(member)
            ):
                raise InstallError("Unsafe sem binary archive member.")
            if member.file_size < 0 or member.file_size > MAX_BINARY_BYTES:
                raise InstallError(
                    "Extracted sem binary exceeds the size limit."
                )
            with bundle.open(member, mode="r") as stream:
                return _read_bounded(stream, MAX_BINARY_BYTES)
    except InstallError:
        raise
    except (OSError, EOFError, zipfile.BadZipFile, RuntimeError) as exc:
        raise InstallError(f"Unable to read sem zip archive: {exc}") from exc


def _extract_binary(archive: bytes, entry: dict[str, Any]) -> bytes:
    binary_name = entry.get("binary")
    archive_type = entry.get("archive")
    if not isinstance(binary_name, str) or not binary_name:
        raise InstallError("Release mapping has no binary name.")
    if archive_type == "tar.gz":
        return _extract_tar_binary(archive, binary_name)
    if archive_type == "zip":
        return _extract_zip_binary(archive, binary_name)
    raise InstallError(f"Unsupported archive type: {archive_type}")


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _copy_verified_managed_binary(
    source_path: Path,
    expected_digest: str,
    *,
    prefix: str = ".sem-verify-",
) -> tuple[Path, tuple[int, int, int, int, int]]:
    snapshot_path: Path | None = None
    try:
        initial = source_path.lstat()
        if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
            raise InstallError(
                "Managed sem binary must be a regular, non-symlink file."
            )
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_BINARY", 0)
        descriptor = os.open(source_path, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            opened = os.fstat(source.fileno())
            identity = _file_identity(opened)
            if _file_identity(initial) != identity:
                raise InstallError("Managed sem binary changed before validation.")
            with tempfile.NamedTemporaryFile(
                dir=source_path.parent,
                prefix=prefix,
                suffix=source_path.suffix,
                delete=False,
            ) as snapshot:
                snapshot_path = Path(snapshot.name)
                digest = hashlib.sha256()
                copied = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > MAX_BINARY_BYTES:
                        raise InstallError(
                            "Managed sem binary exceeds the size limit."
                        )
                    digest.update(chunk)
                    snapshot.write(chunk)
                snapshot.flush()
                os.fsync(snapshot.fileno())
        current = source_path.lstat()
        if _file_identity(current) != identity:
            raise InstallError("Managed sem binary changed during validation.")
        if not hmac.compare_digest(digest.hexdigest(), expected_digest):
            raise InstallError("Managed sem binary SHA-256 verification failed.")
        snapshot_path.chmod(stat.S_IRUSR | stat.S_IXUSR)
        return snapshot_path, identity
    except InstallError:
        if snapshot_path is not None:
            snapshot_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        if snapshot_path is not None:
            snapshot_path.unlink(missing_ok=True)
        raise InstallError(
            f"Unable to verify the managed sem binary safely: {exc}"
        ) from exc


def _validate_managed_binary(path: Path, expected_digest: str) -> str:
    snapshot, identity = _copy_verified_managed_binary(
        path,
        expected_digest,
    )
    try:
        version = validate_binary(snapshot)
        try:
            current = path.lstat()
        except OSError as exc:
            raise InstallError(
                "Managed sem binary changed during version validation."
            ) from exc
        if _file_identity(current) != identity:
            raise InstallError(
                "Managed sem binary changed during version validation."
            )
        return version
    finally:
        snapshot.unlink(missing_ok=True)


def _assert_managed_identity(
    path: Path,
    identity: tuple[int, int, int, int, int],
) -> None:
    try:
        current = path.lstat()
    except OSError as exc:
        raise InstallError(
            "Managed sem binary changed during version validation."
        ) from exc
    if _file_identity(current) != identity:
        raise InstallError(
            "Managed sem binary changed during version validation."
        )


def _verify_binary_bytes(binary: bytes, expected_digest: str) -> None:
    digest = hashlib.sha256(binary).hexdigest()
    if not hmac.compare_digest(digest, expected_digest):
        raise InstallError("Extracted sem binary SHA-256 verification failed.")


def _verify_file_digest(path: Path, expected_digest: str) -> None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise InstallError(f"Unable to verify sem binary bytes: {exc}") from exc
    if not hmac.compare_digest(digest.hexdigest(), expected_digest):
        raise InstallError("sem binary SHA-256 verification failed.")


def ensure_installed() -> Path:
    manifest, entry, key = _release_entry()
    destination = _destination_for(entry, key)
    try:
        ensure_data_dirs()
    except (OSError, RuntimeError) as exc:
        raise InstallError(f"Unable to prepare plugin data directories: {exc}") from exc

    if destination.is_symlink():
        raise InstallError("Existing sem destination is not a regular file.")
    if destination.exists():
        if not destination.is_file():
            raise InstallError("Existing sem destination is not a regular file.")
        try:
            _validate_managed_binary(
                destination,
                str(entry["binary_sha256"]),
            )
        except InstallError:
            pass
        else:
            return destination

    url = f"{str(manifest['base_url']).rstrip('/')}/{entry['asset']}"
    archive = _download(url)
    digest = hashlib.sha256(archive).hexdigest()
    if not hmac.compare_digest(digest, str(entry["sha256"])):
        raise InstallError("sem release SHA-256 verification failed.")
    binary = _extract_binary(archive, entry)
    if len(binary) > MAX_BINARY_BYTES:
        raise InstallError("Extracted sem binary exceeds the size limit.")
    _verify_binary_bytes(binary, str(entry["binary_sha256"]))

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        resolved_destination = destination.resolve(strict=False)
        if _proven_data_root() not in resolved_destination.parents:
            raise InstallError(
                "sem binary destination is outside plugin-owned data."
            )
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=".sem-install-",
            suffix=destination.suffix,
            delete=False,
        ) as temporary:
            temporary.write(binary)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
    except InstallError:
        raise
    except OSError as exc:
        raise InstallError(f"Unable to prepare the sem binary: {exc}") from exc

    try:
        mode = temporary_path.stat().st_mode
        temporary_path.chmod(
            mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        _verify_file_digest(
            temporary_path,
            str(entry["binary_sha256"]),
        )
        validate_binary(temporary_path)
        os.replace(temporary_path, destination)
        _validate_managed_binary(
            destination,
            str(entry["binary_sha256"]),
        )
    except InstallError:
        raise
    except OSError as exc:
        raise InstallError(f"Unable to install the sem binary: {exc}") from exc
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination


@contextmanager
def lease_binary(custom_binary: str = "") -> Iterator[Path]:
    """Yield an executable whose managed bytes stay pinned for the lease."""
    if custom_binary.strip():
        path = Path(custom_binary).expanduser()
        validate_binary(path)
        yield path.resolve(strict=True)
        return

    path = ensure_installed()
    _manifest_payload, entry, key = _release_entry()
    expected_path = _destination_for(entry, key)
    if path != expected_path:
        raise InstallError("Managed sem installer returned an unexpected path.")
    snapshot, identity = _copy_verified_managed_binary(
        path,
        str(entry["binary_sha256"]),
        prefix=".sem-lease-",
    )
    try:
        validate_binary(snapshot)
        _assert_managed_identity(path, identity)
        yield snapshot
    finally:
        snapshot.unlink(missing_ok=True)


def resolve_binary(custom_binary: str = "") -> Path:
    """Resolve only an explicit custom binary.

    Managed binaries must be executed inside ``lease_binary()`` so callers
    cannot receive the mutable cache path after verification.
    """
    if not custom_binary.strip():
        raise InstallError(
            "Managed sem binaries must be executed through lease_binary()."
        )
    path = Path(custom_binary).expanduser()
    validate_binary(path)
    return path.resolve(strict=True)


def remove_plugin_data() -> None:
    data_root = _proven_data_root()
    if not data_root.exists():
        return
    if data_root.is_symlink() or not data_root.is_dir():
        raise InstallError("Refusing to remove an unsafe plugin data path.")
    try:
        shutil.rmtree(data_root)
    except OSError as exc:
        raise InstallError(f"Unable to remove plugin-owned data: {exc}") from exc
