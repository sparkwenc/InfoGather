import * as api from "./api.js";
import * as ui from "./ui.js";

const pageSize = 24;

const state = {
  offset: 0,
  total: null,
  cursor: null,
  hasMore: false,
  appliedQuery: "",
  loading: false,
  entriesRequest: null,
  refreshController: null,
  refreshInFlight: null,
  refreshOnVisible: false,
  selectedSelectors: new Set(),
  favoredOnly: false,
  unnoticedOnly: false,
  updatedWithinDay: false,
  updatedWithinWeek: false,
  versionIs1: false,
  versionIsNot1: false,
  treeGroups: [],
  lastUndo: null,
  mutating: false,
  insPollTimer: null,
  insStatusPromise: null,
  insStatusController: null,
  insWasRunning: false,
  insLastCompletion: null,
  insRunStarting: false,
  treeRefreshTimer: null
};

const metaEl = document.getElementById("meta");
const listEl = document.getElementById("list");
const moreEl = document.getElementById("more");
const qEl = document.getElementById("q");
const searchForm = document.getElementById("search-form");
const undoBtn = document.getElementById("undo-btn");
const insBtn = document.getElementById("ins-btn");
const insPanel = document.getElementById("ins-panel");
const insText = document.getElementById("ins-text");
const insPercent = document.getElementById("ins-percent");
const insProgress = document.getElementById("ins-progress");
const insLog = document.getElementById("ins-log");
const insLogShell = document.getElementById("ins-log-shell");
const insLogCount = document.getElementById("ins-log-count");

const treeRootEl = document.getElementById("tree-root");
const treeListEl = document.getElementById("tree-list");
const clearTagsEl = document.getElementById("clear-tags");

const favoredBtn = document.getElementById("favored-btn");
const unnoticedBtn = document.getElementById("unnoticed-btn");
const dayBtn = document.getElementById("day-btn");
const weekBtn = document.getElementById("week-btn");
const versionBtn = document.getElementById("version-btn");
const versionNotBtn = document.getElementById("version-not-btn");
const resultsHeading = document.getElementById("results-heading");

const insElements = {
  insPanel, insBtn, insText, insPercent, insProgress,
  insLog, insLogShell, insLogCount
};

function renderUndo() {
  undoBtn.textContent = state.lastUndo?.label || "撤销上一步";
  undoBtn.disabled = !state.lastUndo;
  undoBtn.setAttribute("aria-busy", String(state.mutating));
}

function setLastUndo(action) {
  state.lastUndo = action;
  renderUndo();
}

function beginMutation() {
  if (state.mutating) return false;
  state.mutating = true;
  renderUndo();
  return true;
}

function endMutation() {
  state.mutating = false;
  if (!state.loading) listEl.removeAttribute("aria-busy");
  renderUndo();
}

function findRenderedEntry(item) {
  return Array.from(listEl.querySelectorAll(".card")).find((card) => (
    card.dataset.sourceType === String(item.srce_ty || "")
    && card.dataset.sourceId === String(item.srce_id || "")
  )) || null;
}

function entryMatchesMutableFilters(item) {
  return (
    (!state.favoredOnly || Number(item.favored || 0) === 1)
    && (!state.unnoticedOnly || Number(item.noticed || 0) === 0)
  );
}

function removeRenderedEntry(card) {
  card.remove();
  state.offset = Math.max(0, state.offset - 1);
  if (Number.isFinite(state.total)) state.total = Math.max(0, state.total - 1);
  if (!state.hasMore && !listEl.querySelector(".card")) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "没有匹配的条目。";
    listEl.replaceChildren(empty);
  }
  ui.setMeta(metaEl, state);
}

