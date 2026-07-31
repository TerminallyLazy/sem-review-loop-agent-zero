from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from usr.plugins.sem_review_loop.api.sem_mcp import SemMcp
from usr.plugins.sem_review_loop.helpers import mcp_manager as mcp_manager_module
from usr.plugins.sem_review_loop.helpers.api_support import (
    APIInputError,
    agent_scope_config,
)
from usr.plugins.sem_review_loop.helpers.config import parse_config
from usr.plugins.sem_review_loop.helpers.mcp_manager import (
    ConfigSnapshot,
    DirectProjectMCPStore,
    MCPConcurrentModificationError,
    MCPConflictError,
    MCPManager,
    MCPManagerError,
    MCPReceipt,
    MCPRollbackError,
    MCPStalePreviewError,
    MCPVerificationError,
    NATIVE_TOOLS,
    ReceiptStore,
    SERVER_NAME,
    canonical_hash,
    project_root_identity,
)
from usr.plugins.sem_review_loop.helpers.project_scope import (
    ProjectScope,
    make_scope,
)
from usr.plugins.sem_review_loop.helpers.registry import ReviewRegistry


CONFIG = parse_config({})
OTHER = {"command": "keep", "args": ["server"]}


class MemoryStore:
    def __init__(
        self,
        document: dict[str, Any] | None = None,
        *,
        raw: bytes | None = None,
        exists: bool = True,
    ) -> None:
        self.raw = (
            raw
            if raw is not None
            else (
                json.dumps(
                    document if document is not None else {"mcpServers": {}},
                    indent=2,
                )
                + "\n"
            ).encode()
        )
        self.exists = exists
        self.before_swap: Any = None
        self.after_swap: Any = None
        self.swaps = 0
        self._lock = threading.RLock()

    def _snapshot(self) -> ConfigSnapshot:
        from usr.plugins.sem_review_loop.helpers.mcp_manager import (
            MISSING_DIGEST,
            _snapshot_digest,
        )

        return ConfigSnapshot(
            path=Path("/memory/mcp_servers.json"),
            exists=self.exists,
            raw=self.raw if self.exists else b"",
            digest=(
                _snapshot_digest(True, self.raw)
                if self.exists
                else MISSING_DIGEST
            ),
        )

    def read(self, _scope: ProjectScope) -> ConfigSnapshot:
        with self._lock:
            return self._snapshot()

    def compare_and_swap(
        self,
        _scope: ProjectScope,
        expected: ConfigSnapshot,
        replacement: bytes | None,
    ) -> ConfigSnapshot:
        with self._lock:
            if callable(self.before_swap):
                callback = self.before_swap
                self.before_swap = None
                callback()
            current = self._snapshot()
            if (
                current.exists != expected.exists
                or current.digest != expected.digest
                or current.raw != expected.raw
            ):
                raise MCPConcurrentModificationError("concurrent edit")
            self.exists = replacement is not None
            self.raw = replacement or b""
            self.swaps += 1
            result = self._snapshot()
            if callable(self.after_swap):
                callback = self.after_swap
                self.after_swap = None
                callback()
            return result

    def document(self) -> dict[str, Any]:
        return json.loads(self.raw) if self.exists else {"mcpServers": {}}

    def replace_document(self, value: dict[str, Any]) -> None:
        with self._lock:
            self.exists = True
            self.raw = (json.dumps(value, indent=2) + "\n").encode()


class MemoryReceipts:
    def __init__(self) -> None:
        self.values: dict[str, MCPReceipt] = {}
        self.before_put: Any = None
        self.after_put: Any = None

    def get(self, project_name: str) -> MCPReceipt | None:
        return self.values.get(project_name)

    def put(self, receipt: MCPReceipt) -> None:
        if callable(self.before_put):
            self.before_put(receipt)
        self.values[receipt.project_name] = receipt
        if callable(self.after_put):
            self.after_put(receipt)

    def delete(self, project_name: str) -> None:
        self.values.pop(project_name, None)

    def all(self) -> tuple[MCPReceipt, ...]:
        return tuple(self.values.values())


class FakeMCPConfig:
    def __init__(
        self,
        enabled: set[str] | None = None,
        *,
        native: set[str] | None = None,
        disabled: set[str] | None = None,
    ) -> None:
        self.enabled = enabled if enabled is not None else {
            "sem_review_loop.sem_diff",
            "sem_review_loop.sem_context",
            "sem_review_loop.sem_impact",
        }
        self.native = native if native is not None else set(NATIVE_TOOLS)
        self.disabled = disabled if disabled is not None else {
            "sem_entities",
            "sem_blame",
            "sem_log",
        }

    def get_tools(self) -> list[dict[str, object]]:
        return [{name: {}} for name in sorted(self.enabled)]

    def get_server_detail(self, name: str) -> dict[str, object]:
        assert name == SERVER_NAME
        return {
            "tools": [
                {"name": tool, "disabled": tool in self.disabled}
                for tool in sorted(self.native)
            ]
        }


def scope(tmp_path: Path, name: str = "project") -> ProjectScope:
    root = tmp_path / name
    root.mkdir()
    return make_scope("ctx", name, root, ".")


def manager_for(
    project_scope: ProjectScope,
    *,
    store: MemoryStore | None = None,
    refresh: Any = None,
    inspect: Any = None,
    receipts: MemoryReceipts | None = None,
    global_raw: str = '{"mcpServers": {}}',
) -> tuple[MCPManager, MemoryStore, MemoryReceipts]:
    selected_store = store or MemoryStore()
    selected_receipts = receipts or MemoryReceipts()
    provider = refresh or (lambda _name: FakeMCPConfig())
    manager = MCPManager(
        registry=ReviewRegistry(),
        store=selected_store,
        refresh=provider,
        inspect=inspect or provider,
        receipts=selected_receipts,
        global_config=lambda: global_raw,
    )
    return manager, selected_store, selected_receipts


async def enable_from_preview(
    manager: MCPManager,
    project_scope: ProjectScope,
) -> dict[str, object]:
    preview = manager.preview(project_scope, CONFIG)
    return await manager.enable(
        project_scope,
        CONFIG,
        confirmed=True,
        preview_token=preview["preview_token"],
    )


