from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import time
import types
import zipfile
from pathlib import Path

import pytest

from usr.plugins.sem_review_loop import hooks
from usr.plugins.sem_review_loop.helpers import (
    installer,
    maintenance,
    paths as plugin_paths,
)


SOURCE_COMMIT = "a4e8b53521034536dbe26067f948085870d59658"
OFFICIAL_BASE_URL = (
    "https://github.com/Ataraxy-Labs/sem/releases/download/v0.21.0"
)
FAKE_SEM_BYTES = b"#!/bin/sh\nprintf 'sem 0.21.0\\n'\n"


class FakeDownloadResponse:
    def __init__(self, payload: bytes, final_url: str) -> None:
        self._stream = io.BytesIO(payload)
        self._final_url = final_url
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self) -> FakeDownloadResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def geturl(self) -> str:
        return self._final_url

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


class FakeDownloadOpener:
    def __init__(self, response: FakeDownloadResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, float]] = []

    def open(
        self,
        request: object,
        timeout: float,
    ) -> FakeDownloadResponse:
        self.calls.append(
            (request.full_url, timeout)  # type: ignore[attr-defined]
        )
        return self.response


def tar_with_members(*members: tuple[tarfile.TarInfo, bytes | None]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for info, content in members:
            archive.addfile(info, None if content is None else io.BytesIO(content))
    return stream.getvalue()


def tar_file(name: str, content: bytes) -> bytes:
    info = tarfile.TarInfo(name=name)
    info.mode = 0o755
    info.size = len(content)
    return tar_with_members((info, content))


@pytest.fixture
def fake_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, bytes]:
    archive = tar_file("sem", FAKE_SEM_BYTES)
    manifest = {
        "version": "0.21.0",
        "source_commit": SOURCE_COMMIT,
        "base_url": OFFICIAL_BASE_URL,
        "platforms": {
            "linux-x86_64": {
                "asset": "sem-linux-x86_64.tar.gz",
                "sha256": hashlib.sha256(archive).hexdigest(),
                "binary_sha256": hashlib.sha256(FAKE_SEM_BYTES).hexdigest(),
                "archive": "tar.gz",
                "binary": "sem",
            }
        },
    }
    manifest_path = tmp_path / "release_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    data_root = tmp_path / ".data"

    monkeypatch.setattr(plugin_paths, "PLUGIN_ROOT", tmp_path)
    monkeypatch.setattr(plugin_paths, "DATA_ROOT", data_root)
    monkeypatch.setattr(plugin_paths, "RELEASE_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(plugin_paths, "BIN_ROOT", data_root / "bin")
    monkeypatch.setattr(plugin_paths, "CACHE_ROOT", data_root / "cache")
    monkeypatch.setattr(plugin_paths, "LESSON_ROOT", data_root / "projects")
    monkeypatch.setattr(
        plugin_paths,
        "PROJECT_DATA_ROOT",
        data_root / "projects",
    )
    monkeypatch.setattr(
        plugin_paths,
        "RECEIPT_PATH",
        data_root / "mcp_receipts.json",
    )
    monkeypatch.setattr(
        plugin_paths,
        "MCP_RECEIPTS_PATH",
        data_root / "mcp_receipts.json",
    )
    monkeypatch.setattr(installer, "RELEASE_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(installer, "DATA_ROOT", data_root)
    monkeypatch.setattr(installer, "platform_key", lambda: "linux-x86_64")
    monkeypatch.setattr(installer, "_manifest", lambda: manifest)
    return data_root, archive


def test_runtime_manifest_requires_all_five_pinned_platforms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.loads(
        installer.RELEASE_MANIFEST_PATH.read_text(encoding="utf-8")
    )
    payload["platforms"].pop("windows-x86_64")
    manifest_path = tmp_path / "release_manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(installer, "RELEASE_MANIFEST_PATH", manifest_path)
    with pytest.raises(installer.InstallError, match="five pinned"):
        installer._manifest()


def test_runtime_manifest_rejects_changed_pinned_tuple(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.loads(
        installer.RELEASE_MANIFEST_PATH.read_text(encoding="utf-8")
    )
    payload["platforms"]["darwin-arm64"]["sha256"] = "0" * 64
    manifest_path = tmp_path / "release_manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(installer, "RELEASE_MANIFEST_PATH", manifest_path)
    with pytest.raises(installer.InstallError, match="pinned release tuple"):
        installer._manifest()


def test_runtime_manifest_rejects_changed_binary_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.loads(
        installer.RELEASE_MANIFEST_PATH.read_text(encoding="utf-8")
    )
    payload["platforms"]["linux-x86_64"]["binary_sha256"] = "0" * 64
    manifest_path = tmp_path / "release_manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(installer, "RELEASE_MANIFEST_PATH", manifest_path)
    with pytest.raises(installer.InstallError, match="pinned release tuple"):
        installer._manifest()


def test_download_allows_legitimate_github_release_asset_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"verified later by the pinned SHA-256"
    initial_url = f"{OFFICIAL_BASE_URL}/sem-darwin-arm64.tar.gz"
    final_url = (
        "https://release-assets.githubusercontent.com/"
        "github-production-release-asset/123456/abcdef"
        "?sp=r&sv=2025-01-05"
    )
    opener = FakeDownloadOpener(FakeDownloadResponse(payload, final_url))
    monkeypatch.setattr(
        installer,
        "_build_download_opener",
        lambda initial_url, deadline: opener,
    )
    assert installer._download(initial_url) == payload
    assert len(opener.calls) == 1
    assert opener.calls[0][0] == initial_url
    assert 0 < opener.calls[0][1] <= installer.DOWNLOAD_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "final_url",
    [
        (
            "http://release-assets.githubusercontent.com/"
            "github-production-release-asset/123456/abcdef"
        ),
        "https://downloads.example.com/sem-darwin-arm64.tar.gz",
    ],
)
def test_download_rejects_insecure_or_unrelated_redirect(
    final_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_url = f"{OFFICIAL_BASE_URL}/sem-darwin-arm64.tar.gz"
    opener = FakeDownloadOpener(FakeDownloadResponse(b"archive", final_url))
    monkeypatch.setattr(
        installer,
        "_build_download_opener",
        lambda initial_url, deadline: opener,
    )
    with pytest.raises(installer.InstallError, match="redirect"):
        installer._download(initial_url)


def test_redirect_handler_validates_before_follow_and_bounds_hops() -> None:
    initial_url = f"{OFFICIAL_BASE_URL}/sem-darwin-arm64.tar.gz"
    allowed = (
        "https://release-assets.githubusercontent.com/"
        "github-production-release-asset/123456/abcdef?sp=r"
    )

    class Parent:
        def __init__(self) -> None:
            self.calls: list[tuple[str, float]] = []

        def open(self, request: object, timeout: float) -> object:
            self.calls.append(
                (request.full_url, timeout)  # type: ignore[attr-defined]
            )
            return object()

    class Response:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    parent = Parent()
    response = Response()
    handler = installer._PinnedRedirectHandler(
        initial_url,
        deadline=time.monotonic() + 30,
        max_hops=1,
    )
    handler.parent = parent
    request = installer.urllib.request.Request(initial_url)
    request.timeout = 30
    result = handler.http_error_302(
        request,
        response,
        302,
        "Found",
        {"location": allowed},
    )
    assert result is not None
    assert response.closed
    assert parent.calls[0][0] == allowed
    assert 0 < parent.calls[0][1] <= 30

    with pytest.raises(installer.InstallError, match="redirect limit"):
        handler.http_error_302(
            request,
            Response(),
            302,
            "Found",
            {"location": allowed},
        )


@pytest.mark.parametrize(
    "target",
    [
        (
            "http://release-assets.githubusercontent.com/"
            "github-production-release-asset/123456/abcdef"
        ),
        "https://downloads.example.com/release/sem",
    ],
)
def test_redirect_handler_rejects_target_before_follow(target: str) -> None:
    initial_url = f"{OFFICIAL_BASE_URL}/sem-darwin-arm64.tar.gz"

    class Parent:
        def open(self, request: object, timeout: float) -> object:
            pytest.fail("followed an unapproved redirect")

    handler = installer._PinnedRedirectHandler(
        initial_url,
        deadline=time.monotonic() + 30,
        max_hops=3,
    )
    handler.parent = Parent()
    request = installer.urllib.request.Request(initial_url)
    request.timeout = 30
    with pytest.raises(installer.InstallError, match="redirect"):
        handler.http_error_302(
            request,
            FakeDownloadResponse(b"", initial_url),
            302,
            "Found",
            {"location": target},
        )


def test_download_enforces_monotonic_overall_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_url = f"{OFFICIAL_BASE_URL}/sem-darwin-arm64.tar.gz"
    now = [100.0]

    class SlowResponse(FakeDownloadResponse):
        def read(self, size: int = -1) -> bytes:
            now[0] += 6
            return b"late"

    opener = FakeDownloadOpener(SlowResponse(b"", initial_url))
    monkeypatch.setattr(installer.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        installer,
        "_build_download_opener",
        lambda initial_url, deadline: opener,
    )
    with pytest.raises(installer.InstallError, match="timed out"):
        installer._download(initial_url, timeout=5)


def test_writable_paths_are_strictly_below_real_data_root() -> None:
    for path in (
        plugin_paths.BIN_ROOT,
        plugin_paths.CACHE_ROOT,
        plugin_paths.LESSON_ROOT,
        plugin_paths.RECEIPT_PATH,
    ):
        plugin_paths._require_plugin_owned(path)

    with pytest.raises(RuntimeError, match=r"\.data"):
        plugin_paths._require_plugin_owned(
            plugin_paths.PLUGIN_ROOT / "README.md"
        )
    with pytest.raises(RuntimeError, match=r"\.data"):
        plugin_paths._require_plugin_owned(plugin_paths.DATA_ROOT)


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "darwin-arm64"),
        ("Darwin", "x86_64", "darwin-x86_64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Linux", "AMD64", "linux-x86_64"),
        ("Windows", "x86_64", "windows-x86_64"),
    ],
)
def test_supported_platform_mapping(
    system: str,
    machine: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(installer.platform, "system", lambda: system)
    monkeypatch.setattr(installer.platform, "machine", lambda: machine)
    assert installer.platform_key() == expected


@pytest.mark.parametrize(
    ("system", "machine"),
    [("FreeBSD", "x86_64"), ("Windows", "arm64"), ("Linux", "riscv64")],
)
def test_unsupported_platform_mapping(
    system: str,
    machine: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(installer.platform, "system", lambda: system)
    monkeypatch.setattr(installer.platform, "machine", lambda: machine)
    with pytest.raises(installer.InstallError, match="Unsupported platform"):
        installer.platform_key()


def executable_file(tmp_path: Path) -> Path:
    binary = tmp_path / "sem"
    binary.write_bytes(b"binary")
    binary.chmod(0o755)
    return binary


def test_validate_binary_accepts_only_pinned_sem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = executable_file(tmp_path)
    calls: list[Path] = []

    def fake_run(path: Path) -> tuple[int, bytes, bytes, bool]:
        calls.append(path)
        return 0, b"sem 0.21.0\n", b"", False

    monkeypatch.setattr(installer, "_run_version", fake_run)
    assert installer.validate_binary(binary) == "0.21.0"
    assert calls == [binary.resolve()]


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("sem 0.20.0\n", "0.21.0 is required"),
        ("GNU parallel 20260722\n", "Ataraxy"),
        ("sem 0.21.0\nunexpected\n", "Ataraxy"),
    ],
)
def test_validate_binary_rejects_wrong_identity_or_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    message: str,
) -> None:
    binary = executable_file(tmp_path)
    monkeypatch.setattr(
        installer,
        "_run_version",
        lambda path: (0, output.encode(), b"", False),
    )
    with pytest.raises(installer.InstallError, match=message):
        installer.validate_binary(binary)


