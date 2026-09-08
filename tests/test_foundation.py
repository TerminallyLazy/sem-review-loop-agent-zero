from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_manifest_contract_is_exact() -> None:
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest == {
        "name": "sem_review_loop",
        "title": "Semantic Review Loop",
        "description": (
            "Entity-level review, bounded repair, and approved project "
            "lessons powered by sem."
        ),
        "version": "1.2.0",
        "settings_sections": ["agent"],
        "per_project_config": True,
        "per_agent_config": False,
        "always_enabled": False,
    }


def test_default_config_contract_is_exact() -> None:
    defaults = yaml.safe_load(
        (ROOT / "default_config.yaml").read_text(encoding="utf-8")
    )
    assert defaults == {
        "watched_subdirectory": ".",
        "automatic_refresh": True,
        "debounce_ms": 400,
        "automatic_repair": False,
        "max_repair_cycles": 2,
        "custom_sem_binary": "",
        "context_token_budget": 8000,
        "working_tree_payload_mb": 256,
    }


def test_release_manifest_contract_is_exact() -> None:
    release = json.loads(
        (ROOT / "release_manifest.json").read_text(encoding="utf-8")
    )
    assert release == {
        "version": "0.21.0",
        "source_commit": "a4e8b53521034536dbe26067f948085870d59658",
        "base_url": (
            "https://github.com/Ataraxy-Labs/sem/releases/download/v0.21.0"
        ),
        "platforms": {
            "darwin-arm64": {
                "asset": "sem-darwin-arm64.tar.gz",
                "sha256": (
                    "7e17372ffdf6477a2b711e173fb783ecd82a4559ee3747985e2397c"
                    "128d1e6f7"
                ),
                "binary_sha256": (
                    "818c7af64e71b71c37dee84ad5096b05ea09c9b0401828f818c685"
                    "d3da13b81d"
                ),
                "archive": "tar.gz",
                "binary": "sem",
            },
            "darwin-x86_64": {
                "asset": "sem-darwin-x86_64.tar.gz",
                "sha256": (
                    "b179b996cf6060d74873fc117b2dd94104af9835ff48097c6e9c292"
                    "3a374dee1"
                ),
                "binary_sha256": (
                    "25dfe641dd348f1fe153fe0177ad97bd4334e83d148ac18b2bb9e5e6"
                    "97bae1f0"
                ),
                "archive": "tar.gz",
                "binary": "sem",
            },
            "linux-arm64": {
                "asset": "sem-linux-arm64.tar.gz",
                "sha256": (
                    "0480663055d3d7c386dabee6e57766205984ac151bd691540bde0b3b"
                    "e64af27b"
                ),
                "binary_sha256": (
                    "c69626bb9e99fd5de5c7f9de29567e675155941e36559fe205a750946"
                    "d350680"
                ),
                "archive": "tar.gz",
                "binary": "sem",
            },
            "linux-x86_64": {
                "asset": "sem-linux-x86_64.tar.gz",
                "sha256": (
                    "4a06f019552add37b4b0693309daaf529eae7f291217d20c291294c7"
                    "90b16b4b"
                ),
                "binary_sha256": (
                    "23206983bacf23f613452a1fdd97df8e6dfedfd3dce9fff1c03f02672"
                    "fac2ef7"
                ),
                "archive": "tar.gz",
                "binary": "sem",
            },
            "windows-x86_64": {
                "asset": "sem-windows-x86_64.zip",
                "sha256": (
                    "8ead28b095b829dba340e77d8d184394918223bd62a3fcd7cd634df5"
                    "0246fc9b"
                ),
                "binary_sha256": (
                    "4a5c4a666d022aed6574b00ea384dd433625817ecfaa20e2052c375f19"
                    "bdf2ed"
                ),
                "archive": "zip",
                "binary": "sem.exe",
            },
        },
    }


def test_repository_hygiene_and_licenses_are_complete() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ignored == [
        ".DS_Store",
        ".data/",
        ".pytest_cache/",
        "__pycache__/",
        "*.py[cod]",
    ]

    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith(
        "MIT License\n\nCopyright (c) 2026 TerminallyLazy\n"
    )
    assert "Permission is hereby granted, free of charge" in license_text
    assert "THE SOFTWARE IS PROVIDED \"AS IS\"" in license_text
    assert license_text.rstrip().endswith(
        "SOFTWARE."
    )

    notice = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "https://github.com/Ataraxy-Labs/sem" in notice
    assert "MIT OR Apache-2.0" in notice
    assert "does not bundle or modify" in notice


def test_readme_states_safety_installation_and_limitations() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required = (
        "# Semantic Review Loop",
        "Local-only",
        "telemetry",
        "Automatic, project-scoped MCP activation",
        "Automatic Repair is off by default",
        "Lessons remain inactive until approved",
        "No staging, commits, reverts, pushes",
        "global package managers",
        "system services",
        "plugin-owned binaries",
        "drifted MCP entry is preserved",
        "macOS arm64 and x86_64",
        "Linux arm64 and x86_64",
        "Windows x86_64",
        "non-Git",
    )
    for marker in required:
        assert marker in readme
    assert "available in the Plugin Index" not in readme
    assert "hosted CI" not in readme