@pytest.mark.asyncio
async def test_enable_requires_literal_true_and_one_use_bound_preview(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    manager, store, _receipts = manager_for(project_scope)
    preview = manager.preview(project_scope, CONFIG)

    with pytest.raises(MCPManagerError, match="explicit"):
        await manager.enable(
            project_scope,
            CONFIG,
            confirmed="true",
            preview_token=preview["preview_token"],
        )

    preview = manager.preview(project_scope, CONFIG)
    store.replace_document({"mcpServers": {"other": OTHER}})
    with pytest.raises(MCPStalePreviewError, match="preview"):
        await manager.enable(
            project_scope,
            CONFIG,
            confirmed=True,
            preview_token=preview["preview_token"],
        )


@pytest.mark.asyncio
async def test_enable_preserves_unrelated_and_verifies_exact_focused_tools(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    manager, store, _receipts = manager_for(
        project_scope,
        store=MemoryStore({"mcpServers": {"other": OTHER}}),
    )
    result = await enable_from_preview(manager, project_scope)
    servers = store.document()["mcpServers"]
    assert servers["other"] == OTHER
    entry = servers[SERVER_NAME]
    assert entry["args"][-2:] == [
        "-m",
        "usr.plugins.sem_review_loop.helpers.mcp_launcher",
    ]
    assert entry["disabled_tools"] == [
        "sem_entities",
        "sem_blame",
        "sem_log",
    ]
    assert entry["env"]["SEM_NO_NETWORK"] == "1"
    assert entry["env"]["SEM_NO_TELEMETRY"] == "1"
    assert result["enabled"] is True
    assert manager.status(project_scope)["enabled"] is True


def test_preview_rejects_same_named_global_server(tmp_path: Path) -> None:
    project_scope = scope(tmp_path)
    manager, _store, _receipts = manager_for(
        project_scope,
        global_raw='{"mcpServers":{"sem-review-loop":{"command":"other"}}}',
    )
    with pytest.raises(MCPConflictError, match="global"):
        manager.preview(project_scope, CONFIG)


def test_preview_rejects_project_config_corruption(tmp_path: Path) -> None:
    project_scope = scope(tmp_path)
    manager, _store, _receipts = manager_for(
        project_scope,
        store=MemoryStore(raw=b'{"mcpServers":', exists=True),
    )
    with pytest.raises(MCPManagerError, match="valid JSON"):
        manager.preview(project_scope, CONFIG)


def test_verification_requires_complete_native_inventory_and_exact_states() -> None:
    with pytest.raises(MCPVerificationError, match="six-tool"):
        MCPManager._verify_tools(FakeMCPConfig(native=set()))

    class NoInventory:
        @staticmethod
        def get_tools() -> list[dict[str, object]]:
            return FakeMCPConfig().get_tools()

    with pytest.raises(MCPVerificationError, match="unavailable"):
        MCPManager._verify_tools(NoInventory())

    with pytest.raises(MCPVerificationError, match="three-tool"):
        MCPManager._verify_tools(
            FakeMCPConfig(
                disabled={"sem_entities", "sem_blame"},
            )
        )


@pytest.mark.asyncio
async def test_failed_verification_restores_exact_original_bytes(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    original = b'{ "mcpServers": {"other": {"command": "keep"}} }\n'
    manager, store, receipts = manager_for(
        project_scope,
        store=MemoryStore(raw=original),
        refresh=lambda _name: FakeMCPConfig(enabled=set()),
    )
    preview = manager.preview(project_scope, CONFIG)
    with pytest.raises(MCPManagerError, match="missing"):
        await manager.enable(
            project_scope,
            CONFIG,
            confirmed=True,
            preview_token=preview["preview_token"],
        )
    assert store.raw == original
    assert receipts.values == {}


@pytest.mark.asyncio
async def test_enable_rollback_preserves_unrelated_concurrent_edit(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    store = MemoryStore({"mcpServers": {"other": OTHER}})
    calls = 0

    def refresh(_name: str) -> FakeMCPConfig:
        nonlocal calls
        calls += 1
        if calls == 1:
            current = store.document()
            current["mcpServers"]["concurrent"] = {"command": "new"}
            store.replace_document(current)
            raise RuntimeError("refresh failed")
        return FakeMCPConfig()

    manager, _store, _receipts = manager_for(
        project_scope,
        store=store,
        refresh=refresh,
    )
    preview = manager.preview(project_scope, CONFIG)
    with pytest.raises(RuntimeError, match="refresh failed"):
        await manager.enable(
            project_scope,
            CONFIG,
            confirmed=True,
            preview_token=preview["preview_token"],
        )
    servers = store.document()["mcpServers"]
    assert servers["other"] == OTHER
    assert servers["concurrent"] == {"command": "new"}
    assert SERVER_NAME not in servers


@pytest.mark.asyncio
async def test_reenable_rollback_restores_prior_entry_receipt_and_external_edit(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    store = MemoryStore({"mcpServers": {"other": OTHER}})
    calls = 0

    def refresh(_name: str) -> FakeMCPConfig:
        nonlocal calls
        calls += 1
        if calls == 2:
            current = store.document()
            current["mcpServers"]["concurrent"] = {"command": "new"}
            store.replace_document(current)
            raise RuntimeError("re-enable refresh failed")
        return FakeMCPConfig()

    manager, _store, receipts = manager_for(
        project_scope,
        store=store,
        refresh=refresh,
    )
    await enable_from_preview(manager, project_scope)
    prior_raw = store.raw
    prior_entry = dict(store.document()["mcpServers"][SERVER_NAME])
    prior_receipt = receipts.get(project_scope.project_name)
    assert prior_receipt is not None
    updated_config = parse_config(
        {"custom_sem_binary": str(tmp_path / "sem-v2")}
    )
    preview = manager.preview(project_scope, updated_config)

    with pytest.raises(RuntimeError, match="re-enable refresh failed"):
        await manager.enable(
            project_scope,
            updated_config,
            confirmed=True,
            preview_token=preview["preview_token"],
        )

    servers = store.document()["mcpServers"]
    assert servers[SERVER_NAME] == prior_entry
    assert servers["other"] == OTHER
    assert servers["concurrent"] == {"command": "new"}
    assert receipts.get(project_scope.project_name) == prior_receipt
    assert calls == 3
    assert store.raw != prior_raw


@pytest.mark.asyncio
async def test_enable_rechecks_project_config_after_successful_refresh(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    store = MemoryStore({"mcpServers": {"other": OTHER}})

    def refresh(_name: str) -> FakeMCPConfig:
        current = store.document()
        current["mcpServers"]["concurrent"] = {"command": "new"}
        store.replace_document(current)
        return FakeMCPConfig()

    manager, _store, receipts = manager_for(
        project_scope,
        store=store,
        refresh=refresh,
    )
    preview = manager.preview(project_scope, CONFIG)
    with pytest.raises(MCPConcurrentModificationError, match="refresh"):
        await manager.enable(
            project_scope,
            CONFIG,
            confirmed=True,
            preview_token=preview["preview_token"],
        )

    servers = store.document()["mcpServers"]
    assert servers["other"] == OTHER
    assert servers["concurrent"] == {"command": "new"}
    assert SERVER_NAME not in servers
    assert receipts.get(project_scope.project_name) is None


@pytest.mark.asyncio
async def test_cancelled_enable_completes_rollback_before_propagating(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    original = b'{"mcpServers":{"other":{"command":"keep"}}}\n'
    store = MemoryStore(raw=original)
    started = threading.Event()
    calls = 0

    def refresh(_name: str) -> FakeMCPConfig:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            time.sleep(0.2)
        return FakeMCPConfig()

    manager, _store, receipts = manager_for(
        project_scope,
        store=store,
        refresh=refresh,
    )
    preview = manager.preview(project_scope, CONFIG)
    task = asyncio.create_task(
        manager.enable(
            project_scope,
            CONFIG,
            confirmed=True,
            preview_token=preview["preview_token"],
        )
    )
    await asyncio.to_thread(started.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.raw == original
    assert receipts.values == {}


@pytest.mark.asyncio
async def test_cancelled_enable_waits_for_late_refresh_then_reapplies_old_state(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    original = b'{"mcpServers":{"other":{"command":"keep"}}}\n'
    store = MemoryStore(raw=original)
    first_started = threading.Event()
    release_first = threading.Event()
    applied_runtime_states: list[bool] = []
    calls = 0

    def refresh(_name: str) -> FakeMCPConfig:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            assert release_first.wait(2)
        applied_runtime_states.append(
            SERVER_NAME in store.document()["mcpServers"]
        )
        return FakeMCPConfig()

    manager, _store, receipts = manager_for(
        project_scope,
        store=store,
        refresh=refresh,
    )
    preview = manager.preview(project_scope, CONFIG)
    task = asyncio.create_task(
        manager.enable(
            project_scope,
            CONFIG,
            confirmed=True,
            preview_token=preview["preview_token"],
        )
    )
    assert await asyncio.to_thread(first_started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.05)
    assert task.done() is False
    assert SERVER_NAME in store.document()["mcpServers"]

    release_first.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert applied_runtime_states == [True, False]
    assert store.raw == original
    assert receipts.values == {}


@pytest.mark.asyncio
async def test_disable_removes_only_exact_entry_and_preserves_others(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    manager, store, _receipts = manager_for(
        project_scope,
        store=MemoryStore({"mcpServers": {"other": OTHER}}),
    )
    await enable_from_preview(manager, project_scope)
    await manager.disable(project_scope)
    assert store.document()["mcpServers"] == {"other": OTHER}


@pytest.mark.asyncio
async def test_disable_preserves_drifted_entry(tmp_path: Path) -> None:
    project_scope = scope(tmp_path)
    manager, store, receipts = manager_for(project_scope)
    await enable_from_preview(manager, project_scope)
    document = store.document()
    document["mcpServers"][SERVER_NAME]["args"].append("--changed")
    store.replace_document(document)
    swaps = store.swaps
    result = await manager.disable(project_scope)
    assert result["drifted"] is True
    assert store.swaps == swaps
    assert receipts.get(project_scope.project_name) is not None


@pytest.mark.asyncio
async def test_disable_refresh_failure_rolls_back_exact_state(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    fail = False
    failed_once = False

    def refresh(_name: str) -> FakeMCPConfig:
        nonlocal failed_once
        if fail and not failed_once:
            failed_once = True
            raise RuntimeError("disable refresh failed")
        return FakeMCPConfig()

    manager, store, receipts = manager_for(
        project_scope,
        refresh=refresh,
    )
    await enable_from_preview(manager, project_scope)
    before = store.raw
    fail = True
    with pytest.raises(RuntimeError, match="disable refresh failed"):
        await manager.disable(project_scope)
    assert store.raw == before
    assert receipts.get(project_scope.project_name).state == "enabled"


@pytest.mark.asyncio
async def test_cancelled_disable_waits_for_late_refresh_then_reapplies_old_state(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    manager, store, receipts = manager_for(project_scope)
    await enable_from_preview(manager, project_scope)
    before = store.raw
    first_started = threading.Event()
    release_first = threading.Event()
    applied_runtime_states: list[bool] = []
    calls = 0

    def refresh(_name: str) -> FakeMCPConfig:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            assert release_first.wait(2)
        applied_runtime_states.append(
            SERVER_NAME in store.document()["mcpServers"]
        )
        return FakeMCPConfig()

    manager.refresh = refresh
    task = asyncio.create_task(manager.disable(project_scope))
    assert await asyncio.to_thread(first_started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.05)
    assert task.done() is False
    assert SERVER_NAME not in store.document()["mcpServers"]

    release_first.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert applied_runtime_states == [False, True]
    assert store.raw == before
    assert receipts.get(project_scope.project_name).state == "enabled"


@pytest.mark.asyncio
async def test_readiness_uses_verified_cache_without_restarting(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    calls = 0

    def refresh(_name: str) -> FakeMCPConfig:
        nonlocal calls
        calls += 1
        return FakeMCPConfig()

    manager, _store, _receipts = manager_for(
        project_scope,
        refresh=refresh,
    )
    await enable_from_preview(manager, project_scope)
    assert calls == 1
    assert (await manager.readiness(project_scope))["enabled"] is True
    assert (await manager.readiness(project_scope))["enabled"] is True
    assert calls == 1


@pytest.mark.asyncio
async def test_late_global_conflict_invalidates_status_and_readiness(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    global_state = {"raw": '{"mcpServers": {}}'}
    registry = ReviewRegistry()
    store = MemoryStore()
    receipts = MemoryReceipts()
    manager = MCPManager(
        registry=registry,
        store=store,
        refresh=lambda _name: FakeMCPConfig(),
        inspect=lambda _name: FakeMCPConfig(),
        receipts=receipts,
        global_config=lambda: global_state["raw"],
    )
    await enable_from_preview(manager, project_scope)
    assert manager.status(project_scope)["enabled"] is True

    global_state["raw"] = json.dumps(
        {
            "mcpServers": {
                "sem-review-loop": {"command": "unrelated"},
            }
        }
    )
    status = manager.status(project_scope)
    assert status["conflict"] is True
    assert status["enabled"] is False
    assert status["tools"] == []
    assert project_scope.project_id not in manager._readiness
    assert registry.mcp_enabled(project_scope) is False

    readiness = await manager.readiness(project_scope)
    assert readiness["conflict"] is True
    assert readiness["enabled"] is False
    assert readiness["tools"] == []
    assert project_scope.project_id not in manager._readiness
    assert registry.mcp_enabled(project_scope) is False


@pytest.mark.asyncio
async def test_global_conflict_added_by_enable_refresh_rolls_back(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    original = b'{ "mcpServers": {"other": {"command": "keep"}} }\n'
    global_state = {"raw": '{"mcpServers": {}}'}
    store = MemoryStore(raw=original)

    class RecordingReceipts(MemoryReceipts):
        def __init__(self) -> None:
            super().__init__()
            self.states: list[str] = []

        def put(self, receipt: MCPReceipt) -> None:
            self.states.append(receipt.state)
            super().put(receipt)

    receipts = RecordingReceipts()
    registry = ReviewRegistry()
    refreshes = 0

    def refresh(_name: str) -> FakeMCPConfig:
        nonlocal refreshes
        refreshes += 1
        if refreshes == 1:
            global_state["raw"] = json.dumps(
                {
                    "mcpServers": {
                        "sem-review-loop": {"command": "unrelated"},
                    }
                }
            )
        return FakeMCPConfig()

    manager = MCPManager(
        registry=registry,
        store=store,
        refresh=refresh,
        inspect=refresh,
        receipts=receipts,
        global_config=lambda: global_state["raw"],
    )
    preview = manager.preview(project_scope, CONFIG)

    with pytest.raises(MCPConflictError, match="global"):
        await manager.enable(
            project_scope,
            CONFIG,
            confirmed=True,
            preview_token=preview["preview_token"],
        )

    assert store.raw == original
    assert receipts.get(project_scope.project_name) is None
    assert "enabled" not in receipts.states
    assert refreshes == 2
    assert "sem-review-loop" in global_state["raw"]
    assert registry.mcp_enabled(project_scope) is False


@pytest.mark.asyncio
async def test_conflict_added_during_readiness_cannot_be_overridden(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    global_state = {"raw": '{"mcpServers": {}}'}
    enabled, store, receipts = manager_for(project_scope)
    await enable_from_preview(enabled, project_scope)

    def inspect(_name: str) -> FakeMCPConfig:
        global_state["raw"] = json.dumps(
            {
                "mcpServers": {
                    "sem_review_loop": {"command": "unrelated"},
                }
            }
        )
        return FakeMCPConfig()

    registry = ReviewRegistry()
    recovered = MCPManager(
        registry=registry,
        store=store,
        refresh=lambda _name: FakeMCPConfig(),
        inspect=inspect,
        receipts=receipts,
        global_config=lambda: global_state["raw"],
    )
    result = await recovered.readiness(project_scope)
    assert result["conflict"] is True
    assert result["enabled"] is False
    assert result["tools"] == []
    assert project_scope.project_id not in recovered._readiness
    assert registry.mcp_enabled(project_scope) is False


@pytest.mark.asyncio
async def test_verifying_receipt_recovers_without_force_refresh(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    manager, store, receipts = manager_for(project_scope)
    await enable_from_preview(manager, project_scope)
    receipt = receipts.get(project_scope.project_name)
    assert receipt is not None
    receipts.put(replace(receipt, state="verifying"))
    inspections = 0

    def inspect(_name: str) -> FakeMCPConfig:
        nonlocal inspections
        inspections += 1
        return FakeMCPConfig()

    recovered, _store, _receipts = manager_for(
        project_scope,
        store=store,
        receipts=receipts,
        inspect=inspect,
    )
    result = await recovered.readiness(project_scope)
    assert result["enabled"] is True
    assert receipts.get(project_scope.project_name).state == "enabled"
    assert inspections == 1


@pytest.mark.parametrize(
    "boundary",
    ["receipt-written", "cas-written", "verification-complete"],
)
@pytest.mark.parametrize("external_edit", ["none", "unrelated", "managed"])
@pytest.mark.asyncio
async def test_reenable_crash_journal_recovers_each_boundary_without_data_loss(
    tmp_path: Path,
    boundary: str,
    external_edit: str,
) -> None:
    project_scope = scope(tmp_path)
    store = MemoryStore(
        raw=b'{ "mcpServers": {"other": {"command": "keep"}} }\n'
    )
    receipts = MemoryReceipts()
    manager, _store, _receipts = manager_for(
        project_scope,
        store=store,
        receipts=receipts,
    )
    await enable_from_preview(manager, project_scope)
    prior_receipt = receipts.get(project_scope.project_name)
    assert prior_receipt is not None
    prior_entry = dict(store.document()["mcpServers"][SERVER_NAME])
    updated_config = parse_config(
        {"custom_sem_binary": str(tmp_path / "sem-v2")}
    )
    target_entry = manager._entry(project_scope, updated_config)
    captured: dict[str, object] = {}

    def capture() -> None:
        if captured:
            return
        captured["raw"] = store.raw
        captured["exists"] = store.exists
        captured["receipt"] = receipts.get(project_scope.project_name)

    if boundary == "receipt-written":
        receipts.after_put = (
            lambda receipt: capture()
            if receipt.state == "enabling"
            else None
        )
    elif boundary == "cas-written":
        store.after_swap = capture
    else:
        receipts.before_put = (
            lambda receipt: capture()
            if receipt.state == "enabled"
            else None
        )

    preview = manager.preview(project_scope, updated_config)
    result = await manager.enable(
        project_scope,
        updated_config,
        confirmed=True,
        preview_token=preview["preview_token"],
    )
    assert result["enabled"] is True
    captured_receipt = captured["receipt"]
    assert isinstance(captured_receipt, MCPReceipt)
    assert captured_receipt.previous_receipt == prior_receipt
    assert captured_receipt.state == (
        "enabling"
        if boundary in {"receipt-written", "cas-written"}
        else "verifying"
    )

    crashed_store = MemoryStore(
        raw=captured["raw"],
        exists=bool(captured["exists"]),
    )
    crashed_receipts = MemoryReceipts()
    crashed_receipts.put(captured_receipt)
    expected_before_recovery = crashed_store.raw
    if external_edit != "none":
        external_document = crashed_store.document()
        if external_edit == "unrelated":
            external_document["mcpServers"]["external"] = {
                "command": "preserve"
            }
        else:
            external_document["mcpServers"][SERVER_NAME]["args"].append(
                "--external-edit"
            )
        crashed_store.replace_document(external_document)
        expected_before_recovery = crashed_store.raw

    recovered, _store, _receipts = manager_for(
        project_scope,
        store=crashed_store,
        receipts=crashed_receipts,
    )
    status = await recovered.readiness(project_scope)

    if external_edit == "managed":
        assert status["enabled"] is False
        assert status["drifted"] is True
        assert crashed_store.raw == expected_before_recovery
        assert (
            crashed_receipts.get(project_scope.project_name)
            == captured_receipt
        )
        return

    assert status["enabled"] is True
    final_receipt = crashed_receipts.get(project_scope.project_name)
    assert final_receipt is not None
    assert final_receipt.state == "enabled"
    assert final_receipt.previous_receipt is None
    final_servers = crashed_store.document()["mcpServers"]
    if boundary == "receipt-written":
        assert final_receipt == prior_receipt
        assert final_servers[SERVER_NAME] == prior_entry
    else:
        assert final_receipt.entry_hash == canonical_hash(target_entry)
        assert final_servers[SERVER_NAME] == target_entry
    if external_edit == "unrelated":
        assert final_servers["external"] == {"command": "preserve"}
    else:
        assert crashed_store.raw == expected_before_recovery


@pytest.mark.asyncio
async def test_disabling_receipt_with_absent_entry_finalizes_recovery(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    manager, store, receipts = manager_for(project_scope)
    await enable_from_preview(manager, project_scope)
    receipt = receipts.get(project_scope.project_name)
    assert receipt is not None
    receipts.put(replace(receipt, state="disabling"))
    document = store.document()
    del document["mcpServers"][SERVER_NAME]
    store.replace_document(document)

    recovered, _store, _receipts = manager_for(
        project_scope,
        store=store,
        receipts=receipts,
    )
    result = await recovered.readiness(project_scope)
    assert result["enabled"] is False
    assert result["armed"] is False
    assert receipts.get(project_scope.project_name) is None


@pytest.mark.parametrize("state", ["enabling", "verifying"])
@pytest.mark.parametrize("all_managed", [False, True])
@pytest.mark.asyncio
async def test_absent_interrupted_enable_recovers_without_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    all_managed: bool,
) -> None:
    project_scope = scope(tmp_path)
    manager, store, receipts = manager_for(project_scope)
    await enable_from_preview(manager, project_scope)
    receipt = receipts.get(project_scope.project_name)
    assert receipt is not None
    receipts.put(replace(receipt, state=state))
    document = store.document()
    del document["mcpServers"][SERVER_NAME]
    document["mcpServers"]["concurrent"] = {"command": "preserve"}
    store.replace_document(document)
    swaps_before_recovery = store.swaps

    if all_managed:
        monkeypatch.setattr(
            "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_project_folder",
            lambda _name: str(project_scope.project_root),
        )
        assert await manager.disable_all_managed() == []
    else:
        result = await manager.disable(project_scope)
        assert result == {
            "configured": False,
            "armed": False,
            "enabled": False,
            "drifted": False,
            "conflict": False,
        }

    assert receipts.get(project_scope.project_name) is None
    assert store.swaps == swaps_before_recovery
    assert store.document()["mcpServers"] == {
        "concurrent": {"command": "preserve"}
    }


@pytest.mark.asyncio
async def test_same_name_recreated_project_is_drift(
    tmp_path: Path,
) -> None:
    original_scope = scope(tmp_path, "original")
    manager, _store, receipts = manager_for(original_scope)
    await enable_from_preview(manager, original_scope)
    receipt = receipts.get(original_scope.project_name)
    assert receipt is not None

    recreated_root = tmp_path / "recreated"
    recreated_root.mkdir()
    recreated = ProjectScope(
        context_id="ctx2",
        project_name=original_scope.project_name,
        project_id=original_scope.project_id,
        project_root=recreated_root,
        watched_root=recreated_root,
        watched_relative=".",
    )
    result = manager.status(recreated)
    assert result["drifted"] is True
    assert "different project identity" in str(result["error"])


@pytest.mark.asyncio
async def test_disable_all_returns_only_true_entry_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_scope = scope(tmp_path)
    manager, store, _receipts = manager_for(project_scope)
    await enable_from_preview(manager, project_scope)
    document = store.document()
    document["mcpServers"][SERVER_NAME]["args"].append("--user-edit")
    store.replace_document(document)
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_project_folder",
        lambda _name: str(project_scope.project_root),
    )
    assert await manager.disable_all_managed() == [
        project_scope.project_name
    ]


@pytest.mark.asyncio
async def test_disable_all_propagates_unexpected_refresh_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_scope = scope(tmp_path)
    manager, _store, _receipts = manager_for(project_scope)
    await enable_from_preview(manager, project_scope)

    def fail(_name: str) -> FakeMCPConfig:
        raise RuntimeError("refresh transport failed")

    manager.refresh = fail
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_project_folder",
        lambda _name: str(project_scope.project_root),
    )
    with pytest.raises(MCPManagerError, match="rollback"):
        await manager.disable_all_managed()


def test_invalid_receipt_bounds_fail_closed(tmp_path: Path) -> None:
    project_scope = scope(tmp_path)
    receipts = MemoryReceipts()
    receipts.values[project_scope.project_name] = MCPReceipt(
        project_name=project_scope.project_name,
        project_id="../escape",
        entry_hash="0" * 64,
        project_root_identity="1" * 64,
    )
    manager, _store, _receipts = manager_for(
        project_scope,
        receipts=receipts,
    )
    with pytest.raises(MCPManagerError, match="project identity"):
        manager.status(project_scope)


def test_receipt_store_round_trips_reenable_journal_and_migrates_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.DATA_ROOT",
        tmp_path,
    )
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.ensure_data_dirs",
        lambda: tmp_path.mkdir(parents=True, exist_ok=True),
    )
    path = tmp_path / "mcp-receipts.json"
    store = ReceiptStore(path)
    prior = MCPReceipt(
        project_name="project",
        project_id="project-id",
        entry_hash="1" * 64,
        project_root_identity="2" * 64,
        state="enabled",
        config_before_hash="3" * 64,
        config_after_hash="4" * 64,
    )
    pending = MCPReceipt(
        project_name="project",
        project_id="project-id",
        entry_hash="5" * 64,
        project_root_identity="2" * 64,
        state="verifying",
        config_before_hash="4" * 64,
        config_after_hash="6" * 64,
        previous_receipt=prior,
    )
    store.put(pending)
    persisted = json.loads(path.read_text())
    assert persisted["schema_version"] == 2
    assert persisted["receipts"]["project"]["previous_receipt"] is not None
    assert ReceiptStore(path).get("project") == pending

    legacy = {
        "schema_version": 1,
        "receipts": {
            "project": {
                key: value
                for key, value in ReceiptStore._encode(prior).items()
                if key != "previous_receipt"
            }
        },
    }
    path.write_text(json.dumps(legacy))
    migrated = ReceiptStore(path)
    assert migrated.get("project") == prior
    migrated.put(prior)
    assert json.loads(path.read_text())["schema_version"] == 2


def test_direct_store_detects_cas_and_permission_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_parent = tmp_path / "projects"
    root = projects_parent / "project"
    metadata = root / ".a0proj"
    metadata.mkdir(parents=True)
    config_path = metadata / "mcp_servers.json"
    config_path.write_bytes(b'{"mcpServers":{}}\n')
    project_scope = make_scope("ctx", "project", root, ".")
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_projects_parent_folder",
        lambda: str(projects_parent),
    )
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_project_folder",
        lambda _name: str(root),
    )
    store = DirectProjectMCPStore()
    snapshot = store.read(project_scope)
    config_path.write_bytes(b'{"mcpServers":{"external":{}}}\n')
    with pytest.raises(MCPConcurrentModificationError):
        store.compare_and_swap(project_scope, snapshot, b"{}\n")

    real_open = os.open

    def denied(path: object, flags: int, *args: Any, **kwargs: Any) -> int:
        if (
            str(path) == "mcp_servers.json"
            and kwargs.get("dir_fd") is not None
        ):
            raise PermissionError("denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.os.open",
        denied,
    )
    with pytest.raises(MCPManagerError, match="Unable to read"):
        store.read(project_scope)


def test_direct_store_rejects_final_symlink_without_nofollow_and_open_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_parent = tmp_path / "projects"
    root = projects_parent / "project"
    metadata = root / ".a0proj"
    metadata.mkdir(parents=True)
    config_path = metadata / "mcp_servers.json"
    target = tmp_path / "attacker.json"
    target.write_bytes(b'{"mcpServers":{"attacker":{}}}\n')
    project_scope = make_scope("ctx", "project", root, ".")
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_projects_parent_folder",
        lambda: str(projects_parent),
    )
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_project_folder",
        lambda _name: str(root),
    )
    monkeypatch.setattr(os, "O_NOFOLLOW", 0, raising=False)
    store = DirectProjectMCPStore()

    try:
        config_path.symlink_to(target)
    except OSError:
        pytest.skip("Symlinks are unavailable on this platform.")
    with pytest.raises(MCPManagerError, match="symlink"):
        store.read(project_scope)

    config_path.unlink()
    config_path.write_bytes(b'{"mcpServers":{}}\n')
    original_path = metadata / "mcp_servers.original.json"
    real_open = os.open
    swapped = False

    def swap_before_open(
        path: object,
        flags: int,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        nonlocal swapped
        if Path(path) == config_path and not swapped:
            swapped = True
            config_path.replace(original_path)
            config_path.symlink_to(original_path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)
    with pytest.raises(MCPManagerError, match="symlink"):
        store.read(project_scope)


def test_direct_store_parent_swap_during_read_never_accepts_outside_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not DirectProjectMCPStore._supports_secure_dir_fd():
        pytest.skip("This adversarial regression requires secure dir-fd access.")
    projects_parent = tmp_path / "projects"
    root = projects_parent / "project"
    metadata = root / ".a0proj"
    metadata.mkdir(parents=True)
    config_path = metadata / "mcp_servers.json"
    safe_bytes = b'{"mcpServers":{"safe":{}}}\n'
    config_path.write_bytes(safe_bytes)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_config = outside / "mcp_servers.json"
    outside_bytes = b'{"mcpServers":{"outside":{}}}\n'
    outside_config.write_bytes(outside_bytes)
    saved_metadata = root / ".a0proj.saved"
    project_scope = make_scope("ctx", "project", root, ".")
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_projects_parent_folder",
        lambda: str(projects_parent),
    )
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_project_folder",
        lambda _name: str(root),
    )

    real_assert = DirectProjectMCPStore._assert_directory_identity
    checks = 0
    swapped = False

    def swap_after_pin_validation(
        cls: type[DirectProjectMCPStore],
        target: Any,
    ) -> os.stat_result:
        del cls
        nonlocal checks, swapped
        result = real_assert(target)
        checks += 1
        if checks == 2:
            metadata.rename(saved_metadata)
            metadata.symlink_to(outside, target_is_directory=True)
            swapped = True
        return result

    monkeypatch.setattr(
        DirectProjectMCPStore,
        "_assert_directory_identity",
        classmethod(swap_after_pin_validation),
    )
    try:
        with pytest.raises(MCPManagerError, match="metadata directory changed"):
            DirectProjectMCPStore().read(project_scope)
        assert swapped is True
        assert outside_config.read_bytes() == outside_bytes
        assert (saved_metadata / "mcp_servers.json").read_bytes() == safe_bytes
    finally:
        if metadata.is_symlink():
            metadata.unlink()
        if saved_metadata.exists():
            saved_metadata.rename(metadata)


@pytest.mark.parametrize("operation", ["create", "replace", "delete"])
def test_direct_store_parent_swap_cannot_redirect_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    if not DirectProjectMCPStore._supports_secure_dir_fd():
        pytest.skip("This adversarial regression requires secure dir-fd access.")
    projects_parent = tmp_path / "projects"
    root = projects_parent / "project"
    metadata = root / ".a0proj"
    metadata.mkdir(parents=True)
    config_path = metadata / "mcp_servers.json"
    original_bytes = b'{"mcpServers":{"safe":{}}}\n'
    if operation != "create":
        config_path.write_bytes(original_bytes)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_config = outside / "mcp_servers.json"
    outside_bytes = b'{"mcpServers":{"outside":{}}}\n'
    outside_config.write_bytes(outside_bytes)
    saved_metadata = root / ".a0proj.saved"
    project_scope = make_scope("ctx", "project", root, ".")
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_projects_parent_folder",
        lambda: str(projects_parent),
    )
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_project_folder",
        lambda _name: str(root),
    )
    store = DirectProjectMCPStore()
    snapshot = store.read(project_scope)
    swapped = False

    def swap_parent() -> None:
        nonlocal swapped
        metadata.rename(saved_metadata)
        metadata.symlink_to(outside, target_is_directory=True)
        swapped = True

    if operation == "delete":
        real_boundary = DirectProjectMCPStore._capture_delete_pinned

        def swap_before_delete(
            cls: type[DirectProjectMCPStore],
            pinned: Any,
            captured_name: str,
        ) -> None:
            del cls
            swap_parent()
            real_boundary(pinned, captured_name)

        monkeypatch.setattr(
            DirectProjectMCPStore,
            "_capture_delete_pinned",
            classmethod(swap_before_delete),
        )
        replacement = None
    elif operation == "create":
        real_boundary = DirectProjectMCPStore._install_if_missing_pinned

        def swap_before_create(
            cls: type[DirectProjectMCPStore],
            pinned: Any,
            prepared_name: str,
        ) -> None:
            del cls
            swap_parent()
            real_boundary(pinned, prepared_name)

        monkeypatch.setattr(
            DirectProjectMCPStore,
            "_install_if_missing_pinned",
            classmethod(swap_before_create),
        )
        replacement = b'{"mcpServers":{"replacement":{}}}\n'
    else:
        real_boundary = DirectProjectMCPStore._capture_replace_pinned

        def swap_before_replace(
            cls: type[DirectProjectMCPStore],
            pinned: Any,
            prepared_name: str,
        ) -> str:
            del cls
            swap_parent()
            return real_boundary(pinned, prepared_name)

        monkeypatch.setattr(
            DirectProjectMCPStore,
            "_capture_replace_pinned",
            classmethod(swap_before_replace),
        )
        replacement = b'{"mcpServers":{"replacement":{}}}\n'

    try:
        with pytest.raises(MCPManagerError, match="metadata directory changed"):
            store.compare_and_swap(
                project_scope,
                snapshot,
                replacement,
            )
        assert swapped is True
        assert outside_config.read_bytes() == outside_bytes
    finally:
        if metadata.is_symlink():
            metadata.unlink()
        if saved_metadata.exists():
            saved_metadata.rename(metadata)


def test_direct_store_cas_rejects_replaced_metadata_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_parent = tmp_path / "projects"
    root = projects_parent / "project"
    metadata = root / ".a0proj"
    metadata.mkdir(parents=True)
    config_path = metadata / "mcp_servers.json"
    original = b'{"mcpServers":{"safe":{}}}\n'
    config_path.write_bytes(original)
    project_scope = make_scope("ctx", "project", root, ".")
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_projects_parent_folder",
        lambda: str(projects_parent),
    )
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_project_folder",
        lambda _name: str(root),
    )
    store = DirectProjectMCPStore()
    snapshot = store.read(project_scope)

    displaced = root / ".a0proj.displaced"
    metadata.rename(displaced)
    metadata.mkdir()
    replacement_directory_bytes = b'{"mcpServers":{"new-owner":{}}}\n'
    (metadata / "mcp_servers.json").write_bytes(replacement_directory_bytes)

    with pytest.raises(
        MCPConcurrentModificationError,
        match="metadata directory changed",
    ):
        store.compare_and_swap(
            project_scope,
            snapshot,
            b'{"mcpServers":{"replacement":{}}}\n',
        )
    assert (metadata / "mcp_servers.json").read_bytes() == (
        replacement_directory_bytes
    )


def test_direct_store_fails_before_mutation_without_secure_relative_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_parent = tmp_path / "projects"
    root = projects_parent / "project"
    metadata = root / ".a0proj"
    metadata.mkdir(parents=True)
    config_path = metadata / "mcp_servers.json"
    original = b'{"mcpServers":{"safe":{}}}\n'
    config_path.write_bytes(original)
    project_scope = make_scope("ctx", "project", root, ".")
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_projects_parent_folder",
        lambda: str(projects_parent),
    )
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_project_folder",
        lambda _name: str(root),
    )
    store = DirectProjectMCPStore()
    snapshot = store.read(project_scope)
    monkeypatch.setattr(os, "O_NOFOLLOW", 0, raising=False)

    with pytest.raises(MCPManagerError, match="unavailable"):
        store.compare_and_swap(
            project_scope,
            snapshot,
            b'{"mcpServers":{"replacement":{}}}\n',
        )
    assert config_path.read_bytes() == original


def test_direct_store_cas_create_replace_delete_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_parent = tmp_path / "projects"
    root = projects_parent / "project"
    metadata = root / ".a0proj"
    metadata.mkdir(parents=True)
    config_path = metadata / "mcp_servers.json"
    project_scope = make_scope("ctx", "project", root, ".")
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_projects_parent_folder",
        lambda: str(projects_parent),
    )
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_project_folder",
        lambda _name: str(root),
    )
    store = DirectProjectMCPStore()

    missing = store.read(project_scope)
    created_bytes = b'{"mcpServers":{"created":{}}}\n'
    created = store.compare_and_swap(
        project_scope,
        missing,
        created_bytes,
    )
    assert created.exists is True
    assert created.raw == created_bytes
    assert config_path.read_bytes() == created_bytes

    replacement_bytes = b'{"mcpServers":{"replacement":{}}}\n'
    replaced = store.compare_and_swap(
        project_scope,
        created,
        replacement_bytes,
    )
    assert replaced.exists is True
    assert replaced.raw == replacement_bytes
    assert config_path.read_bytes() == replacement_bytes

    deleted = store.compare_and_swap(project_scope, replaced, None)
    assert deleted.exists is False
    assert not config_path.exists()
    assert not list(metadata.glob(".sem-review-mcp-*"))


