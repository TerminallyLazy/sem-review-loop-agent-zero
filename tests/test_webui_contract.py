from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def run_store_behavior(body: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for WebUI behavior tests"
    store_path = ROOT / "webui" / "sem-review-store.js"
    harness = f"""
import fs from "node:fs";

const source = fs.readFileSync({json.dumps(str(store_path))}, "utf8");
const transformed = source
  .replace(/^import .*;\\s*$/gm, "")
  .replace(/^export const store = createStore\\([^\\n]+\\);\\s*$/m, "");
const pending = [];
const notifications = {{ errors: [], successes: [] }};
const socket = {{
  addHandlers() {{}},
  async on() {{}},
  off() {{}},
}};
const callJsonApi = (path, request) => new Promise((resolve, reject) => {{
  pending.push({{ path, request, resolve, reject }});
}});
const notificationStore = {{
  async frontendError(...args) {{ notifications.errors.push(args); }},
  async frontendSuccess(...args) {{ notifications.successes.push(args); }},
}};
const factory = new Function(
  "createStore",
  "callJsonApi",
  "getContext",
  "getNamespacedClient",
  "notificationStore",
  `${{transformed}}\\nreturn model;`,
);
const model = factory(
  (_name, value) => value,
  callJsonApi,
  () => "",
  () => socket,
  notificationStore,
);

function assert(condition, message) {{
  if (!condition) throw new Error(message);
}}

function take(endpoint, action = null) {{
  const index = pending.findIndex((item) => (
    item.path.endsWith(`/${{endpoint}}`)
    && (action === null || item.request.action === action)
  ));
  assert(index >= 0, `missing pending ${{endpoint}}/${{action || ""}} request`);
  return pending.splice(index, 1)[0];
}}

function payload(projectId, revision, fingerprint) {{
  const snapshot = {{
    request: {{ mode: "working" }},
    revision,
    fingerprint,
    changes: [{{
          entity_id: "entity-1",
          entity_name: "example",
          entity_type: "function",
          file_path: "src/example.py",
          change_type: "modified",
          start_line: 1,
          end_line: 1,
          structural: true,
    }}],
    summary: {{ total: 1 }},
    sem_version: "0.21.0",
    completed_at: "2026-07-29T00:00:00+00:00",
    stale: false,
    error: "",
  }};
  return {{
    ok: true,
    review: {{
      context_id: String(model?.contextId || "ctx-a"),
      project_id: projectId,
      watched_relative: ".",
      revision,
      pending_generation: 0,
      snapshot,
      working: snapshot,
      checkpoint: null,
      repair_cycle: 0,
      mcp_enabled: false,
      error: "",
    }},
    lessons: {{ pending: [], approved: [] }},
  }};
}}
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-"],
        input=harness + "\n" + body,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def run_registrar_behavior(body: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for WebUI behavior tests"
    registrar_path = (
        ROOT
        / "extensions"
        / "webui"
        / "right_canvas_register_surfaces"
        / "register-sem-review.js"
    )
    harness = f"""
import fs from "node:fs";

const source = fs.readFileSync({json.dumps(str(registrar_path))}, "utf8");
const transformed = source
  .replace(/^import .*;\\s*$/gm, "")
  .replace(
    "export default async function registerSemReviewSurface",
    "async function registerSemReviewSurface",
  );
let currentPanel = null;
const observers = [];
class TestMutationObserver {{
  constructor(callback) {{
    this.callback = callback;
    this.disconnected = false;
    observers.push(this);
  }}
  observe() {{}}
  disconnect() {{ this.disconnected = true; }}
}}
const document = {{
  body: {{}},
  querySelector() {{ return currentPanel; }},
}};
const semReviewStore = {{
  async onMount() {{ return true; }},
  async onOpen() {{ return true; }},
  cleanup() {{}},
}};
const factory = new Function(
  "semReviewStore",
  "document",
  "MutationObserver",
  `${{transformed}}\\nreturn registerSemReviewSurface;`,
);
const registerSemReviewSurface = factory(
  semReviewStore,
  document,
  TestMutationObserver,
);
let surface = null;
await registerSemReviewSurface({{
  registerSurface(value) {{ surface = value; }},
}});

function assert(condition, message) {{
  if (!condition) throw new Error(message);
}}
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-"],
        input=harness + "\n" + body,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_surface_is_registered_in_both_extension_lanes() -> None:
    register = read(
        "extensions",
        "webui",
        "right_canvas_register_surfaces",
        "register-sem-review.js",
    )
    alias = read(
        "extensions",
        "webui",
        "surfaces_register",
        "register-sem-review.js",
    )
    assert 'id: "sem_review_loop"' in register
    assert 'title: "Semantic Review"' in register
    assert 'icon: "difference"' in register
    assert "order: 40" in register
    assert (
        'modalPath: "/plugins/sem_review_loop/webui/main.html"'
        in register
    )
    assert (
        "../right_canvas_register_surfaces/register-sem-review.js"
        in alias
    )


def test_surface_close_cancels_delayed_panel_insertion() -> None:
    run_registrar_behavior(
        """
let mountCalls = 0;
let openCalls = 0;
let cleanupCalls = 0;
semReviewStore.onMount = async () => {
  mountCalls += 1;
  return true;
};
semReviewStore.onOpen = async () => {
  openCalls += 1;
  return true;
};
semReviewStore.cleanup = () => { cleanupCalls += 1; };

const opening = surface.open({ context_id: "ctx-delayed" });
await new Promise((resolve) => setImmediate(resolve));
assert(observers.length === 1, "open did not wait for panel insertion");
await surface.close();
currentPanel = { id: "late-panel" };
observers[0].callback();

assert(await opening === false, "closed delayed open did not cancel");
assert(observers[0].disconnected, "close left MutationObserver connected");
assert(mountCalls === 0, "late panel insertion mounted a closed surface");
assert(openCalls === 0, "late panel insertion reopened a closed surface");
assert(cleanupCalls === 1, "close did not clean the store exactly once");
"""
    )


def test_surface_overlapping_open_cannot_resume_older_generation() -> None:
    run_registrar_behavior(
        """
currentPanel = { id: "mounted-panel" };
const mountResolvers = [];
const openedPayloads = [];
semReviewStore.onMount = () => new Promise((resolve) => {
  mountResolvers.push(resolve);
});
semReviewStore.onOpen = async (payload) => {
  openedPayloads.push(payload.context_id);
  return true;
};

const older = surface.open({ context_id: "ctx-old" });
await new Promise((resolve) => setImmediate(resolve));
const newer = surface.open({ context_id: "ctx-new" });
await new Promise((resolve) => setImmediate(resolve));
assert(mountResolvers.length === 2, "overlapping opens did not both reach mount");

mountResolvers[1](true);
assert(await newer === true, "newest open did not complete");
mountResolvers[0](true);
assert(await older === false, "older open resumed after a newer open");
assert(
  openedPayloads.length === 1 && openedPayloads[0] === "ctx-new",
  "older open reached onOpen",
);
"""
    )


def test_panel_uses_store_gate_and_mount_cleanup_contract() -> None:
    panel = read("webui", "panel.html")
    assert '<template x-if="$store.semReviewLoop">' in panel
    assert 'x-create="$store.semReviewLoop.onMount' in panel
    assert 'x-destroy="$store.semReviewLoop.cleanup()"' in panel
    assert "x-html" not in panel
    assert "innerHTML" not in panel


def test_store_uses_shared_api_websocket_and_notifications() -> None:
    store = read("webui", "sem-review-store.js")
    assert 'createStore("semReviewLoop"' in store
    assert 'getNamespacedClient("/ws")' in store
    assert 'addHandlers(["ws_webui"])' in store
    assert 'await reviewSocket.on("sem_review_revision"' in store
    assert 'reviewSocket.off("sem_review_revision"' in store
    assert "envelope?.data" in store
    assert "callJsonApi" in store
    assert "notificationStore" in store
    assert "fetch(" not in store


def test_store_exposes_complete_task_twelve_action_contract() -> None:
    store = read("webui", "sem-review-store.js")
    actions = (
        "resolveContextId",
        "init",
        "onMount",
        "onOpen",
        "cleanup",
        "refreshStatus",
        "refreshDiff",
        "selectEntity",
        "loadDetail",
        "loadContext",
        "loadImpact",
        "previewMcp",
        "enableMcp",
        "disableMcp",
        "cancelReview",
        "loadLessons",
        "approveLesson",
        "discardLesson",
        "deleteLesson",
        "forgetAllLessons",
    )
    selectors = (
        "snapshot",
        "changes",
        "summary",
        "groupedChanges",
        "selectedChange",
        "pendingLessons",
        "approvedLessons",
    )
    for method in (*actions, *selectors):
        assert re.search(rf"\n  (?:async )?{method}\(", store), method
    for endpoint in (
        "sem_status",
        "sem_diff",
        "sem_detail",
        "sem_context",
        "sem_impact",
        "sem_mcp",
        "sem_review",
        "sem_lessons",
    ):
        assert f'apiPath("{endpoint}")' in store


def test_store_guards_lifecycle_stale_status_and_revision_envelopes() -> None:
    store = read("webui", "sem-review-store.js")
    assert "_initialized: false" in store
    assert "_lifecycleSeq: 0" in store
    assert "_requestSeq: 0" in store
    assert "_diffRequestSeq: 0" in store
    assert "_mcpRequestSeq: 0" in store
    assert "const requestSeq = ++this._requestSeq" in store
    assert "requestSeq !== this._requestSeq" in store
    assert "_revisionOff: null" in store
    assert "if (this._revisionOff) {" in store
    assert "_lifecycleIsCurrent(lifecycleSeq, rootToken)" in store
    assert "this._requestSeq += 1" in store
    assert "REVISION_EVENT_FIELDS" in store
    assert "Object.keys(data)" in store
    assert "Number.isSafeInteger(data.revision)" in store
    assert "data.context_id" in store
    assert "data.project_id" in store
    assert "_selectionBindingIsCurrent" in store


def test_delayed_mount_subscription_cannot_restart_after_cleanup() -> None:
    run_store_behavior(
        """
let resolveSubscription;
let offCalls = 0;
socket.on = (_event, _handler) => new Promise((resolve) => {
  resolveSubscription = resolve;
});
socket.off = () => { offCalls += 1; };
const errorsBefore = notifications.errors.length;
const successesBefore = notifications.successes.length;
const root = { id: "semantic-review-root" };

const mounting = model.onMount(root, {
  context_id: "ctx-mount",
  mode: "canvas",
});
await new Promise((resolve) => setImmediate(resolve));
assert(
  typeof resolveSubscription === "function",
  "mount did not reach delayed socket subscription",
);
model.cleanup();
resolveSubscription();

assert(await mounting === false, "cleaned-up mount resumed");
assert(pending.length === 0, "mount issued an API call after cleanup");
assert(model._mounted === false, "mount restored mounted state");
assert(model._root === null, "mount restored a cleaned-up root");
assert(model.payload === null, "mount repopulated payload after cleanup");
assert(model.loading === false && model.busy === false, "mount restored busy state");
assert(offCalls === 1, "late mount subscription was not removed");
assert(
  notifications.errors.length === errorsBefore
  && notifications.successes.length === successesBefore,
  "late mount emitted a notification",
);
"""
    )


def test_delayed_open_subscription_cannot_restart_after_cleanup() -> None:
    run_store_behavior(
        """
let resolveSubscription;
let offCalls = 0;
socket.on = (_event, _handler) => new Promise((resolve) => {
  resolveSubscription = resolve;
});
socket.off = () => { offCalls += 1; };
const errorsBefore = notifications.errors.length;
const successesBefore = notifications.successes.length;

const opening = model.onOpen({
  context_id: "ctx-open",
  entity_id: "entity-1",
});
await new Promise((resolve) => setImmediate(resolve));
assert(
  typeof resolveSubscription === "function",
  "open did not reach delayed socket subscription",
);
model.cleanup();
resolveSubscription();

assert(await opening === false, "cleaned-up open resumed");
assert(pending.length === 0, "open issued an API call after cleanup");
assert(model._mounted === false, "open restored mounted state");
assert(model._root === null, "open restored a cleaned-up root");
assert(model.payload === null, "open repopulated payload after cleanup");
assert(model.selectedEntityId === "", "open selected an entity after cleanup");
assert(model.loading === false && model.busy === false, "open restored busy state");
assert(offCalls === 1, "late open subscription was not removed");
assert(
  notifications.errors.length === errorsBefore
  && notifications.successes.length === successesBefore,
  "late open emitted a notification",
);
"""
    )


def test_store_requires_explicit_mcp_preview_confirmation() -> None:
    store = read("webui", "sem-review-store.js")
    enable_start = store.index("  async enableMcp(")
    enable_end = store.index("\n  async disableMcp(", enable_start)
    enable = store[enable_start:enable_end]
    assert "this.mcpPreview" in enable
    assert "this.mcpConfirmed !== true" in enable
    assert "preview_token" in enable
    assert "confirmed: true" in enable
    assert "throw error" in enable


def test_grouping_is_sorted_without_mutating_api_changes() -> None:
    store = read("webui", "sem-review-store.js")
    grouped_start = store.index("  groupedChanges(")
    grouped_end = store.index("\n  selectedChange(", grouped_start)
    grouped = store[grouped_start:grouped_end]
    assert "[...this.changes()]" in grouped
    assert ".sort(" in grouped
    assert ".push(" in grouped
    assert ".changes.sort(" not in grouped


def test_shell_is_local_bounded_and_structurally_polished() -> None:
    documents = {
        "main": read("webui", "main.html"),
        "panel": read("webui", "panel.html"),
        "canvas": read(
            "extensions",
            "webui",
            "right-canvas-panels",
            "sem-review-panel.html",
        ),
    }
    assert "Semantic Review" in documents["main"]
    assert "sem-review-ledger" in documents["panel"]
    assert 'data-surface-id="sem_review_loop"' in documents["canvas"]
    combined = "\n".join(documents.values())
    assert "http://" not in combined
    assert "https://" not in combined
    assert "<iframe" not in combined
    assert "x-html" not in combined
    assert "overflow-wrap: anywhere" in documents["panel"]


def test_store_formats_root_scope_and_colored_line_diff() -> None:
    run_store_behavior(
        """
model.payload = payload("project-a", 1, "fingerprint-a");
assert(model.watchedLabel() === "Project root", "root scope should be readable");
model.detail = {
  before_content: "keep\\nold value",
  after_content: "keep\\nnew value",
};
const lines = model.detailDiffLines();
assert(lines.some((line) => line.kind === "removed" && line.text === "old value"), "removed line was not marked");
assert(lines.some((line) => line.kind === "added" && line.text === "new value"), "added line was not marked");
"""
    )


def test_late_mcp_preview_cannot_cross_context_or_project() -> None:
    run_store_behavior(
        """
model.contextId = "ctx-a";
model.payload = payload("project-a", 1, "fingerprint-a");
const errorsBefore = notifications.errors.length;
const previewPromise = model.previewMcp();
const previewCall = take("sem_mcp", "preview");
model._resetForContext("ctx-b");
model.payload = payload("project-b", 1, "fingerprint-b");
previewCall.resolve({
  ok: true,
  preview_token: "old-project-token",
  entry: { command: "/old/sem" },
  confirmation_required: true,
});
assert(await previewPromise === null, "late preview should be ignored");
assert(model.mcpPreview === null, "late preview token leaked into new context");
assert(
  notifications.errors.length === errorsBefore,
  "stale preview emitted an error notification",
);

model._resetForContext("ctx-a");
model.payload = payload("project-a", 2, "fingerprint-a2");
const currentPreview = model.previewMcp();
take("sem_mcp", "preview").resolve({
  ok: true,
  preview_token: "project-a-token",
  entry: { command: "/current/sem" },
  confirmation_required: true,
});
await currentPreview;
model.mcpConfirmed = true;
model.payload = payload("project-b", 2, "fingerprint-b2");
let rejected = false;
try {
  await model.enableMcp();
} catch {
  rejected = true;
}
assert(rejected, "cross-project preview was accepted");
assert(
  !pending.some((item) => item.request.action === "enable"),
  "cross-project enable request reached the backend",
);
"""
    )


def test_late_diff_response_cannot_overwrite_or_notify_new_context() -> None:
    run_store_behavior(
        """
model.contextId = "ctx-a";
model.payload = payload("project-a", 1, "fingerprint-a");
const errorsBefore = notifications.errors.length;
const diffPromise = model.refreshDiff();
const diffCall = take("sem_diff");
model._resetForContext("ctx-b");
model.payload = payload("project-b", 4, "fingerprint-b");
diffCall.resolve({
  ok: true,
      snapshot: {
        request: { mode: "working" },
        revision: 2,
        fingerprint: "old-response",
        changes: [],
        summary: { total: 0 },
        sem_version: "0.21.0",
        completed_at: "2026-07-29T00:00:00+00:00",
        stale: false,
        error: "",
  },
});
assert(await diffPromise === null, "late diff should be ignored");
assert(model._projectId() === "project-b", "late diff changed project");
assert(
  model.snapshot().fingerprint === "fingerprint-b",
  "late diff overwrote the new context snapshot",
);
assert(
  notifications.errors.length === errorsBefore,
  "stale diff emitted an error notification",
);

model._resetForContext("ctx-a");
model.payload = payload("project-a", 1, "fingerprint-a");
const olderDiff = model.refreshDiff();
const olderDiffCall = take("sem_diff");
const newerStatus = model.refreshStatus({
  keepSelection: true,
  notify: false,
});
take("sem_status").resolve(payload("project-a", 3, "newer-status"));
await newerStatus;
olderDiffCall.resolve({
  ok: true,
    snapshot: {
      request: { mode: "working" },
      revision: 2,
      fingerprint: "older-diff",
      changes: [],
      summary: { total: 0 },
      sem_version: "0.21.0",
      completed_at: "2026-07-29T00:00:00+00:00",
      stale: false,
      error: "",
  },
});
assert(await olderDiff === null, "older diff beat a newer status");
assert(
  model.snapshot().fingerprint === "newer-status",
  "older diff replaced newer same-context status",
);
"""
    )


def test_snapshot_change_clears_and_rejects_all_derived_responses() -> None:
    run_store_behavior(
        """
model.contextId = "ctx-a";
model.payload = payload("project-a", 1, "fingerprint-a");
model.selectedEntityId = "entity-1";
model.detail = { value: "old detail" };
model.contextResult = { value: "old context" };
model.impactResult = { value: "old impact" };
const errorsBefore = notifications.errors.length;

const detailPromise = model.loadDetail();
const detailCall = take("sem_detail");
const contextPromise = model.loadContext();
const contextCall = take("sem_context");
const impactPromise = model.loadImpact();
const impactCall = take("sem_impact");
const statusPromise = model.refreshStatus({
  keepSelection: true,
  notify: false,
});
take("sem_status").resolve(payload("project-a", 2, "fingerprint-b"));
await statusPromise;

assert(model.selectedEntityId === "entity-1", "valid selection was lost");
assert(model.detail === null, "detail survived a new snapshot");
assert(model.contextResult === null, "context survived a new snapshot");
assert(model.impactResult === null, "impact survived a new snapshot");

detailCall.resolve({ ok: true, detail: { value: "late detail" } });
contextCall.resolve({ ok: true, context: { value: "late context" } });
impactCall.resolve({ ok: true, impact: { value: "late impact" } });
await Promise.all([detailPromise, contextPromise, impactPromise]);

assert(model.detail === null, "late detail crossed view revision");
assert(model.contextResult === null, "late context crossed view revision");
assert(model.impactResult === null, "late impact crossed view revision");
assert(
  notifications.errors.length === errorsBefore,
  "stale derived response emitted an error notification",
);
"""
    )


def test_same_context_project_switch_rejects_old_lessons_and_actions() -> None:
    run_store_behavior(
        """
model.contextId = "ctx-shared";
model.payload = {
  ...payload("project-a", 1, "fingerprint-a"),
  lessons: { pending: [{ proposal_id: "project-a-only" }], approved: [] },
};
const errorsBefore = notifications.errors.length;
const successesBefore = notifications.successes.length;

const projectSwitch = model.refreshStatus({ notify: false });
take("sem_status").resolve(payload("project-b", 2, "fingerprint-b"));
await projectSwitch;
assert(
  model.pendingLessons().length === 0,
  "status project switch carried lessons across projects",
);

model._applyPayload(payload("project-a", 1, "fingerprint-a"));
const oldLessons = model.loadLessons();
const oldLessonsCall = take("sem_lessons", "list");
model._applyPayload({
  ...payload("project-b", 2, "fingerprint-b"),
  lessons: { pending: [{ proposal_id: "new" }], approved: [] },
});
oldLessonsCall.resolve({
  ok: true,
  lessons: { pending: [{ proposal_id: "old" }], approved: [] },
});
assert(await oldLessons === null, "old-project lessons were accepted");
assert(
  model.pendingLessons()[0]?.proposal_id === "new",
  "old-project lessons overwrote the current project",
);

model._applyPayload(payload("project-a", 3, "fingerprint-a3"));
const oldAction = model.approveLesson("proposal-old");
const oldActionCall = take("sem_lessons", "approve");
model._applyPayload(payload("project-b", 4, "fingerprint-b4"));
oldActionCall.resolve({ ok: true, action: "approve" });
await new Promise((resolve) => setImmediate(resolve));
const leakedRefresh = pending.find((item) => (
  item.path.endsWith("/sem_status")
  || (item.path.endsWith("/sem_lessons") && item.request.action === "list")
));
if (leakedRefresh) {
  if (leakedRefresh.path.endsWith("/sem_status")) {
    leakedRefresh.resolve(payload("project-b", 4, "fingerprint-b4"));
  } else {
    leakedRefresh.resolve({
      ok: true,
      lessons: { pending: [], approved: [] },
    });
  }
}
assert(await oldAction === null, "old-project lesson action succeeded");
assert(!leakedRefresh, "old-project lesson action refreshed current project");
assert(
  notifications.errors.length === errorsBefore
  && notifications.successes.length === successesBefore,
  "stale project work emitted a notification",
);
"""
    )


def test_old_context_review_mutation_cannot_refresh_or_notify_new_context() -> None:
    run_store_behavior(
        """
model.contextId = "ctx-a";
model.payload = payload("project-a", 1, "fingerprint-a");
const errorsBefore = notifications.errors.length;
const successesBefore = notifications.successes.length;
const cancellation = model.cancelReview();
const cancelCall = take("sem_review", "cancel");

model._resetForContext("ctx-b");
model.payload = payload("project-b", 2, "fingerprint-b");
cancelCall.resolve({ ok: true, action: "cancel" });
await new Promise((resolve) => setImmediate(resolve));
const leakedStatus = pending.find((item) => item.path.endsWith("/sem_status"));
if (leakedStatus) leakedStatus.resolve(payload("project-b", 2, "fingerprint-b"));

assert(await cancellation === null, "old-context cancellation succeeded");
assert(!leakedStatus, "old-context cancellation refreshed new context");
assert(
  model._projectId() === "project-b"
  && model.snapshot().fingerprint === "fingerprint-b",
  "old-context cancellation mutated the current view",
);
assert(
  notifications.errors.length === errorsBefore
  && notifications.successes.length === successesBefore,
  "old-context cancellation emitted a notification",
);
"""
    )


def test_false_ok_and_malformed_query_responses_never_succeed() -> None:
    run_store_behavior(
        """
model.contextId = "ctx-a";
model.payload = payload("project-a", 1, "fingerprint-a");
model.selectedEntityId = "entity-1";

const statusErrors = notifications.errors.length;
const invalidStatus = model.refreshStatus();
take("sem_status").resolve({
  review: payload("project-a", 9, "malformed-status").review,
});
assert(await invalidStatus === null, "status without ok:true succeeded");
assert(
  model.snapshot().fingerprint === "fingerprint-a",
  "malformed status mutated the current view",
);
assert(
  notifications.errors.length === statusErrors + 1,
  "malformed current status did not emit one error",
);

const detailErrors = notifications.errors.length;
const invalidDetail = model.loadDetail();
take("sem_detail").resolve({
  ok: false,
  detail: { value: "must-not-apply" },
});
assert(await invalidDetail === null, "ok:false detail succeeded");
assert(model.detail === null, "ok:false detail mutated state");
assert(
  notifications.errors.length === detailErrors + 1,
  "ok:false detail did not emit one error",
);

const lessonErrors = notifications.errors.length;
const invalidLessons = model.loadLessons();
take("sem_lessons", "list").resolve({
  ok: true,
  lessons: { pending: "not-an-array", approved: [] },
});
assert(await invalidLessons === null, "malformed lesson lists succeeded");
assert(
  notifications.errors.length === lessonErrors + 1,
  "malformed lessons did not emit one error",
);
"""
    )


def test_false_ok_mutations_do_not_refresh_or_notify_success() -> None:
    run_store_behavior(
        """
model.contextId = "ctx-a";
model.payload = payload("project-a", 1, "fingerprint-a");
const errorsBefore = notifications.errors.length;
const successesBefore = notifications.successes.length;

const cancellation = model.cancelReview();
take("sem_review", "cancel").resolve({
  ok: false,
  error: "cancel rejected",
});
await new Promise((resolve) => setImmediate(resolve));
const leakedStatus = pending.find((item) => item.path.endsWith("/sem_status"));
if (leakedStatus) leakedStatus.resolve(payload("project-a", 1, "fingerprint-a"));

assert(await cancellation === null, "ok:false cancellation succeeded");
assert(!leakedStatus, "ok:false cancellation triggered a refresh");
assert(
  notifications.successes.length === successesBefore,
  "ok:false cancellation emitted success",
);
assert(
  notifications.errors.length === errorsBefore + 1,
  "ok:false cancellation did not emit one error",
);
"""
    )


def test_overlapping_context_and_impact_keep_busy_until_both_finish() -> None:
    run_store_behavior(
        """
model.contextId = "ctx-a";
model.payload = payload("project-a", 1, "fingerprint-a");
model.selectedEntityId = "entity-1";

const contextPromise = model.loadContext();
const contextCall = take("sem_context");
const impactPromise = model.loadImpact();
const impactCall = take("sem_impact");
assert(model.busy === true, "overlapping work did not set busy");

contextCall.resolve({ ok: true, context: { value: "context" } });
await contextPromise;
assert(model.busy === true, "first completion cleared overlapping busy state");

impactCall.resolve({ ok: true, impact: { value: "impact" } });
await impactPromise;
assert(model.busy === false, "busy survived all current operations");

const lateContext = model.loadContext();
const lateContextCall = take("sem_context");
model.cleanup();
lateContextCall.resolve({ ok: true, context: { value: "late" } });
await lateContext;
assert(model.busy === false, "late decrement restored busy after cleanup");
"""
    )


def test_nested_status_shape_is_validated_before_replacing_payload() -> None:
    run_store_behavior(
        """
model.contextId = "ctx-a";
model.payload = payload("project-a", 1, "fingerprint-a");
const previousPayload = model.payload;
const errorsBefore = notifications.errors.length;

const invalidStatus = model.refreshStatus();
take("sem_status").resolve({ ok: true, review: {} });

assert(await invalidStatus === null, "empty nested review status succeeded");
assert(model.payload === previousPayload, "invalid status replaced last-valid payload");
assert(
  model.snapshot().fingerprint === "fingerprint-a",
  "invalid status cleared the last-valid snapshot",
);
assert(
  notifications.errors.length === errorsBefore + 1,
  "invalid nested status did not call frontendError",
);
assert(
  typeof model.error === "string"
  && model.error.length > 0
  && model.error.length <= 500,
  "invalid nested status did not retain a bounded diagnostic",
);
"""
    )


def test_cancel_uses_snapshot_cas_and_ignores_same_project_drift() -> None:
    run_store_behavior(
        """
model.contextId = "ctx-a";
model.payload = payload("project-a", 1, "fingerprint-a");
const successesBefore = notifications.successes.length;
const errorsBefore = notifications.errors.length;

const cancellation = model.cancelReview();
const cancelCall = take("sem_review", "cancel");
assert(cancelCall.request.revision === 1, "cancel omitted exact revision");
assert(
  cancelCall.request.fingerprint === "fingerprint-a",
  "cancel omitted exact fingerprint",
);

model._applyPayload(payload("project-a", 2, "fingerprint-b"));
cancelCall.resolve({ ok: true, review: {} });
await new Promise((resolve) => setImmediate(resolve));

assert(await cancellation === null, "stale same-project cancellation succeeded");
assert(
  !pending.some((item) => item.path.endsWith("/sem_status")),
  "stale cancellation refreshed newer state",
);
assert(
  notifications.successes.length === successesBefore,
  "stale cancellation emitted success",
);
assert(
  notifications.errors.length === errorsBefore,
  "stale cancellation emitted a misleading error",
);
"""
    )


def test_post_mutation_refresh_failures_notify_and_never_toast_success() -> None:
    run_store_behavior(
        """
model.contextId = "ctx-a";
model.payload = payload("project-a", 1, "fingerprint-a");

const cancelErrors = notifications.errors.length;
const cancelSuccesses = notifications.successes.length;
const cancellation = model.cancelReview();
take("sem_review", "cancel").resolve({ ok: true, review: {} });
await new Promise((resolve) => setImmediate(resolve));
take("sem_status").reject(new Error("cancel follow-up\\nstatus failed"));
assert(await cancellation === null, "cancel succeeded without refreshed status");
assert(
  notifications.errors.length === cancelErrors + 1,
  "cancel follow-up failure did not call frontendError",
);
assert(
  notifications.successes.length === cancelSuccesses,
  "cancel follow-up failure emitted success",
);
assert(
  model.error === "cancel follow-up status failed",
  "cancel follow-up diagnostic was not retained as plain text",
);

const lessonErrors = notifications.errors.length;
const lessonSuccesses = notifications.successes.length;
const lessonMutation = model.approveLesson("proposal-1");
take("sem_lessons", "approve").resolve({ ok: true, lesson: {} });
await new Promise((resolve) => setImmediate(resolve));
take("sem_status").resolve(payload("project-a", 1, "fingerprint-a"));
await new Promise((resolve) => setImmediate(resolve));
take("sem_lessons", "list").reject(new Error("lesson reload failed"));
assert(
  await lessonMutation === null,
  "lesson mutation succeeded without refreshed lessons",
);
assert(
  notifications.errors.length === lessonErrors + 1,
  "lesson reload failure did not call frontendError",
);
assert(
  notifications.successes.length === lessonSuccesses,
  "lesson reload failure emitted success",
);
assert(model.error === "lesson reload failed", "lesson diagnostic was lost");
"""
    )


def test_all_status_followups_report_failure_without_success_toasts() -> None:
    run_store_behavior(
        """
model.contextId = "ctx-a";
model.payload = payload("project-a", 1, "fingerprint-a");

async function rejectStatus(operation, operationCall, operationResponse, label) {
  const errorsBefore = notifications.errors.length;
  const successesBefore = notifications.successes.length;
  operationCall.resolve(operationResponse);
  await new Promise((resolve) => setImmediate(resolve));
  take("sem_status").reject(new Error(`${label} status failed`));
  assert(await operation === null, `${label} succeeded without fresh status`);
  assert(
    notifications.errors.length === errorsBefore + 1,
    `${label} status failure did not call frontendError`,
  );
  assert(
    notifications.successes.length === successesBefore,
    `${label} status failure emitted success`,
  );
}

const diff = model.refreshDiff();
await rejectStatus(
  diff,
  take("sem_diff"),
  {
    ok: true,
    snapshot: {
      request: { mode: "working" },
      revision: 2,
      fingerprint: "fingerprint-b",
      changes: [],
      summary: { total: 0 },
      sem_version: "0.21.0",
      completed_at: "2026-07-29T00:00:00+00:00",
      stale: false,
      error: "",
    },
  },
  "diff",
);

model.mcpPreview = { preview_token: "preview-token" };
model.mcpConfirmed = true;
model._mcpPreviewBinding = model._operationBinding();
const enable = model.enableMcp();
await rejectStatus(
  enable,
  take("sem_mcp", "enable"),
  { ok: true, enabled: true },
  "enable",
);

const disable = model.disableMcp();
await rejectStatus(
  disable,
  take("sem_mcp", "disable"),
  { ok: true, enabled: false },
  "disable",
);

const lesson = model.approveLesson("proposal-status");
await rejectStatus(
  lesson,
  take("sem_lessons", "approve"),
  { ok: true, lesson: {} },
  "lesson",
);
assert(
  !pending.some(
    (item) => item.path.endsWith("/sem_lessons")
    && item.request.action === "list"
  ),
  "lesson mutation loaded lessons after status failure",
);
"""
    )


def test_revision_handler_uses_canonical_project_and_snapshot_revision() -> None:
    run_store_behavior(
        """
let revisionHandler = null;
socket.on = async (_event, handler) => { revisionHandler = handler; };
model.contextId = "ctx-a";
model.payload = {
  ok: true,
  project: { id: "project-a" },
  review: {
    snapshot: {
      revision: 5,
      fingerprint: "fingerprint-a",
      changes: [],
      summary: { total: 0 },
    },
  },
};
model._root = { id: "root" };
model._mounted = true;
assert(
  await model._subscribeRevision(model._lifecycleSeq, model._root),
  "revision subscription failed",
);

const originalSetTimeout = globalThis.setTimeout;
const originalClearTimeout = globalThis.clearTimeout;
const timers = [];
globalThis.setTimeout = (callback) => {
  timers.push(callback);
  return timers.length;
};
globalThis.clearTimeout = () => {};
let refreshes = 0;
model.refreshStatus = async () => { refreshes += 1; };

revisionHandler({
  data: { context_id: "ctx-a", project_id: "project-b", revision: 6 },
});
assert(timers.length === 0, "alternate project shape accepted wrong project");
revisionHandler({
  data: { context_id: "ctx-a", project_id: "project-a", revision: 5 },
});
assert(timers.length === 0, "equal snapshot revision scheduled a refresh");
revisionHandler({
  data: { context_id: "ctx-a", project_id: "project-a", revision: 6 },
});
assert(timers.length === 1, "new canonical revision did not schedule refresh");
timers[0]();
assert(refreshes === 1, "scheduled revision refresh did not run");

globalThis.setTimeout = originalSetTimeout;
globalThis.clearTimeout = originalClearTimeout;
"""
    )


def test_revision_timer_cannot_refresh_same_context_after_project_switch() -> None:
    run_store_behavior(
        """
let revisionHandler = null;
socket.on = async (_event, handler) => { revisionHandler = handler; };
model.contextId = "ctx-shared";
model.payload = payload("project-a", 1, "fingerprint-a");
model._root = { id: "root" };
model._mounted = true;
assert(
  await model._subscribeRevision(model._lifecycleSeq, model._root),
  "revision subscription failed",
);

const originalSetTimeout = globalThis.setTimeout;
const originalClearTimeout = globalThis.clearTimeout;
const timers = [];
const cleared = [];
globalThis.setTimeout = (callback) => {
  timers.push(callback);
  return timers.length;
};
globalThis.clearTimeout = (handle) => { cleared.push(handle); };
let refreshes = 0;
model.refreshStatus = async () => { refreshes += 1; };

revisionHandler({
  data: { context_id: "ctx-shared", project_id: "project-a", revision: 2 },
});
assert(timers.length === 1, "project-a revision did not schedule a refresh");

model._applyPayload(payload("project-b", 1, "fingerprint-b"));
assert(
  cleared.length === 1 && cleared[0] === 1,
  "project switch did not clear the pending revision timer",
);

timers[0]();
assert(refreshes === 0, "project-a timer refreshed project-b");
assert(model._projectId() === "project-b", "timer changed the active project");

globalThis.setTimeout = originalSetTimeout;
globalThis.clearTimeout = originalClearTimeout;
"""
    )


def test_store_rejects_malformed_diff_snapshots_and_bounds_display_values() -> None:
    run_store_behavior(
        """
model.contextId = "ctx-a";
model.payload = payload("project-a", 1, "fingerprint-a");
const errorsBefore = notifications.errors.length;
const request = model.refreshDiff();
const call = take("sem_diff");
call.resolve({
  ok: true,
  snapshot: { revision: 2, fingerprint: "bad", changes: [], summary: { total: 0 } },
});
assert(await request === null, "malformed diff snapshot succeeded");
assert(model.snapshot().fingerprint === "fingerprint-a", "malformed diff replaced state");
assert(notifications.errors.length === errorsBefore + 1, "malformed diff was not reported");
assert(
  model.formatResult({ source: "x".repeat(250000) }).includes("exceeded the display limit"),
  "oversized result was stringified instead of bounded",
);
"""
    )


def test_store_rolls_back_failed_subscription_and_redacts_diagnostics() -> None:
    run_store_behavior(
        """
socket.on = async () => { throw new Error("credential=super-secret /Users/private/repo"); };
let rejected = false;
try {
  await model.onMount({ id: "root" }, { context_id: "ctx-a" });
} catch {
  rejected = true;
}
assert(rejected, "subscription failure was swallowed");
assert(!model._mounted && model._root === null, "failed mount remained active");
assert(model.contextId === "" && model.payload === null, "failed mount retained source state");
model._notifyError(new Error("credential=super-secret /Users/private/repo"));
assert(!model.error.includes("super-secret"), "diagnostic leaked credential");
assert(!model.error.includes("/Users/private/repo"), "diagnostic leaked local path");
assert(notifications.errors.at(-1)[0] === model.error, "notification used an unsanitized diagnostic");
"""
    )