function updateRenderedEntry(item, card) {
  card._liveItem = item;
  if (!entryMatchesMutableFilters(item)) {
    removeRenderedEntry(card);
    return false;
  }

  const title = card.querySelector(".title")?.textContent || "该条目";
  const favored = Number(item.favored || 0) === 1;
  const favoredBtn = card.querySelector('[data-action="favored"]');
  favoredBtn.classList.toggle("on", favored);
  favoredBtn.dataset.favored = favored ? "1" : "0";
  favoredBtn.querySelector("span").textContent = favored ? "已收藏" : "收藏";
  favoredBtn.setAttribute("aria-pressed", String(favored));
  favoredBtn.setAttribute(
    "aria-label",
    favored ? `取消收藏《${title}》` : `收藏《${title}》`
  );

  const noticed = Number(item.noticed || 0) === 1;
  const noticedBtn = card.querySelector('[data-action="noticed"]');
  noticedBtn.classList.toggle("on", noticed);
  noticedBtn.dataset.noticed = noticed ? "1" : "0";
  noticedBtn.querySelector("span").textContent = noticed ? "已读" : "未读";
  noticedBtn.setAttribute("aria-pressed", String(noticed));
  noticedBtn.setAttribute(
    "aria-label",
    noticed ? `标记《${title}》为未读` : `标记《${title}》为已读`
  );
  return true;
}

function scheduleTreeRefresh() {
  clearTimeout(state.treeRefreshTimer);
  state.treeRefreshTimer = setTimeout(() => {
    state.treeRefreshTimer = null;
    if (!document.hidden) void loadTagTree();
  }, 250);
}

function stopInsPolling() {
  clearTimeout(state.insPollTimer);
  state.insPollTimer = null;
}

function startInsPolling(delay = 900) {
  if (state.insPollTimer || document.hidden) return;
  state.insPollTimer = setTimeout(() => {
    state.insPollTimer = null;
    void pollInsStatus();
  }, delay);
}

function pauseInsPolling() {
  stopInsPolling();
  state.insStatusController?.abort();
  if (state.refreshInFlight || state.entriesRequest) {
    state.refreshController?.abort();
    state.refreshOnVisible = true;
  }
}

function renderTree() {
  clearTagsEl.disabled = state.selectedSelectors.size === 0;
  ui.renderTree(treeListEl, state, {
    onSelectorChange(selector, checked) {
      if (checked) {
        state.selectedSelectors.add(selector);
      } else {
        state.selectedSelectors.delete(selector);
      }
      clearTagsEl.disabled = state.selectedSelectors.size === 0;
      ui.setMeta(metaEl, state);
      void refreshFilteredView();
    }
  });
}

function buildFilterParams() {
  const params = new URLSearchParams({
    q: state.appliedQuery
  });
  if (state.favoredOnly) params.set("favored", "1");
  if (state.unnoticedOnly) params.set("unnoticed", "1");
  if (state.updatedWithinDay) params.set("updated_within_day", "1");
  if (state.updatedWithinWeek) params.set("updated_within_week", "1");
  if (state.versionIs1) params.set("version_is_1", "1");
  if (state.versionIsNot1) params.set("version_is_not_1", "1");
  for (const selector of state.selectedSelectors) {
    params.append("selectors", selector);
  }
  return params;
}

async function refreshFilteredView() {
  clearTimeout(state.treeRefreshTimer);
  state.treeRefreshTimer = null;
  state.refreshController?.abort();
  const controller = new AbortController();
  state.refreshController = controller;
  state.refreshInFlight = controller;
  try {
    const results = await Promise.all([
      loadTagTree(controller.signal),
      fetchEntries({ reset: true, signal: controller.signal })
    ]);
    return !controller.signal.aborted && results.every(Boolean);
  } finally {
    if (state.refreshInFlight === controller) state.refreshInFlight = null;
  }
}

function renderEntries(items, { reset }) {
  if (reset) {
    const activeElement = document.activeElement;
    const activeCard = listEl.contains(activeElement)
      ? activeElement.closest(".card")
      : null;
    const focusTarget = activeCard && activeElement.dataset.action
      ? {
          srceTy: activeCard.dataset.sourceType,
          srceId: activeCard.dataset.sourceId,
          action: activeElement.dataset.action
        }
      : null;
    const nodes = [];
    if (!items.length) {
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = "没有匹配的条目。";
      nodes.push(p);
    } else {
      nodes.push(...items.map(ui.makeCard));
    }
    listEl.replaceChildren(...nodes);
    if (focusTarget) {
      const card = findRenderedEntry({
        srce_ty: focusTarget.srceTy,
        srce_id: focusTarget.srceId
      });
      card?.querySelector(`[data-action="${focusTarget.action}"]`)?.focus();
    }
    return;
  }

  listEl.querySelector(".empty")?.remove();
  listEl.append(...items.map(ui.makeCard));
}