@pytest.mark.parametrize("operation", ["create", "replace", "delete"])
def test_direct_store_unsupported_atomic_primitive_fails_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    if not DirectProjectMCPStore._supports_secure_dir_fd():
        pytest.skip("This primitive regression requires secure dir-fd access.")
    projects_parent = tmp_path / "projects"
    root = projects_parent / "project"
    metadata = root / ".a0proj"
    metadata.mkdir(parents=True)
    config_path = metadata / "mcp_servers.json"
    original = b'{"mcpServers":{"original":{}}}\n'
    if operation != "create":
        config_path.write_bytes(original)
    project_scope = make_scope("ctx", "project", root, ".")
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_projects_parent_folder",
        lambda: str(projects_parent),
    )
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_project_folder",
        lambda _name: str(root),
    )
    store = DirectProjectMCPStore()
    snapshot = store.read(project_scope)

    def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise MCPManagerError(
            "Atomic filesystem compare-and-swap is unavailable."
        )

    monkeypatch.setattr(
        DirectProjectMCPStore,
        "_rename_with_flags_relative",
        classmethod(unavailable),
    )
    replacement = (
        None
        if operation == "delete"
        else b'{"mcpServers":{"plugin-replacement":{}}}\n'
    )

    with pytest.raises(MCPManagerError, match="unavailable"):
        store.compare_and_swap(project_scope, snapshot, replacement)

    if operation == "create":
        assert not config_path.exists()
    else:
        assert config_path.read_bytes() == original
    assert not list(metadata.glob(".sem-review-mcp-*"))


