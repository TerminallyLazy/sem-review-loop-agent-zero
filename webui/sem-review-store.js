import { createStore } from "/js/AlpineStore.js";
import { callJsonApi } from "/js/api.js";
import { getContext } from "/index.js";
import { getNamespacedClient } from "/js/websocket.js";
import { store as notificationStore } from "/components/notifications/notification-store.js";

const reviewSocket = getNamespacedClient("/ws");
reviewSocket.addHandlers(["ws_webui"]);

const REVISION_EVENT_FIELDS = Object.freeze([
  "context_id",
  "project_id",
  "revision",
]);
const REVISION_DELAY_MS = 120;
const MAX_DIAGNOSTIC_CHARS = 500;
const MAX_FORMATTED_RESULT_CHARS = 200000;
const MAX_RESPONSE_ITEMS = 10000;
const MAX_RESPONSE_NODES = 4096;
const MAX_RESPONSE_DEPTH = 8;
const MAX_RESPONSE_STRING_CHARS = 200000;
const MAX_FINGERPRINT_CHARS = 256;
const MAX_PATH_CHARS = 4096;
const MAX_ENTITY_CHARS = 2048;

function apiPath(name) {
  return `/plugins/sem_review_loop/${name}`;
}

function plainError(error, fallback = "Semantic Review request failed.") {
  const value = error instanceof Error ? error.message : String(error || "");
  let message = value.replace(/\s+/g, " ").trim();
  if (!message) return fallback;
  if (
    /-----BEGIN [^-]+-----/i.test(message)
    || /(?:api[_ -]?key|token|secret|password|authorization|bearer|credential)\s*[:=]\s*\S+/i.test(message)
  ) {
    message = message.replace(
      /((?:api[_ -]?key|token|secret|password|authorization|bearer|credential)\s*[:=]\s*)\S+/gi,
      "$1[redacted]",
    );
  }
  message = message.replace(/\b(?:\/Users|\/home|\/private|\/tmp)\/[^\s,;]+/g, "<local path>");
  message = message.replace(/\b[A-Za-z]:\\[^\s,;]+/g, "<local path>");
  if (/\b(?:ssh|https?):\/\/[^\s]+/i.test(message)) {
    message = message.replace(/\b(?:ssh|https?):\/\/[^\s]+/gi, "<remote reference>");
  }
  return message.slice(0, MAX_DIAGNOSTIC_CHARS) || fallback;
}

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function requireApiSuccess(response, message, required = {}) {
  if (!isPlainObject(response) || response.ok !== true) {
    const responseError = (
      isPlainObject(response)
      && typeof response.error === "string"
      && response.error.trim()
    )
      ? response.error
      : message;
    throw new Error(responseError);
  }
  for (const [field, validator] of Object.entries(required)) {
    if (!validator(response[field])) {
      throw new Error(message);
    }
  }
  return response;
}

function isLessonLists(value) {
  return Boolean(
    isPlainObject(value)
    && Array.isArray(value.pending)
    && Array.isArray(value.approved)
    && value.pending.length <= 512
    && value.approved.length <= 512
    && value.pending.every(isBoundedResponseObject)
    && value.approved.every(isBoundedResponseObject)
  );
}

function isNonEmptyString(value) {
  return typeof value === "string" && value.trim().length > 0;
}

function isNonNegativeInteger(value) {
  return (
    typeof value === "number"
    && Number.isSafeInteger(value)
    && value >= 0
  );
}

function isBoundedJson(value, options = {}, state = null) {
  const maxNodes = options.maxNodes ?? MAX_RESPONSE_NODES;
  const maxDepth = options.maxDepth ?? MAX_RESPONSE_DEPTH;
  const maxStringChars = options.maxStringChars ?? MAX_RESPONSE_STRING_CHARS;
  const maxItems = options.maxItems ?? MAX_RESPONSE_ITEMS;
  const tracker = state || { nodes: 0, chars: 0, seen: new WeakSet() };
  if (++tracker.nodes > maxNodes) return false;
  if (typeof value === "string") {
    if (value.length > maxStringChars) return false;
    tracker.chars += value.length;
    return tracker.chars <= maxStringChars;
  }
  if (
    value === null
    || typeof value === "boolean"
    || (typeof value === "number" && Number.isFinite(value))
  ) return true;
  if (
    typeof value !== "object"
    || (!isPlainObject(value) && !Array.isArray(value))
    || maxDepth < 0
  ) return false;
  if (tracker.seen.has(value)) return false;
  tracker.seen.add(value);
  const values = Array.isArray(value) ? value : Object.values(value);
  if (values.length > maxItems) return false;
  return values.every((item) => isBoundedJson(item, {
    maxNodes,
    maxDepth: maxDepth - 1,
    maxStringChars,
    maxItems,
  }, tracker));
}

function boundedString(value, maximum = MAX_ENTITY_CHARS) {
  return typeof value === "string" && value.length > 0 && value.length <= maximum;
}

function isSnapshotChange(value) {
  return Boolean(
    isPlainObject(value)
    && boundedString(value.entity_id)
    && boundedString(value.file_path, MAX_PATH_CHARS)
    && boundedString(value.change_type, 32)
    && typeof value.structural === "boolean"
    && (value.start_line === null || isNonNegativeInteger(value.start_line))
    && (value.end_line === null || isNonNegativeInteger(value.end_line))
    && isBoundedJson(value, { maxStringChars: MAX_PATH_CHARS }),
  );
}

