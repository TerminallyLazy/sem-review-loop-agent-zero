import { store as semReviewStore } from "/plugins/sem_review_loop/webui/sem-review-store.js";

function waitForPanel(timeoutMs = 3000) {
  const selector = '[data-surface-id="sem_review_loop"] .sem-review-panel';
  const found = document.querySelector(selector);
  if (found) {
    return {
      promise: Promise.resolve(found),
      cancel() {},
    };
  }

  let observer = null;
  let timeout = null;
  let resolveWait = null;
  let settled = false;
  const finish = (element) => {
    if (settled) return;
    settled = true;
    if (timeout !== null) globalThis.clearTimeout(timeout);
    observer?.disconnect();
    resolveWait?.(element);
  };
  const promise = new Promise((resolve) => {
    resolveWait = resolve;
    observer = new MutationObserver(() => {
      const element = document.querySelector(selector);
      if (!element) return;
      finish(element);
    });
    timeout = globalThis.setTimeout(() => {
      finish(document.querySelector(selector));
    }, timeoutMs);
    observer.observe(document.body, { childList: true, subtree: true });
  });
  return {
    promise,
    cancel() {
      finish(null);
    },
  };
}

export default async function registerSemReviewSurface(surfaces) {
  let openGeneration = 0;
  let pendingPanelWait = null;
  surfaces.registerSurface({
    id: "sem_review_loop",
    title: "Semantic Review",
    icon: "difference",
    order: 40,
    modalPath: "/plugins/sem_review_loop/webui/main.html",
    async open(payload = {}) {
      const generation = ++openGeneration;
      pendingPanelWait?.cancel();
      const panelWait = waitForPanel();
      pendingPanelWait = panelWait;
      const panel = await panelWait.promise;
      if (pendingPanelWait === panelWait) pendingPanelWait = null;
      if (generation !== openGeneration) return false;
      if (!panel) {
        throw new Error("Semantic Review surface did not mount.");
      }
      const mounted = await semReviewStore.onMount(
        panel,
        { mode: "canvas" },
      );
      if (generation !== openGeneration || mounted !== true) return false;
      const opened = await semReviewStore.onOpen(payload);
      if (generation !== openGeneration) return false;
      return opened === true;
    },
    async close() {
      openGeneration += 1;
      pendingPanelWait?.cancel();
      pendingPanelWait = null;
      semReviewStore.cleanup();
      return true;
    },
  });
}