@pytest.mark.parametrize("operation", ["create", "replace", "delete"])
def test_direct_store_boundary_edit_wins_exact_byte_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    projects_parent = tmp_path / "projects"
    root = projects_parent / "project"
    metadata = root / ".a0proj"
    metadata.mkdir(parents=True)
    config_path = metadata / "mcp_servers.json"
    original = b'{"mcpServers":{"original":{}}}\n'
    external = b'{"mcpServers":{"external-boundary-edit":{}}}\n'
    plugin = b'{"mcpServers":{"plugin-replacement":{}}}\n'
    if operation != "create":
        config_path.write_bytes(original)
    project_scope = make_scope("ctx", "project", root, ".")
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_projects_parent_folder",
        lambda: str(projects_parent),
    )
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_project_folder",
        lambda _name: str(root),
    )
    store = DirectProjectMCPStore()
    snapshot = store.read(project_scope)

    if operation == "create":
        real_boundary = DirectProjectMCPStore._install_if_missing_pinned

        def inject_create(
            cls: type[DirectProjectMCPStore],
            pinned: Any,
            prepared_name: str,
        ) -> None:
            del cls
            config_path.write_bytes(external)
            real_boundary(pinned, prepared_name)

        monkeypatch.setattr(
            DirectProjectMCPStore,
            "_install_if_missing_pinned",
            classmethod(inject_create),
        )
        replacement: bytes | None = plugin
    elif operation == "replace":
        real_boundary = DirectProjectMCPStore._capture_replace_pinned
        injected = False

        def inject_replace(
            cls: type[DirectProjectMCPStore],
            pinned: Any,
            prepared_name: str,
        ) -> str:
            del cls
            nonlocal injected
            if not injected:
                config_path.write_bytes(external)
                injected = True
            return real_boundary(pinned, prepared_name)

        monkeypatch.setattr(
            DirectProjectMCPStore,
            "_capture_replace_pinned",
            classmethod(inject_replace),
        )
        replacement = plugin
    else:
        real_boundary = DirectProjectMCPStore._capture_delete_pinned

        def inject_delete(
            cls: type[DirectProjectMCPStore],
            pinned: Any,
            captured_name: str,
        ) -> None:
            del cls
            config_path.write_bytes(external)
            real_boundary(pinned, captured_name)

        monkeypatch.setattr(
            DirectProjectMCPStore,
            "_capture_delete_pinned",
            classmethod(inject_delete),
        )
        replacement = None

    with pytest.raises(
        MCPConcurrentModificationError,
        match="changed",
    ):
        store.compare_and_swap(project_scope, snapshot, replacement)

    assert config_path.read_bytes() == external
    assert config_path.read_bytes() != plugin
    assert not list(metadata.glob(".sem-review-mcp-*"))


