from __future__ import annotations

from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PLUGIN_ROOT / ".data"
RELEASE_MANIFEST_PATH = PLUGIN_ROOT / "release_manifest.json"
BIN_ROOT = DATA_ROOT / "bin"
CACHE_ROOT = DATA_ROOT / "cache"
LESSON_ROOT = DATA_ROOT / "projects"
PROJECT_DATA_ROOT = LESSON_ROOT
RECEIPT_PATH = DATA_ROOT / "mcp_receipts.json"
MCP_RECEIPTS_PATH = RECEIPT_PATH


def _proven_data_root() -> Path:
    plugin_root = PLUGIN_ROOT.resolve(strict=True)
    data_root = DATA_ROOT.resolve(strict=False)
    if data_root.name != ".data" or data_root.parent != plugin_root:
        raise RuntimeError("Plugin data root is not the plugin-owned .data path.")
    return data_root


def _require_plugin_owned(path: Path) -> None:
    data_root = _proven_data_root()
    resolved = path.resolve(strict=False)
    if resolved == data_root or data_root not in resolved.parents:
        raise RuntimeError(
            f"Writable path is outside the plugin .data root: {path}"
        )


def ensure_data_dirs() -> None:
    data_root = _proven_data_root()
    data_root.mkdir(parents=True, exist_ok=True)
    for path in (BIN_ROOT, CACHE_ROOT, LESSON_ROOT):
        _require_plugin_owned(path)
        path.mkdir(parents=True, exist_ok=True)
    _require_plugin_owned(RECEIPT_PATH)