async function applyInsJob(job, { refreshCompleted = true } = {}) {
  state.insWasRunning = job.state === "running";
  ui.renderInsJob(insElements, job);
  let retryRefresh = false;

  const completion = job.state === "succeeded"
    ? (job.ended_at || job.started_at)
    : null;
  if (completion && completion !== state.insLastCompletion) {
    if (refreshCompleted && !document.hidden) {
      if (await refreshFilteredView()) {
        state.insLastCompletion = completion;
      } else {
        retryRefresh = true;
      }
    }
  }

  if (!state.insWasRunning) {
    stopInsPolling();
    if (retryRefresh) startInsPolling(2000);
  }
}

async function pollInsStatus({ refreshCompleted = true } = {}) {
  if (state.insRunStarting || document.hidden) return;
  if (state.insStatusPromise) {
    try {
      await state.insStatusPromise;
    } catch (err) {
      console.error(err);
    }
    return;
  }

  const controller = new AbortController();
  state.insStatusController = controller;
  const request = (async () => {
    const payload = await api.getInsStatus(controller.signal);
    if (controller.signal.aborted) return;
    const job = payload.job || {};
    await applyInsJob(job, { refreshCompleted });
  })();
  state.insStatusPromise = request;
  try {
    await request;
  } catch (err) {
    if (err.name !== "AbortError") console.error(err);
  } finally {
    if (state.insStatusPromise === request) state.insStatusPromise = null;
    if (state.insStatusController === controller) {
      state.insStatusController = null;
    }
    if (state.insWasRunning) startInsPolling();
  }
}

async function runIns() {
  if (state.insRunStarting) return;
  state.insRunStarting = true;
  insBtn.disabled = true;
  try {
    stopInsPolling();
    if (state.insStatusPromise) {
      try {
        await state.insStatusPromise;
      } catch (err) {
        if (err.name !== "AbortError") console.error(err);
      }
    }
    stopInsPolling();
    insBtn.disabled = true;
    const payload = await api.runIns();
    const job = payload.job || {};
    stopInsPolling();
    await applyInsJob(job);
    state.insRunStarting = false;
    if (state.insWasRunning) await pollInsStatus();
  } catch (err) {
    insBtn.disabled = false;
    console.error(err);
    window.alert("启动拉取失败，请稍后重试。");
  } finally {
    state.insRunStarting = false;
  }
}

async function updateFlag(item, field, value, btnEl) {
  if (!beginMutation()) return;
  const previous = Number(item[field] || 0);
  const previousRevision = Number(item.state_rev || 0);
  const restoreFocus = btnEl.matches(":focus-visible");
  const update = field === "favored" ? api.setFavored : api.setNoticed;
  const card = btnEl.closest(".card");
  let remainsVisible = true;
  let succeeded = false;
  btnEl.disabled = true;
  try {
    const response = await update(
      item.srce_ty, item.srce_id, value, previous, previousRevision
    );
    item[field] = value;
    item.state_rev = response.state_rev;
    remainsVisible = updateRenderedEntry(item, card);
    setLastUndo({
      type: field,
      srceTy: item.srce_ty,
      srceId: item.srce_id,
      previous,
      current: value,
      currentRevision: response.state_rev,
      label: field === "favored"
        ? (value === 1 ? "撤销收藏" : "撤销取消收藏")
        : (value === 1 ? "撤销标为已读" : "撤销标为未读")
    });
    if (
      (field === "favored" && state.favoredOnly)
      || (field === "noticed" && state.unnoticedOnly)
    ) {
      scheduleTreeRefresh();
    }
    succeeded = true;
  } catch (err) {
    console.error(err);
    window.alert(field === "favored"
      ? "收藏更新失败，请稍后重试。"
      : "已读状态更新失败，请稍后重试。");
    await refreshFilteredView();
  } finally {
    if (btnEl.isConnected) btnEl.disabled = false;
    endMutation();
    if (restoreFocus && (!succeeded || !remainsVisible)) {
      (succeeded ? undoBtn : resultsHeading).focus();
    }
  }
}