@pytest.mark.parametrize("operation", ["replace", "delete"])
def test_direct_store_retains_captured_external_bytes_when_target_changes_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    if not DirectProjectMCPStore._supports_secure_dir_fd():
        pytest.skip("This adversarial regression requires secure dir-fd access.")
    projects_parent = tmp_path / "projects"
    root = projects_parent / "project"
    metadata = root / ".a0proj"
    metadata.mkdir(parents=True)
    config_path = metadata / "mcp_servers.json"
    original = b'{"mcpServers":{"original":{}}}\n'
    external_a = b'{"mcpServers":{"external-a":{}}}\n'
    external_b = b'{"mcpServers":{"external-b":{}}}\n'
    plugin = b'{"mcpServers":{"plugin-replacement":{}}}\n'
    config_path.write_bytes(original)
    project_scope = make_scope("ctx", "project", root, ".")
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_projects_parent_folder",
        lambda: str(projects_parent),
    )
    monkeypatch.setattr(
        "usr.plugins.sem_review_loop.helpers.mcp_manager.projects.get_project_folder",
        lambda _name: str(root),
    )
    store = DirectProjectMCPStore()
    snapshot = store.read(project_scope)

    if operation == "replace":
        real_boundary = DirectProjectMCPStore._capture_replace_pinned

        def inject_two_writers(
            cls: type[DirectProjectMCPStore],
            pinned: Any,
            prepared_name: str,
        ) -> str:
            del cls
            config_path.write_bytes(external_a)
            captured_name = real_boundary(pinned, prepared_name)
            config_path.write_bytes(external_b)
            return captured_name

        monkeypatch.setattr(
            DirectProjectMCPStore,
            "_capture_replace_pinned",
            classmethod(inject_two_writers),
        )
        replacement: bytes | None = plugin
    else:
        real_boundary = DirectProjectMCPStore._capture_delete_pinned

        def inject_two_writers_for_delete(
            cls: type[DirectProjectMCPStore],
            pinned: Any,
            captured_name: str,
        ) -> None:
            del cls
            config_path.write_bytes(external_a)
            real_boundary(pinned, captured_name)
            config_path.write_bytes(external_b)

        monkeypatch.setattr(
            DirectProjectMCPStore,
            "_capture_delete_pinned",
            classmethod(inject_two_writers_for_delete),
        )
        replacement = None

    with pytest.raises(
        MCPRollbackError,
        match="retained.*project metadata",
    ):
        store.compare_and_swap(project_scope, snapshot, replacement)

    assert config_path.read_bytes() == external_b
    retained = list(metadata.glob(".sem-review-mcp-conflict-*.json"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == external_a
    assert not [
        path
        for path in metadata.glob(".sem-review-mcp-*")
        if path not in retained
    ]
    assert all(path.read_bytes() != plugin for path in metadata.iterdir())


def test_atomic_replace_does_not_require_fchmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "mcp_servers.json"
    destination.write_bytes(b"old")
    monkeypatch.delattr(os, "fchmod", raising=False)
    DirectProjectMCPStore._atomic_replace(destination, b"new", 0o600)
    assert destination.read_bytes() == b"new"


@pytest.mark.asyncio
async def test_project_lock_serializes_concurrent_enable_and_stales_second(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def refresh(_name: str) -> FakeMCPConfig:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            release.wait(1)
        return FakeMCPConfig()

    manager, _store, _receipts = manager_for(
        project_scope,
        refresh=refresh,
    )
    first = manager.preview(project_scope, CONFIG)
    second = manager.preview(project_scope, CONFIG)
    task_one = asyncio.create_task(
        manager.enable(
            project_scope,
            CONFIG,
            confirmed=True,
            preview_token=first["preview_token"],
        )
    )
    await asyncio.to_thread(entered.wait, 1)
    task_two = asyncio.create_task(
        manager.enable(
            project_scope,
            CONFIG,
            confirmed=True,
            preview_token=second["preview_token"],
        )
    )
    release.set()
    await task_one
    with pytest.raises(MCPStalePreviewError):
        await task_two


def test_project_gate_serializes_across_event_loops_and_evicts(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    manager, _store, _receipts = manager_for(project_scope)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    order: list[str] = []
    errors: list[BaseException] = []

    async def first() -> None:
        async with manager._project_gate(project_scope.project_id):
            order.append("first-enter")
            first_entered.set()
            released = await asyncio.to_thread(release_first.wait, 2)
            if not released:
                raise TimeoutError("first project gate was not released")
            order.append("first-exit")

    async def second() -> None:
        async with manager._project_gate(project_scope.project_id):
            order.append("second-enter")
            second_entered.set()

    def run(coroutine: Any) -> None:
        try:
            asyncio.run(coroutine)
        except BaseException as exc:
            errors.append(exc)

    first_thread = threading.Thread(target=run, args=(first(),))
    first_thread.start()
    assert first_entered.wait(2)

    second_thread = threading.Thread(target=run, args=(second(),))
    second_thread.start()
    assert not second_entered.wait(0.1)

    release_first.set()
    first_thread.join(2)
    second_thread.join(2)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert order == ["first-enter", "first-exit", "second-enter"]
    assert manager._locks == {}


def test_path_gate_acquire_failure_evicts_registry_entry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mcp_servers.json"
    key = os.path.normcase(str(path))

    class FailingLock:
        def acquire(self) -> bool:
            raise KeyboardInterrupt("injected acquire failure")

        def release(self) -> None:
            raise AssertionError("unacquired lock must not be released")

    state = mcp_manager_module._PathLockState(lock=FailingLock())  # type: ignore[arg-type]
    with mcp_manager_module._PATH_LOCKS_GUARD:
        assert key not in mcp_manager_module._PATH_LOCKS
        mcp_manager_module._PATH_LOCKS[key] = state

    with pytest.raises(KeyboardInterrupt, match="injected acquire failure"):
        with mcp_manager_module._path_gate(path):
            raise AssertionError("gate body must not run")

    assert state.references == 0
    with mcp_manager_module._PATH_LOCKS_GUARD:
        assert key not in mcp_manager_module._PATH_LOCKS


def test_project_gate_waiter_cancellation_error_and_reuse_do_not_leak(
    tmp_path: Path,
) -> None:
    project_scope = scope(tmp_path)
    manager, _store, _receipts = manager_for(project_scope)
    holder_entered = threading.Event()
    release_holder = threading.Event()
    waiter_started = threading.Event()
    waiter_entered = threading.Event()
    waiter_cancelled = threading.Event()
    errors: list[BaseException] = []

    async def holder() -> None:
        async with manager._project_gate(project_scope.project_id):
            holder_entered.set()
            released = await asyncio.to_thread(release_holder.wait, 2)
            if not released:
                raise TimeoutError("project gate holder was not released")

    async def cancel_waiter() -> None:
        async def wait() -> None:
            waiter_started.set()
            async with manager._project_gate(project_scope.project_id):
                waiter_entered.set()

        task = asyncio.create_task(wait())
        while not waiter_started.is_set():
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            waiter_cancelled.set()

    def run(coroutine: Any) -> None:
        try:
            asyncio.run(coroutine)
        except BaseException as exc:
            errors.append(exc)

    holder_thread = threading.Thread(target=run, args=(holder(),))
    holder_thread.start()
    assert holder_entered.wait(2)

    waiter_thread = threading.Thread(target=run, args=(cancel_waiter(),))
    waiter_thread.start()
    waiter_thread.join(2)
    assert not waiter_thread.is_alive()
    assert waiter_cancelled.is_set()
    assert not waiter_entered.is_set()

    release_holder.set()
    holder_thread.join(2)
    assert not holder_thread.is_alive()
    assert errors == []
    assert manager._locks == {}

    owner_entered = threading.Event()
    owner_cancelled = threading.Event()

    async def cancel_owner() -> None:
        async def own() -> None:
            async with manager._project_gate(project_scope.project_id):
                owner_entered.set()
                await asyncio.Future()

        task = asyncio.create_task(own())
        while not owner_entered.is_set():
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            owner_cancelled.set()

    asyncio.run(cancel_owner())
    assert owner_cancelled.is_set()
    assert manager._locks == {}

    async def fail_inside_gate() -> None:
        async with manager._project_gate(project_scope.project_id):
            raise RuntimeError("injected project gate failure")

    with pytest.raises(RuntimeError, match="injected"):
        asyncio.run(fail_inside_gate())
    assert manager._locks == {}

    acquired = False

    async def reuse() -> None:
        nonlocal acquired
        async with manager._project_gate(project_scope.project_id):
            acquired = True

    asyncio.run(reuse())
    assert acquired is True
    assert manager._locks == {}


def test_api_defaults_and_unknown_context_are_fail_closed() -> None:
    assert SemMcp.requires_auth() is True
    assert SemMcp.requires_csrf() is True

    class MissingContext:
        @staticmethod
        def use_context(
            _context_id: str,
            *,
            create_if_not_exists: bool,
        ) -> object:
            assert create_if_not_exists is False
            raise Exception("sensitive internal detail")

    with pytest.raises(APIInputError, match="Unknown Agent Zero context") as exc:
        agent_scope_config(MissingContext(), {"context_id": "missing"})
    assert "sensitive" not in str(exc.value)