function isBoundedResponseObject(value) {
  return isPlainObject(value) && isBoundedJson(value);
}

function isSnapshotStatus(value, { working = false } = {}) {
  if (!isPlainObject(value)) return false;
  const mode = value?.request?.mode;
  return Boolean(
    isPlainObject(value.request)
    && ["working", "staged", "commit", "range", "stdin"].includes(mode)
    && (!working || mode === "working")
    && boundedString(value.fingerprint, MAX_FINGERPRINT_CHARS)
    && isNonNegativeInteger(value.revision)
    && isPlainObject(value.summary)
    && isNonNegativeInteger(value.summary.total)
    && value.summary.total <= MAX_RESPONSE_ITEMS
    && Object.values(value.summary).every(
      (item) => (
        typeof item === "number"
        && Number.isFinite(item)
        && item >= 0
        && item <= MAX_RESPONSE_ITEMS
      ),
    )
    && Array.isArray(value.changes)
    && value.changes.length <= MAX_RESPONSE_ITEMS
    && value.changes.every(isSnapshotChange)
    && boundedString(value.sem_version, 64)
    && boundedString(value.completed_at, 128)
    && typeof value.stale === "boolean"
    && typeof value.error === "string"
    && value.error.length <= MAX_DIAGNOSTIC_CHARS
    && isBoundedJson(value.request, { maxStringChars: MAX_PATH_CHARS })
  );
}

function isCheckpointStatus(value) {
  if (value === null) return true;
  return Boolean(
    isPlainObject(value)
    && isNonEmptyString(value.fingerprint)
    && ["pass", "repaired", "unresolved", "cancelled"].includes(value.outcome)
    && Array.isArray(value.structural_entities)
    && value.structural_entities.every(
      (item) => typeof item === "string",
    )
    && Array.isArray(value.findings)
    && value.findings.every((item) => typeof item === "string")
  );
}

function isReviewStatus(value, contextId) {
  if (!isPlainObject(value)) return false;
  if (
    !Object.hasOwn(value, "working")
    || !Object.hasOwn(value, "snapshot")
    || !Object.hasOwn(value, "checkpoint")
  ) {
    return false;
  }
  const working = value.working;
  const snapshot = value.snapshot;
  if (
    working !== null
    && !isSnapshotStatus(working, { working: true })
  ) {
    return false;
  }
  if (snapshot !== null && !isSnapshotStatus(snapshot)) {
    return false;
  }
  if (
    (working && working.revision > value.revision)
    || (snapshot && snapshot.revision > value.revision)
  ) {
    return false;
  }
  return Boolean(
    isNonEmptyString(value.context_id)
    && value.context_id === contextId
    && isNonEmptyString(value.project_id)
    && typeof value.watched_relative === "string"
    && isNonNegativeInteger(value.revision)
    && isNonNegativeInteger(value.pending_generation)
    && isCheckpointStatus(value.checkpoint)
    && isNonNegativeInteger(value.repair_cycle)
    && typeof value.mcp_enabled === "boolean"
    && typeof value.error === "string"
  );
}

function isRevisionEventData(data) {
  if (!isPlainObject(data)) return false;
  const keys = Object.keys(data).sort();
  if (
    keys.length !== REVISION_EVENT_FIELDS.length
    || keys.some((key, index) => key !== REVISION_EVENT_FIELDS[index])
  ) {
    return false;
  }
  return (
    typeof data.context_id === "string"
    && data.context_id.trim().length > 0
    && typeof data.project_id === "string"
    && data.project_id.trim().length > 0
    && typeof data.revision === "number"
    && Number.isSafeInteger(data.revision)
    && data.revision >= 0
  );
}

function lessonLists(value) {
  return {
    pending: Array.isArray(value?.pending) ? value.pending : [],
    approved: Array.isArray(value?.approved) ? value.approved : [],
  };
}

const EMPTY_SUMMARY = Object.freeze({
  file_count: 0,
  added: 0,
  modified: 0,
  deleted: 0,
  moved: 0,
  renamed: 0,
  reordered: 0,
  binary: 0,
  orphan: 0,
  total: 0,
  structural: 0,
});