def test_validate_binary_rejects_invalid_utf8_and_truncated_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = executable_file(tmp_path)
    monkeypatch.setattr(
        installer,
        "_run_version",
        lambda path: (0, b"\xff", b"", False),
    )
    with pytest.raises(installer.InstallError, match="UTF-8"):
        installer.validate_binary(binary)

    monkeypatch.setattr(
        installer,
        "_run_version",
        lambda path: (0, b"sem 0.21.0\n", b"", True),
    )
    with pytest.raises(installer.InstallError, match="output limit"):
        installer.validate_binary(binary)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
def test_version_timeout_terminates_process_tree_and_returns_bounded_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "child.pid"
    binary = tmp_path / "sem"
    binary.write_text(
        "#!/bin/sh\n"
        "trap '' TERM\n"
        "sleep 30 &\n"
        "child=$!\n"
        f"printf '%s' \"$child\" > '{marker}'\n"
        "wait \"$child\"\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    monkeypatch.setattr(installer, "VERSION_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(installer, "PROCESS_TERMINATION_GRACE_SECONDS", 0.1)

    started = time.monotonic()
    with pytest.raises(installer.InstallError, match="timed out"):
        installer.validate_binary(binary)
    assert time.monotonic() - started < 2
    child_pid = int(marker.read_text(encoding="utf-8"))
    status = subprocess.run(
        ["ps", "-p", str(child_pid), "-o", "state="],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    ).stdout.strip()
    assert not status or status.startswith("Z")


def test_version_output_is_bounded_before_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "sem"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "sys.stdout.buffer.write(b'x' * 10000)\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    monkeypatch.setattr(installer, "MAX_VERSION_OUTPUT_BYTES", 32)
    with pytest.raises(installer.InstallError, match="output limit"):
        installer.validate_binary(binary)


def test_custom_binary_must_be_absolute_existing_regular_executable(
    tmp_path: Path,
) -> None:
    with pytest.raises(installer.InstallError, match="absolute"):
        installer.resolve_binary("relative/sem")
    with pytest.raises(installer.InstallError, match="exist"):
        installer.resolve_binary(str(tmp_path / "missing-sem"))

    target = executable_file(tmp_path)
    symlink = tmp_path / "sem-link"
    symlink.symlink_to(target)
    with pytest.raises(installer.InstallError, match="regular"):
        installer.resolve_binary(str(symlink))


def test_managed_binary_resolution_requires_verified_lease() -> None:
    with pytest.raises(installer.InstallError, match="lease_binary"):
        installer.resolve_binary()


def test_managed_binary_lease_executes_verified_snapshot_after_cache_replacement(
    fake_release: tuple[Path, bytes],
) -> None:
    data_root, _archive = fake_release
    managed = data_root / "bin/linux-x86_64/sem"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(FAKE_SEM_BYTES)
    managed.chmod(0o755)

    with installer.lease_binary() as leased:
        assert leased != managed
        assert leased.parent == managed.parent
        assert leased.name.startswith(".sem-lease-")
        assert leased.read_bytes() == FAKE_SEM_BYTES
        assert leased.stat().st_mode & 0o222 == 0

        managed.write_bytes(b"#!/bin/sh\nprintf 'tampered\\n'\n")
        managed.chmod(0o755)
        result = subprocess.run(
            [str(leased), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        assert result.returncode == 0
        assert result.stdout == "sem 0.21.0\n"
        assert leased.exists()

    assert not leased.exists()
    assert not list(managed.parent.glob(".sem-lease-*"))


def test_custom_binary_lease_yields_explicit_user_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom = executable_file(tmp_path)
    validated: list[Path] = []
    monkeypatch.setattr(
        installer,
        "validate_binary",
        lambda path: validated.append(path) or "0.21.0",
    )

    with installer.lease_binary(str(custom)) as leased:
        assert leased == custom.resolve(strict=True)

    assert validated == [custom]


def test_checksum_mismatch_never_creates_destination(
    fake_release: tuple[Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, _archive = fake_release
    monkeypatch.setattr(installer, "_download", lambda *_args, **_kwargs: b"wrong")
    with pytest.raises(installer.InstallError, match="SHA-256"):
        installer.ensure_installed()
    assert not (data_root / "bin/linux-x86_64/sem").exists()


def test_extracted_binary_digest_mismatch_never_executes_or_installs(
    fake_release: tuple[Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, archive = fake_release
    manifest = installer._manifest()
    manifest["platforms"]["linux-x86_64"]["binary_sha256"] = "0" * 64
    monkeypatch.setattr(installer, "_download", lambda *_args, **_kwargs: archive)
    monkeypatch.setattr(
        installer,
        "validate_binary",
        lambda path: pytest.fail("executed an unverified binary"),
    )
    with pytest.raises(installer.InstallError, match="binary SHA-256"):
        installer.ensure_installed()
    assert not (data_root / "bin/linux-x86_64/sem").exists()


def test_verified_archive_installs_atomically(
    fake_release: tuple[Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, archive = fake_release
    monkeypatch.setattr(installer, "_download", lambda *_args, **_kwargs: archive)
    validated: list[Path] = []

    def fake_validate(path: Path) -> str:
        assert path.read_bytes().startswith(b"#!/bin/sh")
        validated.append(path)
        return "0.21.0"

    monkeypatch.setattr(installer, "validate_binary", fake_validate)
    installed = installer.ensure_installed()
    assert installed == data_root / "bin/linux-x86_64/sem"
    assert installed.read_bytes().startswith(b"#!/bin/sh")
    if os.name != "nt":
        assert os.access(installed, os.X_OK)
    assert len(validated) == 2
    assert validated[0] != installed
    assert validated[1] != installed
    assert not list(installed.parent.glob(".sem-install-*"))
    assert not list(installed.parent.glob(".sem-verify-*"))


def test_existing_valid_binary_is_reused_without_download(
    fake_release: tuple[Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, _archive = fake_release
    destination = data_root / "bin/linux-x86_64/sem"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(FAKE_SEM_BYTES)
    destination.chmod(0o755)
    validated: list[Path] = []
    monkeypatch.setattr(installer, "validate_binary", lambda path: "0.21.0")
    monkeypatch.setattr(
        installer,
        "_download",
        lambda *_args, **_kwargs: pytest.fail("downloaded existing binary"),
    )
    assert installer.ensure_installed() == destination
    assert destination.read_bytes() == FAKE_SEM_BYTES


def test_tampered_cached_binary_is_not_executed_before_repair(
    fake_release: tuple[Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, archive = fake_release
    destination = data_root / "bin/linux-x86_64/sem"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"tampered cached executable")
    destination.chmod(0o755)
    validated: list[Path] = []
    monkeypatch.setattr(installer, "_download", lambda *_args, **_kwargs: archive)

    def fake_validate(path: Path) -> str:
        assert path != destination
        assert path.read_bytes() == FAKE_SEM_BYTES
        validated.append(path)
        return "0.21.0"

    monkeypatch.setattr(installer, "validate_binary", fake_validate)
    assert installer.ensure_installed() == destination
    assert destination.read_bytes() == FAKE_SEM_BYTES
    assert len(validated) == 2


def test_managed_validation_executes_verified_snapshot(
    fake_release: tuple[Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, _archive = fake_release
    managed = data_root / "bin/linux-x86_64/sem"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(FAKE_SEM_BYTES)
    managed.chmod(0o755)
    validated: list[Path] = []

    def fake_validate(path: Path) -> str:
        assert path != managed
        assert path.read_bytes() == FAKE_SEM_BYTES
        validated.append(path)
        return "0.21.0"

    monkeypatch.setattr(installer, "validate_binary", fake_validate)
    digest = hashlib.sha256(FAKE_SEM_BYTES).hexdigest()
    assert installer._validate_managed_binary(managed, digest) == "0.21.0"
    assert len(validated) == 1
    assert not list(managed.parent.glob(".sem-verify-*"))




def test_tar_extraction_rejects_traversal_link_and_ambiguity() -> None:
    entry = {"archive": "tar.gz", "binary": "sem"}

    traversal = tar_file("../sem", b"payload")
    with pytest.raises(installer.InstallError, match="Unsafe"):
        installer._extract_binary(traversal, entry)

    link = tarfile.TarInfo("sem")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../outside"
    linked = tar_with_members((link, None))
    with pytest.raises(installer.InstallError, match="Unsafe"):
        installer._extract_binary(linked, entry)

    first = tarfile.TarInfo("one/sem")
    first.size = 3
    second = tarfile.TarInfo("two/sem")
    second.size = 3
    ambiguous = tar_with_members((first, b"one"), (second, b"two"))
    with pytest.raises(installer.InstallError, match="exactly one"):
        installer._extract_binary(ambiguous, entry)


def test_tar_extraction_bounds_member_count_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sem = tarfile.TarInfo("sem")
    sem.size = 3
    extra = tarfile.TarInfo("metadata-name")
    extra.size = 1
    archive = tar_with_members((sem, b"sem"), (extra, b"x"))

    monkeypatch.setattr(installer, "MAX_ARCHIVE_MEMBERS", 1)
    with pytest.raises(installer.InstallError, match="member count"):
        installer._extract_binary(
            archive,
            {"archive": "tar.gz", "binary": "sem"},
        )

    monkeypatch.setattr(installer, "MAX_ARCHIVE_MEMBERS", 10)
    monkeypatch.setattr(installer, "MAX_ARCHIVE_METADATA_BYTES", 4)
    with pytest.raises(installer.InstallError, match="metadata"):
        installer._extract_binary(
            archive,
            {"archive": "tar.gz", "binary": "sem"},
        )


def test_zip_extraction_rejects_symlink_and_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = {"archive": "zip", "binary": "sem.exe"}

    link_stream = io.BytesIO()
    with zipfile.ZipFile(link_stream, mode="w") as bundle:
        member = zipfile.ZipInfo("sem.exe")
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(member, "../../outside")
    with pytest.raises(installer.InstallError, match="Unsafe"):
        installer._extract_binary(link_stream.getvalue(), entry)

    oversized_stream = io.BytesIO()
    with zipfile.ZipFile(oversized_stream, mode="w") as bundle:
        bundle.writestr("sem.exe", b"12345")
    monkeypatch.setattr(installer, "MAX_BINARY_BYTES", 4)
    with pytest.raises(installer.InstallError, match="size limit"):
        installer._extract_binary(oversized_stream.getvalue(), entry)


def test_zip_extraction_bounds_total_expanded_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, mode="w") as bundle:
        bundle.writestr("sem.exe", b"sem")
        bundle.writestr("unrelated", b"expanded")
    monkeypatch.setattr(installer, "MAX_EXPANDED_ARCHIVE_BYTES", 5)
    with pytest.raises(installer.InstallError, match="expanded size"):
        installer._extract_binary(
            stream.getvalue(),
            {"archive": "zip", "binary": "sem.exe"},
        )


def test_uninstall_removes_only_proven_plugin_data(
    fake_release: tuple[Path, bytes],
) -> None:
    data_root, _archive = fake_release
    (data_root / "cache").mkdir(parents=True)
    outside = data_root.parent / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    installer.remove_plugin_data()
    assert not data_root.exists()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_cleanup_refuses_unproven_data_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    manifest_path = plugin_root / "release_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    unsafe_root = tmp_path / "outside" / ".data"
    unsafe_root.mkdir(parents=True)
    marker = unsafe_root / "keep"
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(installer, "RELEASE_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(installer, "DATA_ROOT", unsafe_root)
    with pytest.raises(installer.InstallError, match="outside"):
        installer.remove_plugin_data()
    assert marker.exists()


def test_maintenance_prints_one_bounded_json_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    binary = tmp_path / "sem"
    monkeypatch.setattr(maintenance, "ensure_installed", lambda: binary)
    assert maintenance.main() == 0
    output = capsys.readouterr().out.splitlines()
    assert len(output) == 1
    assert json.loads(output[0]) == {
        "ok": True,
        "path": str(binary),
        "version": "0.21.0",
    }

    monkeypatch.setattr(
        maintenance,
        "ensure_installed",
        lambda: (_ for _ in ()).throw(installer.InstallError("x" * 2_000)),
    )
    assert maintenance.main() == 1
    output = capsys.readouterr().out.splitlines()
    assert len(output) == 1
    error = json.loads(output[0])
    assert error["ok"] is False
    assert 0 < len(error["error"]) <= maintenance.MAX_ERROR_CHARS


def test_hooks_install_and_async_uninstall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    warnings: list[str] = []

    monkeypatch.setattr(
        hooks,
        "ensure_installed",
        lambda: events.append("install"),
    )
    from helpers import projects
    monkeypatch.setattr(projects, "get_active_projects_list", lambda: [])
    import asyncio
    assert asyncio.run(hooks.install(ignored=True))
    assert events == ["install"]
    assert asyncio.run(hooks.pre_update(ignored=True))
    assert events == ["install", "install"]

    class FakeManager:
        async def disable_all_managed(self) -> list[str]:
            events.append("disable")
            return ["project-b", "project-a"]

    services = types.ModuleType(
        "usr.plugins.sem_review_loop.helpers.services"
    )
    services.get_mcp_manager = lambda: FakeManager()  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules,
        "usr.plugins.sem_review_loop.helpers.services",
        services,
    )
    monkeypatch.setattr(
        hooks.PrintStyle,
        "warning",
        lambda message: warnings.append(message),
    )
    monkeypatch.setattr(
        hooks,
        "remove_plugin_data",
        lambda: events.append("cleanup"),
    )

    import asyncio

    assert asyncio.run(hooks.uninstall(ignored=True))
    assert events == ["install", "install", "disable", "cleanup"]
    assert len(warnings) == 1
    assert "project-a, project-b" in warnings[0]


def test_install_hook_defers_unsupported_platform_to_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        hooks,
        "ensure_installed",
        lambda: (_ for _ in ()).throw(
            installer.UnsupportedPlatformError("unsupported")
        ),
    )
    monkeypatch.setattr(
        hooks.PrintStyle,
        "warning",
        lambda message: warnings.append(message),
    )
    from helpers import projects
    monkeypatch.setattr(projects, "get_active_projects_list", lambda: [])
    import asyncio
    assert asyncio.run(hooks.install())
    assert len(warnings) == 1
    assert "first semantic review" in warnings[0]
    assert "custom" in warnings[0]


def test_install_hook_propagates_supported_install_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        hooks,
        "ensure_installed",
        lambda: (_ for _ in ()).throw(installer.InstallError("checksum")),
    )
    with pytest.raises(installer.InstallError, match="checksum"):
        import asyncio
        asyncio.run(hooks.install())


def test_uninstall_aborts_and_preserves_recovery_data_on_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    errors: list[str] = []

    class FailingManager:
        async def disable_all_managed(self) -> list[str]:
            events.append("disable")
            raise RuntimeError("MCP settings unavailable")

    services = types.ModuleType(
        "usr.plugins.sem_review_loop.helpers.services"
    )
    services.get_mcp_manager = (  # type: ignore[attr-defined]
        lambda: FailingManager()
    )
    monkeypatch.setitem(
        sys.modules,
        "usr.plugins.sem_review_loop.helpers.services",
        services,
    )
    monkeypatch.setattr(
        hooks.PrintStyle,
        "error",
        lambda message: errors.append(message),
    )
    monkeypatch.setattr(
        hooks,
        "remove_plugin_data",
        lambda: events.append("cleanup"),
    )

    import asyncio

    with pytest.raises(RuntimeError, match="MCP settings unavailable"):
        asyncio.run(hooks.uninstall())
    assert events == ["disable"]
    assert len(errors) == 1
    assert "aborted" in errors[0]
    assert "recovery data" in errors[0]


def test_install_automatically_connects_existing_projects(monkeypatch, tmp_path):
    import asyncio
    from helpers import projects, plugins
    from usr.plugins.sem_review_loop.helpers import services
    root = tmp_path / 'existing-project'
    root.mkdir()
    calls = []
    class Manager:
        async def ensure_enabled(self, scope, config):
            calls.append((scope.project_name, scope.project_root, config.watched_subdirectory))
            return {'enabled': True}
    monkeypatch.setattr(hooks, 'ensure_installed', lambda: None)
    monkeypatch.setattr(projects, 'get_projects_parent_folder', lambda: str(tmp_path))
    monkeypatch.setattr(projects, 'get_active_projects_list', lambda: [{'name': root.name}])
    monkeypatch.setattr(projects, 'get_project_folder', lambda name: str(root))
    monkeypatch.setattr(plugins, 'get_plugin_config', lambda *args, **kwargs: {})
    monkeypatch.setattr(services, 'get_mcp_manager', lambda: Manager())
    assert asyncio.run(hooks.install())
    assert calls == [(root.name, root, '.')]