async function removeEntry(item, btnEl) {
  if (state.mutating) return;
  const ok = window.confirm(
    `确认从本地列表移除 ${item.srce_ty}:${item.srce_id}？再次拉取时可能恢复。`
  );
  if (!ok) return;

  if (!beginMutation()) return;
  const restoreFocus = btnEl.matches(":focus-visible");
  try {
    const response = await api.removeEntry(item.srce_ty, item.srce_id);
    setLastUndo({
      type: "remove",
      undoToken: response.undo_token,
      label: "撤销移除"
    });
    await refreshFilteredView();
  } catch (err) {
    console.error(err);
    window.alert("移除失败，请稍后重试。");
    await refreshFilteredView();
  } finally {
    endMutation();
    if (restoreFocus) resultsHeading.focus();
  }
}

async function undoLastAction() {
  const action = state.lastUndo;
  const restoreFocus = undoBtn.matches(":focus-visible");
  if (!action || !beginMutation()) return;

  state.lastUndo = null;
  renderUndo();
  let succeeded = false;
  try {
    if (action.type === "favored") {
      await api.setFavored(
        action.srceTy,
        action.srceId,
        action.previous,
        action.current,
        action.currentRevision
      );
    } else if (action.type === "noticed") {
      await api.setNoticed(
        action.srceTy,
        action.srceId,
        action.previous,
        action.current,
        action.currentRevision
      );
    } else if (action.type === "remove") {
      const response = await api.restoreEntry(action.undoToken);
      if (response.already_present) {
        window.alert("条目已存在于列表中，无需恢复。");
      }
    }
    await refreshFilteredView();
    succeeded = true;
  } catch (err) {
    if (![404, 409].includes(err.status) && !state.lastUndo) {
      state.lastUndo = action;
    }
    console.error(err);
    window.alert(
      [404, 409].includes(err.status)
        ? "该操作已无法撤销。"
        : "撤销失败，请稍后重试。"
    );
    await refreshFilteredView();
  } finally {
    endMutation();
    if (restoreFocus) {
      if (!succeeded && state.lastUndo) undoBtn.focus();
      else resultsHeading.focus();
    }
  }
}

async function loadTagTree(signal) {
  try {
    const params = buildFilterParams();
    const payload = await api.getTagTree(params, signal);
    if (signal?.aborted) return;
    const root = payload.root || { name: "配置源", group_count: 0, source_count: 0, count: 0 };
    treeRootEl.textContent = `${root.name}（${root.group_count} 类 / ${root.source_count} 源 / ${root.count} 条）`;
    state.treeGroups = Array.isArray(payload.groups) ? payload.groups : [];
    renderTree();
    return true;
  } catch (err) {
    if (signal?.aborted) return false;
    treeRootEl.textContent = "配置源 (加载失败)";
    treeListEl.innerHTML = '<li class="tree-item tree-empty">无法读取来源列表</li>';
    console.error(err);
    return false;
  }
}

async function fetchEntries({ reset = false, signal } = {}) {
  if (!reset && state.entriesRequest) return false;
  const request = {};
  let succeeded = false;
  state.entriesRequest = request;
  state.loading = true;
  listEl.setAttribute("aria-busy", "true");
  moreEl.disabled = true;

  const params = buildFilterParams();
  params.set("limit", String(pageSize));
  params.set("include_total", reset ? "1" : "0");
  if (!reset && state.cursor) params.set("cursor", state.cursor);

  try {
    const payload = await api.getEntries(params, signal);
    if (signal?.aborted || state.entriesRequest !== request) return;
    const items = Array.isArray(payload.items) ? payload.items : [];

    renderEntries(items, { reset });
    if (reset) {
      state.offset = items.length;
      state.total = Number(payload.total || 0);
    } else {
      state.offset += items.length;
    }
    state.cursor = typeof payload.next_cursor === "string"
      ? payload.next_cursor
      : null;
    state.hasMore = payload.has_more === true;
    succeeded = true;
  } catch (err) {
    if (!signal?.aborted && state.entriesRequest === request) {
      if (reset || !listEl.children.length) {
        state.offset = 0;
        state.total = null;
        state.cursor = null;
        state.hasMore = false;
        listEl.innerHTML = '<p class="empty">读取失败，请确认本地服务已启动。</p>';
      }
      console.error(err);
    }
  } finally {
    if (state.entriesRequest === request) {
      ui.setMeta(metaEl, state);
      moreEl.hidden = !state.hasMore;
      state.loading = false;
      state.entriesRequest = null;
      if (!state.mutating) listEl.removeAttribute("aria-busy");
      moreEl.disabled = false;
    }
  }
  return succeeded;
}