const model = {
  loading: false,
  busy: false,
  error: "",
  contextId: "",
  payload: null,
  activeTab: "changes",
  diffMode: "working",
  commitRef: "",
  fromRef: "",
  toRef: "",
  selectedEntityId: "",
  detail: null,
  contextResult: null,
  impactResult: null,
  mcpPreview: null,
  mcpConfirmed: false,
  _mcpPreviewBinding: null,
  _root: null,
  _mode: "canvas",
  _initialized: false,
  _mounted: false,
  _revisionHandler: null,
  _revisionOff: null,
  _revisionTimer: null,
  _revisionTimerBinding: null,
  _lifecycleSeq: 0,
  _subscriptionSeq: 0,
  _requestSeq: 0,
  _diffRequestSeq: 0,
  _mcpRequestSeq: 0,
  _detailRequestSeq: 0,
  _contextRequestSeq: 0,
  _impactRequestSeq: 0,
  _lessonRequestSeq: 0,
  _actionEpoch: 0,
  _busyEpoch: 0,
  _busyCount: 0,

  resolveContextId(payload = {}) {
    const explicit = String(payload?.context_id || payload?.ctxid || "").trim();
    if (explicit) return explicit;
    try {
      return String(getContext?.() || "").trim();
    } catch {
      return "";
    }
  },

  async init() {
    if (this._initialized) return;
    this._initialized = true;
  },

  _projectId(payload = this.payload) {
    return String(
      payload?.review?.project_id
      || payload?.project?.project_id
      || payload?.project?.id
      || payload?.project_id
      || "",
    );
  },

  _revisionIdentity(payload = this.payload) {
    const value = (
      payload?.review?.revision
      ?? payload?.review?.snapshot?.revision
      ?? payload?.review?.working?.revision
      ?? 0
    );
    const revision = Number(value);
    return (
      Number.isSafeInteger(revision) && revision >= 0
        ? revision
        : 0
    );
  },

  _operationBinding() {
    const snapshot = this.snapshot();
    const revisionValue = Number(snapshot?.revision);
    return {
      lifecycleSeq: this._lifecycleSeq,
      actionEpoch: this._actionEpoch,
      contextId: String(this.contextId || ""),
      projectId: this._projectId(),
      revision: (
        Number.isSafeInteger(revisionValue) && revisionValue >= 0
          ? revisionValue
          : null
      ),
      fingerprint: String(snapshot?.fingerprint || ""),
    };
  },

  _operationBindingIsCurrent(binding) {
    const current = this._operationBinding();
    return Boolean(
      binding
      && binding.lifecycleSeq === this._lifecycleSeq
      && binding.actionEpoch === this._actionEpoch
      && binding.contextId === String(this.contextId || "")
      && binding.projectId === this._projectId()
      && binding.revision === current.revision
      && binding.fingerprint === current.fingerprint
    );
  },

  _invalidateActions() {
    this._actionEpoch += 1;
    this._lessonRequestSeq += 1;
  },

  _beginBusy() {
    const token = { epoch: this._busyEpoch };
    this._busyCount += 1;
    this.busy = true;
    return token;
  },

  _endBusy(token) {
    if (!token || token.epoch !== this._busyEpoch) return;
    this._busyCount = Math.max(0, this._busyCount - 1);
    this.busy = this._busyCount > 0;
  },

  _resetBusy() {
    this._busyEpoch += 1;
    this._busyCount = 0;
    this.busy = false;
  },

  _snapshotIdentity(snapshot = this.snapshot()) {
    if (!snapshot) return null;
    return {
      revision: String(snapshot.revision ?? ""),
      fingerprint: String(snapshot.fingerprint || ""),
    };
  },

  _sameSnapshot(left, right) {
    const leftIdentity = this._snapshotIdentity(left);
    const rightIdentity = this._snapshotIdentity(right);
    if (!leftIdentity || !rightIdentity) {
      return leftIdentity === rightIdentity;
    }
    return (
      leftIdentity.revision === rightIdentity.revision
      && leftIdentity.fingerprint === rightIdentity.fingerprint
    );
  },

  _clearRevisionTimer() {
    if (this._revisionTimer !== null) {
      globalThis.clearTimeout(this._revisionTimer);
    }
    this._revisionTimer = null;
    this._revisionTimerBinding = null;
  },

  _revisionTimerBindingIsCurrent(binding) {
    const currentRevision = this._revisionIdentity();
    return Boolean(
      binding
      && this._mounted
      && binding.lifecycleSeq === this._lifecycleSeq
      && binding.subscriptionSeq === this._subscriptionSeq
      && binding.rootToken === this._root
      && binding.contextId === String(this.contextId || "")
      && binding.projectId === this._projectId()
      && binding.baseRevision === currentRevision
      && binding.eventRevision > currentRevision
    );
  },

  _invalidateDerivedState({ clearSelection = false } = {}) {
    this._detailRequestSeq += 1;
    this._contextRequestSeq += 1;
    this._impactRequestSeq += 1;
    if (clearSelection) this.selectedEntityId = "";
    this.detail = null;
    this.contextResult = null;
    this.impactResult = null;
  },

  _clearMcpPreview({ invalidateRequest = false } = {}) {
    if (invalidateRequest) this._mcpRequestSeq += 1;
    this.mcpPreview = null;
    this.mcpConfirmed = false;
    this._mcpPreviewBinding = null;
  },

  _applyPayload(
    payload,
    {
      keepSelection = false,
      invalidateDiff = false,
    } = {},
  ) {
    const previousSnapshot = this.snapshot();
    const previousProjectId = this._projectId();
    this.payload = payload;
    const projectChanged = previousProjectId !== this._projectId();
    const snapshotChanged = !this._sameSnapshot(
      previousSnapshot,
      this.snapshot(),
    );
    if (projectChanged || snapshotChanged) {
      this._clearRevisionTimer();
    }
    if (projectChanged) {
      this._diffRequestSeq += 1;
      this._clearMcpPreview({ invalidateRequest: true });
      this._invalidateActions();
      this._resetBusy();
    }
    if (snapshotChanged) {
      if (invalidateDiff) this._diffRequestSeq += 1;
      this._invalidateDerivedState();
    }
    this._preserveSelection(keepSelection);
  },

  _selectionBinding() {
    const snapshot = this.snapshot();
    const entityId = String(this.selectedEntityId || "");
    const contextId = String(this.contextId || "");
    if (!snapshot || !entityId || !contextId) return null;
    return {
      contextId,
      projectId: this._projectId(),
      entityId,
      revision: String(snapshot.revision ?? ""),
      requestRevision: snapshot.revision,
      fingerprint: String(snapshot.fingerprint || ""),
    };
  },

  _selectionBindingIsCurrent(binding) {
    if (!binding) return false;
    const current = this._selectionBinding();
    return Boolean(
      current
      && current.contextId === binding.contextId
      && current.projectId === binding.projectId
      && current.entityId === binding.entityId
      && current.revision === binding.revision
      && current.fingerprint === binding.fingerprint
    );
  },

  _resetForContext(contextId) {
    if (contextId === this.contextId) return false;
    this._clearRevisionTimer();
    this.contextId = contextId;
    this.payload = null;
    this.error = "";
    this.loading = false;
    this._resetBusy();
    this._requestSeq += 1;
    this._diffRequestSeq += 1;
    this._invalidateDerivedState({ clearSelection: true });
    this._clearMcpPreview({ invalidateRequest: true });
    this._invalidateActions();
    return true;
  },

  _lifecycleIsCurrent(lifecycleSeq, rootToken) {
    return Boolean(
      this._mounted
      && lifecycleSeq === this._lifecycleSeq
      && this._root === rootToken
    );
  },

  _detachRevisionSubscription() {
    const removeRevisionHandler = this._revisionOff;
    this._revisionOff = null;
    this._revisionHandler = null;
    removeRevisionHandler?.();
  },

  _rollbackLifecycle(lifecycleSeq, rootToken) {
    if (!this._lifecycleIsCurrent(lifecycleSeq, rootToken)) return false;
    this.cleanup();
    return true;
  },

  async _subscribeRevision(lifecycleSeq, rootToken) {
    if (this._revisionOff) {
      return this._lifecycleIsCurrent(lifecycleSeq, rootToken);
    }
    const subscriptionSeq = ++this._subscriptionSeq;
    const revisionHandler = (envelope) => {
      if (
        !this._mounted
        || subscriptionSeq !== this._subscriptionSeq
        || this._revisionHandler !== revisionHandler
      ) {
        return;
      }
      const data = envelope?.data;
      if (!isRevisionEventData(data)) return;
      if (String(data.context_id) !== this.contextId) return;
      const currentProject = this._projectId();
      if (currentProject && String(data.project_id) !== currentProject) return;
      const revision = Number(data.revision);
      const currentRevision = this._revisionIdentity();
      if (revision <= currentRevision) return;
      this._clearRevisionTimer();
      const timerBinding = {
        lifecycleSeq: this._lifecycleSeq,
        subscriptionSeq,
        rootToken: this._root,
        contextId: String(this.contextId || ""),
        projectId: currentProject,
        baseRevision: currentRevision,
        eventRevision: revision,
      };
      this._revisionTimerBinding = timerBinding;
      this._revisionTimer = globalThis.setTimeout(() => {
        if (this._revisionTimerBinding !== timerBinding) return;
        this._revisionTimer = null;
        this._revisionTimerBinding = null;
        if (!this._revisionTimerBindingIsCurrent(timerBinding)) return;
        void this.refreshStatus({ keepSelection: true, notify: false });
      }, REVISION_DELAY_MS);
    };

    try {
      await reviewSocket.on("sem_review_revision", revisionHandler);
    } catch (error) {
      reviewSocket.off("sem_review_revision", revisionHandler);
      throw error;
    }
    if (
      !this._lifecycleIsCurrent(lifecycleSeq, rootToken)
      || subscriptionSeq !== this._subscriptionSeq
    ) {
      reviewSocket.off("sem_review_revision", revisionHandler);
      return false;
    }
    this._revisionHandler = revisionHandler;
    this._revisionOff = () => {
      reviewSocket.off("sem_review_revision", revisionHandler);
    };
    return true;
  },

  async onMount(element = null, options = {}) {
    const lifecycleSeq = ++this._lifecycleSeq;
    const rootToken = element || this._root;
    await this.init();
    if (lifecycleSeq !== this._lifecycleSeq) return false;
    const contextId = this.resolveContextId(options);
    const contextChanged = this._resetForContext(contextId);
    const sameMount = Boolean(
      this._mounted
      && element
      && element === this._root
      && this._revisionOff,
    );
    this._root = rootToken;
    this._mode = options?.mode === "modal" ? "modal" : "canvas";
    this._mounted = true;
    try {
      const subscribed = await this._subscribeRevision(
        lifecycleSeq,
        rootToken,
      );
      if (
        !subscribed
        || !this._lifecycleIsCurrent(lifecycleSeq, rootToken)
      ) {
        return false;
      }
      if (!sameMount || contextChanged || !this.payload) {
        await this.refreshStatus({ keepSelection: true });
        if (!this._lifecycleIsCurrent(lifecycleSeq, rootToken)) {
          return false;
        }
        await this.loadLessons({ notify: false, manageBusy: false });
        if (!this._lifecycleIsCurrent(lifecycleSeq, rootToken)) {
          return false;
        }
      }
      return true;
    } catch (error) {
      this._rollbackLifecycle(lifecycleSeq, rootToken);
      throw error;
    }
  },

  async onOpen(payload = {}) {
    const lifecycleSeq = ++this._lifecycleSeq;
    const rootToken = this._root;
    await this.init();
    if (lifecycleSeq !== this._lifecycleSeq) return false;
    this._mounted = true;
    this._resetForContext(this.resolveContextId(payload));
    try {
      const subscribed = await this._subscribeRevision(
        lifecycleSeq,
        rootToken,
      );
      if (
        !subscribed
        || !this._lifecycleIsCurrent(lifecycleSeq, rootToken)
      ) {
        return false;
      }
      await this.refreshStatus({ keepSelection: true });
      if (!this._lifecycleIsCurrent(lifecycleSeq, rootToken)) {
        return false;
      }
      await this.loadLessons({ notify: false, manageBusy: false });
      if (!this._lifecycleIsCurrent(lifecycleSeq, rootToken)) {
        return false;
      }
      const entityId = String(payload?.entity_id || "").trim();
      if (entityId) {
        await this.selectEntity(entityId);
        if (!this._lifecycleIsCurrent(lifecycleSeq, rootToken)) {
          return false;
        }
      }
      return true;
    } catch (error) {
      this._rollbackLifecycle(lifecycleSeq, rootToken);
      throw error;
    }
  },

  cleanup() {
    this._mounted = false;
    this._root = null;
    this._lifecycleSeq += 1;
    this._subscriptionSeq += 1;
    this._requestSeq += 1;
    this._diffRequestSeq += 1;
    this._invalidateDerivedState();
    this._clearMcpPreview({ invalidateRequest: true });
    this._invalidateActions();
    this.loading = false;
    this._resetBusy();
    this._clearRevisionTimer();
    this._detachRevisionSubscription();
    this.contextId = "";
    this.payload = null;
    this.selectedEntityId = "";
    this.error = "";
  },

  _notifyError(error, fallback) {
    const message = plainError(error, fallback);
    this.error = message;
    void notificationStore.frontendError(
      message,
      "Semantic Review",
      7,
    );
    return message;
  },

  _notifySuccess(message) {
    void notificationStore.frontendSuccess(
      message,
      "Semantic Review",
      3,
    );
  },

  _requireContext() {
    const contextId = this.resolveContextId({ context_id: this.contextId });
    if (!contextId) throw new Error("Select an Agent Zero project chat first.");
    if (contextId !== this.contextId) this._resetForContext(contextId);
    return contextId;
  },

  _preserveSelection(keepSelection = false) {
    if (
      keepSelection
      && this.selectedEntityId
      && this.changes().some(
        (change) => change?.entity_id === this.selectedEntityId,
      )
    ) {
      return;
    }
    this._invalidateDerivedState({ clearSelection: true });
  },

  async refreshStatus(options = {}) {
    let contextId;
    try {
      contextId = this._requireContext();
    } catch (error) {
      this.loading = false;
      this._clearRevisionTimer();
      this.payload = null;
      if (options?.notify !== false) this._notifyError(error);
      return null;
    }

    const requestSeq = ++this._requestSeq;
    this.loading = true;
    try {
      const rawResponse = await callJsonApi(apiPath("sem_status"), {
        context_id: contextId,
      });
      if (
        requestSeq !== this._requestSeq
        || contextId !== this.contextId
      ) {
        return null;
      }
      const response = requireApiSuccess(
        rawResponse,
        "Semantic Review returned an invalid status.",
        { review: (value) => isReviewStatus(value, contextId) },
      );
      const existingLessons = lessonLists(this.payload?.lessons);
      const hasLessonLists = (
        existingLessons.pending.length > 0
        || existingLessons.approved.length > 0
        || Array.isArray(this.payload?.lessons?.pending)
        || Array.isArray(this.payload?.lessons?.approved)
      );
      const sameProject = (
        this._projectId(response) === this._projectId()
      );
      this._applyPayload({
        ...response,
        lessons: (
          sameProject && hasLessonLists
            ? existingLessons
            : response.lessons
        ),
      }, {
        keepSelection: options?.keepSelection === true,
        invalidateDiff: true,
      });
      this.error = "";
      return response;
    } catch (error) {
      if (
        requestSeq !== this._requestSeq
        || contextId !== this.contextId
      ) {
        return null;
      }
      if (options?.notify !== false) {
        this._notifyError(error, "Unable to refresh semantic status.");
      } else {
        this.error = plainError(
          error,
          "Unable to refresh semantic status.",
        );
      }
      return null;
    } finally {
      if (requestSeq === this._requestSeq) this.loading = false;
    }
  },

  async refreshDiff() {
    const contextId = this._requireContext();
    const binding = this._operationBinding();
    const requestSeq = ++this._diffRequestSeq;
    this._requestSeq += 1;
    this.loading = false;
    const mode = ["working", "staged", "commit", "range"].includes(
      this.diffMode,
    )
      ? this.diffMode
      : "working";
    const request = { context_id: contextId, mode };
    if (mode === "commit") request.commit = String(this.commitRef || "").trim();
    if (mode === "range") {
      request.from_ref = String(this.fromRef || "").trim();
      request.to_ref = String(this.toRef || "").trim();
    }

    const busyToken = this._beginBusy();
    try {
      const rawResponse = await callJsonApi(apiPath("sem_diff"), request);
      if (
        requestSeq !== this._diffRequestSeq
        || !this._operationBindingIsCurrent(binding)
      ) {
        return null;
      }
      const response = requireApiSuccess(
        rawResponse,
        "Semantic diff response was invalid.",
        {
          snapshot: (value) => isSnapshotStatus(
            value,
            { working: mode === "working" },
          ),
        },
      );
      const review = { ...(this.payload?.review || {}) };
      review.snapshot = response.snapshot;
      review.revision = response.snapshot.revision;
      if (mode === "working") review.working = response.snapshot;
      this._applyPayload(
        { ...(this.payload || {}), review },
        { keepSelection: true },
      );
      const refreshBinding = this._operationBinding();
      this.error = "";
      const refreshed = await this.refreshStatus({
        keepSelection: true,
        notify: true,
      });
      if (
        !refreshed
        || requestSeq !== this._diffRequestSeq
        || !this._operationBindingIsCurrent(refreshBinding)
      ) {
        return null;
      }
      return response;
    } catch (error) {
      if (
        requestSeq !== this._diffRequestSeq
        || !this._operationBindingIsCurrent(binding)
      ) {
        return null;
      }
      this._notifyError(error, "Unable to refresh semantic diff.");
      return null;
    } finally {
      this._endBusy(busyToken);
    }
  },

  async selectEntity(entityId) {
    const normalized = String(entityId || "").trim();
    this.selectedEntityId = normalized;
    this._invalidateDerivedState();
    if (!normalized) return null;
    return await this.loadDetail();
  },

  async loadDetail() {
    this._requireContext();
    const binding = this._selectionBinding();
    if (!binding) return null;
    const requestSeq = ++this._detailRequestSeq;
    try {
      const rawResponse = await callJsonApi(apiPath("sem_detail"), {
        context_id: binding.contextId,
        entity_id: binding.entityId,
        revision: binding.requestRevision,
        fingerprint: binding.fingerprint,
      });
      if (
        requestSeq !== this._detailRequestSeq
        || !this._selectionBindingIsCurrent(binding)
      ) {
        return null;
      }
      const response = requireApiSuccess(
        rawResponse,
        "Semantic detail response was invalid.",
        { detail: isBoundedResponseObject },
      );
      this.detail = response.detail;
      this.error = "";
      return this.detail;
    } catch (error) {
      if (
        requestSeq !== this._detailRequestSeq
        || !this._selectionBindingIsCurrent(binding)
      ) {
        return null;
      }
      this.detail = null;
      this._notifyError(error, "Unable to load semantic detail.");
      return null;
    }
  },

  async loadContext() {
    this._requireContext();
    const binding = this._selectionBinding();
    if (!binding) return null;
    const requestSeq = ++this._contextRequestSeq;
    const busyToken = this._beginBusy();
    try {
      const rawResponse = await callJsonApi(apiPath("sem_context"), {
        context_id: binding.contextId,
        entity_id: binding.entityId,
        revision: binding.requestRevision,
        fingerprint: binding.fingerprint,
      });
      if (
        requestSeq !== this._contextRequestSeq
        || !this._selectionBindingIsCurrent(binding)
      ) {
        return null;
      }
      const response = requireApiSuccess(
        rawResponse,
        "Semantic context response was invalid.",
        { context: isBoundedResponseObject },
      );
      this.contextResult = response.context;
      this.error = "";
      return this.contextResult;
    } catch (error) {
      if (
        requestSeq !== this._contextRequestSeq
        || !this._selectionBindingIsCurrent(binding)
      ) {
        return null;
      }
      this.contextResult = null;
      this._notifyError(error, "Unable to load semantic context.");
      return null;
    } finally {
      this._endBusy(busyToken);
    }
  },

  async loadImpact() {
    this._requireContext();
    const binding = this._selectionBinding();
    if (!binding) return null;
    const requestSeq = ++this._impactRequestSeq;
    const busyToken = this._beginBusy();
    try {
      const rawResponse = await callJsonApi(apiPath("sem_impact"), {
        context_id: binding.contextId,
        entity_id: binding.entityId,
        revision: binding.requestRevision,
        fingerprint: binding.fingerprint,
      });
      if (
        requestSeq !== this._impactRequestSeq
        || !this._selectionBindingIsCurrent(binding)
      ) {
        return null;
      }
      const response = requireApiSuccess(
        rawResponse,
        "Semantic impact response was invalid.",
        { impact: isBoundedResponseObject },
      );
      this.impactResult = response.impact;
      this.error = "";
      return this.impactResult;
    } catch (error) {
      if (
        requestSeq !== this._impactRequestSeq
        || !this._selectionBindingIsCurrent(binding)
      ) {
        return null;
      }
      this.impactResult = null;
      this._notifyError(error, "Unable to load semantic impact.");
      return null;
    } finally {
      this._endBusy(busyToken);
    }
  },

  async previewMcp() {
    this._requireContext();
    const binding = this._operationBinding();
    const requestSeq = ++this._mcpRequestSeq;
    this._clearMcpPreview();
    const busyToken = this._beginBusy();
    try {
      const rawResponse = await callJsonApi(apiPath("sem_mcp"), {
        context_id: binding.contextId,
        action: "preview",
      });
      if (
        requestSeq !== this._mcpRequestSeq
        || !this._operationBindingIsCurrent(binding)
      ) {
        return null;
      }
      const response = requireApiSuccess(
        rawResponse,
        "MCP preview response was invalid.",
        {
          preview_token: (value) => (
            typeof value === "string" && value.length > 0
          ),
          entry: isPlainObject,
          confirmation_required: (value) => value === true,
        },
      );
      this.mcpPreview = response;
      this.mcpConfirmed = false;
      this._mcpPreviewBinding = binding;
      this.error = "";
      return response;
    } catch (error) {
      if (
        requestSeq !== this._mcpRequestSeq
        || !this._operationBindingIsCurrent(binding)
      ) {
        return null;
      }
      this._clearMcpPreview();
      this._notifyError(error, "Unable to preview project MCP control.");
      return null;
    } finally {
      this._endBusy(busyToken);
    }
  },

  async enableMcp() {
    this._requireContext();
    const binding = this._operationBinding();
    const previewBinding = this._mcpPreviewBinding;
    if (
      !this.mcpPreview
      || this.mcpConfirmed !== true
      || !previewBinding
      || !this._operationBindingIsCurrent(previewBinding)
    ) {
      const error = new Error(
        "Review and confirm the current local MCP preview first.",
      );
      this._clearMcpPreview({ invalidateRequest: true });
      this._notifyError(error);
      throw error;
    }
    const previewToken = String(this.mcpPreview.preview_token || "");
    const requestSeq = ++this._mcpRequestSeq;
    const busyToken = this._beginBusy();
    try {
      const rawResponse = await callJsonApi(apiPath("sem_mcp"), {
        context_id: binding.contextId,
        action: "enable",
        confirmed: true,
        preview_token: previewToken,
      });
      if (
        requestSeq !== this._mcpRequestSeq
        || !this._operationBindingIsCurrent(binding)
      ) {
        return null;
      }
      const response = requireApiSuccess(
        rawResponse,
        "MCP enable response was invalid.",
        { enabled: (value) => value === true },
      );
      this._clearMcpPreview();
      const refreshed = await this.refreshStatus({
        keepSelection: true,
        notify: true,
      });
      if (
        !refreshed
        || requestSeq !== this._mcpRequestSeq
        || !this._operationBindingIsCurrent(binding)
      ) {
        return null;
      }
      this._notifySuccess("Focused semantic MCP tools enabled.");
      return response;
    } catch (error) {
      if (
        requestSeq !== this._mcpRequestSeq
        || !this._operationBindingIsCurrent(binding)
      ) {
        return null;
      }
      this.mcpConfirmed = false;
      this._notifyError(error, "Unable to enable semantic MCP tools.");
      return null;
    } finally {
      this._endBusy(busyToken);
    }
  },

  async disableMcp() {
    this._requireContext();
    const binding = this._operationBinding();
    const requestSeq = ++this._mcpRequestSeq;
    const busyToken = this._beginBusy();
    try {
      const rawResponse = await callJsonApi(apiPath("sem_mcp"), {
        context_id: binding.contextId,
        action: "disable",
      });
      if (
        requestSeq !== this._mcpRequestSeq
        || !this._operationBindingIsCurrent(binding)
      ) {
        return null;
      }
      const response = requireApiSuccess(
        rawResponse,
        "MCP disable response was invalid.",
        { enabled: (value) => value === false },
      );
      this._clearMcpPreview();
      const refreshed = await this.refreshStatus({
        keepSelection: true,
        notify: true,
      });
      if (
        !refreshed
        || requestSeq !== this._mcpRequestSeq
        || !this._operationBindingIsCurrent(binding)
      ) {
        return null;
      }
      this._notifySuccess("Semantic MCP tools disabled.");
      return response;
    } catch (error) {
      if (
        requestSeq !== this._mcpRequestSeq
        || !this._operationBindingIsCurrent(binding)
      ) {
        return null;
      }
      this._notifyError(error, "Unable to disable semantic MCP tools.");
      return null;
    } finally {
      this._endBusy(busyToken);
    }
  },

  async cancelReview() {
    this._requireContext();
    const binding = this._operationBinding();
    if (
      binding.revision === null
      || !binding.fingerprint
    ) {
      this._notifyError(
        new Error("Refresh the current semantic snapshot before cancelling."),
      );
      return null;
    }
    const busyToken = this._beginBusy();
    try {
      const rawResponse = await callJsonApi(apiPath("sem_review"), {
        context_id: binding.contextId,
        action: "cancel",
        revision: binding.revision,
        fingerprint: binding.fingerprint,
      });
      if (!this._operationBindingIsCurrent(binding)) return null;
      const response = requireApiSuccess(
        rawResponse,
        "Semantic review cancellation response was invalid.",
        { review: isPlainObject },
      );
      const refreshed = await this.refreshStatus({
        keepSelection: true,
        notify: true,
      });
      if (
        !refreshed
        || !this._operationBindingIsCurrent(binding)
      ) {
        return null;
      }
      this._notifySuccess("Current semantic review cancelled.");
      return response;
    } catch (error) {
      if (!this._operationBindingIsCurrent(binding)) return null;
      this._notifyError(error, "Unable to cancel semantic review.");
      return null;
    } finally {
      this._endBusy(busyToken);
    }
  },

  async loadLessons(options = {}) {
    let contextId;
    try {
      contextId = this._requireContext();
    } catch (error) {
      if (options?.notify !== false) this._notifyError(error);
      return null;
    }
    const binding = this._operationBinding();
    const requestSeq = ++this._lessonRequestSeq;
    const busyToken = (
      options?.manageBusy !== false ? this._beginBusy() : null
    );
    try {
      const rawResponse = await callJsonApi(apiPath("sem_lessons"), {
        context_id: binding.contextId,
        action: "list",
      });
      if (
        requestSeq !== this._lessonRequestSeq
        || !this._operationBindingIsCurrent(binding)
      ) {
        return null;
      }
      const response = requireApiSuccess(
        rawResponse,
        "Semantic lesson response was invalid.",
        { lessons: isLessonLists },
      );
      const lessons = lessonLists(response.lessons);
      this.payload = { ...(this.payload || {}), lessons };
      this.error = "";
      return lessons;
    } catch (error) {
      if (
        requestSeq !== this._lessonRequestSeq
        || !this._operationBindingIsCurrent(binding)
      ) {
        return null;
      }
      if (options?.notify !== false) {
        this._notifyError(error, "Unable to load project lessons.");
      } else {
        this.error = plainError(error, "Unable to load project lessons.");
      }
      return null;
    } finally {
      this._endBusy(busyToken);
    }
  },

  async _lessonMutation(action, proposalId, successMessage) {
    this._requireContext();
    const binding = this._operationBinding();
    const busyToken = this._beginBusy();
    try {
      const request = { context_id: binding.contextId, action };
      if (proposalId) request.proposal_id = String(proposalId);
      const rawResponse = await callJsonApi(
        apiPath("sem_lessons"),
        request,
      );
      if (!this._operationBindingIsCurrent(binding)) return null;
      const required = {};
      if (action === "approve") {
        required.lesson = isBoundedResponseObject;
      } else if (action === "forget_all") {
        required.forgotten = (value) => (
          Number.isSafeInteger(value) && value >= 0
        );
      }
      const response = requireApiSuccess(
        rawResponse,
        "Semantic lesson mutation response was invalid.",
        required,
      );
      const refreshed = await this.refreshStatus({
        keepSelection: true,
        notify: true,
      });
      if (
        !refreshed
        || !this._operationBindingIsCurrent(binding)
      ) {
        return null;
      }
      const lessons = await this.loadLessons({
        notify: true,
        manageBusy: false,
      });
      if (
        !lessons
        || !this._operationBindingIsCurrent(binding)
      ) {
        return null;
      }
      this._notifySuccess(successMessage);
      return response;
    } catch (error) {
      if (!this._operationBindingIsCurrent(binding)) return null;
      this._notifyError(error, "Unable to update project lessons.");
      return null;
    } finally {
      this._endBusy(busyToken);
    }
  },

  async approveLesson(proposalId) {
    return await this._lessonMutation(
      "approve",
      proposalId,
      "Project lesson approved.",
    );
  },

  async discardLesson(proposalId) {
    return await this._lessonMutation(
      "discard",
      proposalId,
      "Lesson proposal discarded.",
    );
  },

  async deleteLesson(proposalId) {
    return await this._lessonMutation(
      "delete",
      proposalId,
      "Approved project lesson deleted.",
    );
  },

  async forgetAllLessons() {
    return await this._lessonMutation(
      "forget_all",
      "",
      "All Semantic Review project lessons forgotten.",
    );
  },

  snapshot() {
    return this.payload?.review?.snapshot
      || this.payload?.review?.working
      || null;
  },

  changes() {
    const values = this.snapshot()?.changes;
    return Array.isArray(values) ? values : [];
  },

  summary() {
    const summary = this.snapshot()?.summary;
    const structural = this.changes().filter(
      (change) => change?.structural === true,
    ).length;
    return {
      ...EMPTY_SUMMARY,
      ...(isPlainObject(summary) ? summary : {}),
      structural,
    };
  },

  groupedChanges() {
    const sorted = [...this.changes()].sort((left, right) => {
      const pathOrder = String(left?.file_path || "").localeCompare(
        String(right?.file_path || ""),
      );
      if (pathOrder) return pathOrder;
      const leftLine = Number.isFinite(Number(left?.start_line))
        ? Number(left.start_line)
        : Number.MAX_SAFE_INTEGER;
      const rightLine = Number.isFinite(Number(right?.start_line))
        ? Number(right.start_line)
        : Number.MAX_SAFE_INTEGER;
      if (leftLine !== rightLine) return leftLine - rightLine;
      return String(left?.entity_name || "").localeCompare(
        String(right?.entity_name || ""),
      );
    });
    const groups = new Map();
    for (const change of sorted) {
      const path = String(change?.file_path || "Unknown file");
      if (!groups.has(path)) groups.set(path, []);
      groups.get(path).push(change);
    }
    return Array.from(groups, ([path, changes]) => ({ path, changes }));
  },

  selectedChange() {
    return this.changes().find(
      (change) => change?.entity_id === this.selectedEntityId,
    ) || null;
  },

  pendingLessons() {
    return lessonLists(this.payload?.lessons).pending;
  },

  approvedLessons() {
    return lessonLists(this.payload?.lessons).approved;
  },

  formatResult(value) {
    if (value == null) return "";
    if (!isBoundedJson(value)) {
      return "Result unavailable: response exceeded the display limit.";
    }
    let text;
    try {
      text = JSON.stringify(value, null, 2);
    } catch {
      text = String(value);
    }
    if (typeof text !== "string") text = String(value);
    if (text.length <= MAX_FORMATTED_RESULT_CHARS) return text;
    return `${text.slice(0, MAX_FORMATTED_RESULT_CHARS)}\n…truncated`;
  },
};

export const store = createStore("semReviewLoop", model);