insBtn.addEventListener("click", runIns);
undoBtn.addEventListener("click", undoLastAction);
listEl.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  const card = button?.closest(".card");
  const item = card?._liveItem;
  if (!button || !item) return;
  if (button.dataset.action === "favored") {
    const current = Number(button.dataset.favored || 0);
    void updateFlag(item, "favored", current === 1 ? 0 : 1, button);
  } else if (button.dataset.action === "noticed") {
    const current = Number(button.dataset.noticed || 0);
    void updateFlag(item, "noticed", current === 1 ? 0 : 1, button);
  } else if (button.dataset.action === "remove") {
    void removeEntry(item, button);
  }
});

favoredBtn.addEventListener("click", () => {
  state.favoredOnly = !state.favoredOnly;
  ui.setToggle(favoredBtn, state.favoredOnly);
  void refreshFilteredView();
});

unnoticedBtn.addEventListener("click", () => {
  state.unnoticedOnly = !state.unnoticedOnly;
  ui.setToggle(unnoticedBtn, state.unnoticedOnly);
  void refreshFilteredView();
});

dayBtn.addEventListener("click", () => {
  const next = !state.updatedWithinDay;
  state.updatedWithinDay = next;
  if (next) {
    state.updatedWithinWeek = false;
    ui.setToggle(weekBtn, false);
  }
  ui.setToggle(dayBtn, state.updatedWithinDay);
  void refreshFilteredView();
});

weekBtn.addEventListener("click", () => {
  const next = !state.updatedWithinWeek;
  state.updatedWithinWeek = next;
  if (next) {
    state.updatedWithinDay = false;
    ui.setToggle(dayBtn, false);
  }
  ui.setToggle(weekBtn, state.updatedWithinWeek);
  void refreshFilteredView();
});

versionBtn.addEventListener("click", () => {
  const next = !state.versionIs1;
  state.versionIs1 = next;
  if (next) {
    state.versionIsNot1 = false;
    ui.setToggle(versionNotBtn, false);
  }
  ui.setToggle(versionBtn, state.versionIs1);
  void refreshFilteredView();
});

versionNotBtn.addEventListener("click", () => {
  const next = !state.versionIsNot1;
  state.versionIsNot1 = next;
  if (next) {
    state.versionIs1 = false;
    ui.setToggle(versionBtn, false);
  }
  ui.setToggle(versionNotBtn, state.versionIsNot1);
  void refreshFilteredView();
});

clearTagsEl.addEventListener("click", () => {
  if (!state.selectedSelectors.size) return;
  state.selectedSelectors.clear();
  renderTree();
  void refreshFilteredView();
});

searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  state.appliedQuery = qEl.value.trim();
  void refreshFilteredView();
});

moreEl.addEventListener("click", () => {
  void fetchEntries({
    reset: false,
    signal: state.refreshController?.signal
  });
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    pauseInsPolling();
  } else {
    void (async () => {
      const refreshOnVisible = state.refreshOnVisible;
      const completionBeforePoll = state.insLastCompletion;
      state.refreshOnVisible = false;
      if (state.insStatusPromise) await pollInsStatus();
      stopInsPolling();
      await pollInsStatus();
      if (document.hidden) {
        state.refreshOnVisible ||= refreshOnVisible;
        return;
      }
      if (
        refreshOnVisible
        && state.insLastCompletion === completionBeforePoll
      ) {
        await refreshFilteredView();
      }
    })();
  }
});

void (async () => {
  await pollInsStatus({ refreshCompleted: false });
  if (document.hidden) {
    state.refreshOnVisible = true;
    return;
  }
  if (!await refreshFilteredView()) startInsPolling(2000);
})();
