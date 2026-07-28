const pageSize = 24;

const api = window.InfoAPI;
const ui = window.InfoUI;
const state = window.InfoState.createState();

const metaEl = document.getElementById("meta");
const listEl = document.getElementById("list");
const moreEl = document.getElementById("more");
const qEl = document.getElementById("q");
const searchEl = document.getElementById("search");
const undoBtn = document.getElementById("undo-btn");
const insBtn = document.getElementById("ins-btn");
const insPanel = document.getElementById("ins-panel");
const insText = document.getElementById("ins-text");
const insPercent = document.getElementById("ins-percent");
const insBar = document.getElementById("ins-bar");
const insLog = document.getElementById("ins-log");

const treeRootEl = document.getElementById("tree-root");
const treeListEl = document.getElementById("tree-list");
const clearTagsEl = document.getElementById("clear-tags");

const favoredBtn = document.getElementById("favored-btn");
const unnoticedBtn = document.getElementById("unnoticed-btn");
const dayBtn = document.getElementById("day-btn");
const weekBtn = document.getElementById("week-btn");
const versionBtn = document.getElementById("version-btn");
const versionNotBtn = document.getElementById("version-not-btn");

function insElements() {
  return { insPanel, insBtn, insText, insPercent, insBar, insLog };
}

function renderUndo() {
  undoBtn.textContent = state.lastUndo?.label || "撤销上一步";
  undoBtn.disabled = !state.lastUndo || state.mutating;
}

function setLastUndo(action) {
  state.lastUndo = action;
  renderUndo();
}

function beginMutation() {
  if (state.mutating) return false;
  state.mutating = true;
  listEl.setAttribute("aria-busy", "true");
  listEl.inert = true;
  renderUndo();
  return true;
}

function endMutation() {
  state.mutating = false;
  listEl.removeAttribute("aria-busy");
  listEl.inert = false;
  renderUndo();
}

function scheduleMutationRefresh() {
  if (state.mutationRefreshTimer) {
    clearTimeout(state.mutationRefreshTimer);
  }
  state.entriesGeneration += 1;
  state.treeGeneration += 1;
  const entriesGeneration = state.entriesGeneration;
  const treeGeneration = state.treeGeneration;
  state.mutationRefreshTimer = setTimeout(() => {
    state.mutationRefreshTimer = null;
    if (
      entriesGeneration !== state.entriesGeneration
      || treeGeneration !== state.treeGeneration
    ) {
      return;
    }
    void refreshFilteredView();
  }, 80);
}

function findRenderedEntry(item) {
  return Array.from(listEl.querySelectorAll(".card")).find((card) => (
    card.dataset.sourceType === String(item.srce_ty || "")
    && card.dataset.sourceId === String(item.srce_id || "")
  )) || null;
}

function removeRenderedCard(card, item = null) {
  if (!card?.isConnected && item) card = findRenderedEntry(item);
  if (!card?.isConnected) return;
  card.remove();
  state.offset = Math.max(0, state.offset - 1);
  state.total = Math.max(0, state.total - 1);
  ui.setMeta(metaEl, state);
  ui.setMoreVisible(moreEl, state);
}

function entryMatchesMutableFilters(item) {
  return (
    (!state.renderedFavoredOnly || Number(item.favored || 0) === 1)
    && (!state.renderedUnnoticedOnly || Number(item.noticed || 0) === 0)
  );
}

function updateRenderedEntry(item, card = findRenderedEntry(item)) {
  if (!card?.isConnected) card = findRenderedEntry(item);
  if (!card?.isConnected) return;
  if (!entryMatchesMutableFilters(item)) {
    removeRenderedCard(card, item);
    return;
  }
  card.replaceWith(makeEntryCard(item));
}

function prependRenderedEntry(item) {
  const existing = findRenderedEntry(item);
  if (existing) {
    existing.replaceWith(makeEntryCard(item));
    return;
  }
  const empty = listEl.querySelector(".empty");
  if (empty) empty.remove();
  listEl.prepend(makeEntryCard(item));
  state.offset += 1;
  state.total += 1;
  ui.setMeta(metaEl, state);
  ui.setMoreVisible(moreEl, state);
}

function stopInsPolling() {
  if (state.insPollTimer) {
    clearInterval(state.insPollTimer);
    state.insPollTimer = null;
  }
}

function startInsPolling() {
  if (state.insPollTimer) return;
  state.insPollTimer = setInterval(() => {
    void pollInsStatus();
  }, 900);
}

function renderTree() {
  ui.renderTree(treeListEl, state, {
    onToggleGroup(groupKey) {
      if (state.collapsedGroups.has(groupKey)) {
        state.collapsedGroups.delete(groupKey);
      } else {
        state.collapsedGroups.add(groupKey);
      }
      renderTree();
    },
    onSelectorChange(selector, checked) {
      if (checked) {
        state.selectedSelectors.add(selector);
      } else {
        state.selectedSelectors.delete(selector);
      }
      void refreshFilteredView();
    }
  });
}

function buildFilterParams() {
  const params = new URLSearchParams({
    q: qEl.value.trim()
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
  await Promise.all([
    loadTagTree(),
    fetchEntries({ reset: true })
  ]);
}

function makeEntryCard(item) {
  const card = ui.makeCard(item, {
    onToggleFavored: updateFavored,
    onToggleNoticed: updateNoticed,
    onRemove: removeEntry
  });
  ui.renderMath(card);
  return card;
}

function renderEntries(items, { reset }) {
  if (reset) {
    const fragment = document.createDocumentFragment();
    if (!items.length) {
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = "没有匹配的条目。";
      fragment.appendChild(p);
    } else {
      items.forEach((item) => {
        fragment.appendChild(makeEntryCard(item));
      });
    }
    listEl.replaceChildren(fragment);
    return;
  }

  const fragment = document.createDocumentFragment();
  items.forEach((item) => {
    fragment.appendChild(makeEntryCard(item));
  });
  listEl.appendChild(fragment);
}

async function pollInsStatus() {
  if (state.insStatusLoading) return;
  state.insStatusLoading = true;

  try {
    const payload = await api.getInsStatus();
    const job = payload.job || {};

    const wasRunning = state.insWasRunning;
    state.insWasRunning = job.state === "running";
    ui.renderInsJob(insElements(), job);

    if (job.state === "running") {
      startInsPolling();
    }

    if (wasRunning && job.state === "succeeded") {
      await loadTagTree();
      await fetchEntries({ reset: true });
    }

    if (job.state !== "running") {
      stopInsPolling();
    }
  } catch (err) {
    console.error(err);
  } finally {
    state.insStatusLoading = false;
  }
}

async function runIns() {
  insBtn.disabled = true;
  try {
    const { payload } = await api.runIns();
    const job = payload.job || {};
    ui.renderInsJob(insElements(), job);
    state.insWasRunning = true;
    startInsPolling();
    await pollInsStatus();
  } catch (err) {
    insBtn.disabled = false;
    console.error(err);
    window.alert("启动拉取失败，请稍后重试。");
  }
}

async function updateFavored(item, favored, btnEl) {
  if (!beginMutation()) return;
  const previous = Number(item.favored || 0);
  const previousRevision = Number(item.state_rev || 0);
  const filterSignature = state.renderedFilterSignature;
  const card = btnEl.closest(".card");
  btnEl.disabled = true;
  try {
    const response = await api.setFavored(
      item.srce_ty, item.srce_id, favored, previous, previousRevision
    );
    item.favored = favored;
    item.state_rev = response.state_rev;
    updateRenderedEntry(item, card);
    setLastUndo({
      type: "favored",
      srceTy: item.srce_ty,
      srceId: item.srce_id,
      previous: previous,
      current: favored,
      currentRevision: previousRevision + 1,
      item: item,
      filterSignature: filterSignature,
      label: favored === 1 ? "撤销收藏" : "撤销取消收藏"
    });
    scheduleMutationRefresh();
  } catch (err) {
    console.error(err);
    window.alert("收藏更新失败，请稍后重试。");
    scheduleMutationRefresh();
  } finally {
    btnEl.disabled = false;
    endMutation();
  }
}

async function updateNoticed(item, noticed, btnEl) {
  if (!beginMutation()) return;
  const previous = Number(item.noticed || 0);
  const previousRevision = Number(item.state_rev || 0);
  const filterSignature = state.renderedFilterSignature;
  const card = btnEl.closest(".card");
  btnEl.disabled = true;
  try {
    const response = await api.setNoticed(
      item.srce_ty, item.srce_id, noticed, previous, previousRevision
    );
    item.noticed = noticed;
    item.state_rev = response.state_rev;
    updateRenderedEntry(item, card);
    setLastUndo({
      type: "noticed",
      srceTy: item.srce_ty,
      srceId: item.srce_id,
      previous: previous,
      current: noticed,
      currentRevision: previousRevision + 1,
      item: item,
      filterSignature: filterSignature,
      label: noticed === 1 ? "撤销标为已读" : "撤销标为未读"
    });
    scheduleMutationRefresh();
  } catch (err) {
    console.error(err);
    window.alert("已读状态更新失败，请稍后重试。");
    scheduleMutationRefresh();
  } finally {
    btnEl.disabled = false;
    endMutation();
  }
}

async function removeEntry(item, btnEl) {
  const ok = window.confirm(
    `确认从本地列表移除 ${item.srce_ty}:${item.srce_id}？再次拉取时可能恢复。`
  );
  if (!ok) return;

  if (!beginMutation()) return;
  const card = btnEl.closest(".card");
  const filterSignature = state.renderedFilterSignature;
  btnEl.disabled = true;
  try {
    const response = await api.removeEntry(item.srce_ty, item.srce_id);
    removeRenderedCard(card, item);
    const responseEntry = response.entry;
    const canRestoreOptimistically = (
      responseEntry
      && responseEntry.srce_ty === item.srce_ty
      && responseEntry.srce_id === item.srce_id
      && Number(responseEntry.state_rev) === Number(item.state_rev || 0) + 1
    );
    setLastUndo({
      type: "remove",
      undoToken: response.undo_token,
      entry: canRestoreOptimistically ? responseEntry : null,
      filterSignature: filterSignature,
      label: "撤销移除"
    });
    scheduleMutationRefresh();
  } catch (err) {
    console.error(err);
    window.alert("移除失败，请稍后重试。");
    scheduleMutationRefresh();
  } finally {
    btnEl.disabled = false;
    endMutation();
  }
}

async function undoLastAction() {
  const action = state.lastUndo;
  if (!action || !beginMutation()) return;

  state.lastUndo = null;
  renderUndo();
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
        window.alert("条目已重新存在，可能因当前筛选条件未显示。");
      } else if (
        action.entry
        && state.renderedViewValid
        && action.filterSignature === state.renderedFilterSignature
      ) {
        prependRenderedEntry(action.entry);
      }
    }
    if (action.item) {
      action.item[action.type] = action.previous;
      action.item.state_rev = action.currentRevision + 1;
      const card = findRenderedEntry(action.item);
      if (card) {
        updateRenderedEntry(action.item, card);
      } else if (
        state.renderedViewValid
        && action.filterSignature === state.renderedFilterSignature
        && entryMatchesMutableFilters(action.item)
      ) {
        prependRenderedEntry(action.item);
      }
    }
    scheduleMutationRefresh();
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
    scheduleMutationRefresh();
  } finally {
    endMutation();
  }
}

async function loadTagTree() {
  const generation = ++state.treeGeneration;
  try {
    const payload = await api.getTagTree(buildFilterParams());
    if (generation !== state.treeGeneration) return;
    const root = payload.root || { name: "配置源", group_count: 0, source_count: 0, count: 0 };
    treeRootEl.textContent = `${root.name}（${root.group_count} 类 / ${root.source_count} 源 / ${root.count} 条）`;
    state.treeGroups = Array.isArray(payload.groups) ? payload.groups : [];
    renderTree();
  } catch (err) {
    if (generation !== state.treeGeneration) return;
    treeRootEl.textContent = "配置源 (加载失败)";
    treeListEl.innerHTML = '<li class="tree-item"><label>无法读取源列表</label></li>';
    console.error(err);
  }
}

async function fetchEntries({ reset = false } = {}) {
  if (reset) state.entriesGeneration += 1;
  const generation = state.entriesGeneration;
  if (state.loading) {
    if (reset) state.pendingReset = true;
    return;
  }
  state.loading = true;
  moreEl.disabled = true;
  const queryOffset = reset ? 0 : state.offset;

  const params = buildFilterParams();
  const filterSignature = params.toString();
  params.set("limit", String(pageSize));
  params.set("offset", String(queryOffset));

  try {
    const payload = await api.getEntries(params);
    if (generation !== state.entriesGeneration) return;
    const items = Array.isArray(payload.items) ? payload.items : [];
    const nextTotal = Number(payload.total || 0);

    renderEntries(items, { reset });
    if (reset) {
      state.offset = items.length;
      state.total = nextTotal;
      state.renderedFilterSignature = filterSignature;
      state.renderedFavoredOnly = params.get("favored") === "1";
      state.renderedUnnoticedOnly = params.get("unnoticed") === "1";
      state.renderedViewValid = true;
    } else {
      state.offset += items.length;
      state.total = nextTotal;
    }
  } catch (err) {
    if (generation !== state.entriesGeneration) return;
    if (reset || !listEl.children.length) {
      state.offset = 0;
      state.total = 0;
      if (reset) state.renderedViewValid = false;
      listEl.innerHTML = '<p class="empty">读取失败，请确认本地服务已启动。</p>';
    }
    console.error(err);
  } finally {
    ui.setMeta(metaEl, state);
    ui.setMoreVisible(moreEl, state);
    state.loading = false;
    moreEl.disabled = false;
    if (state.pendingReset) {
      state.pendingReset = false;
      void fetchEntries({ reset: true });
    }
  }
}

insBtn.addEventListener("click", runIns);
undoBtn.addEventListener("click", undoLastAction);

favoredBtn.addEventListener("click", () => {
  state.favoredOnly = !state.favoredOnly;
  ui.setToggle(favoredBtn, state.favoredOnly);
  void refreshFilteredView();
});

if (unnoticedBtn) {
  unnoticedBtn.addEventListener("click", () => {
    state.unnoticedOnly = !state.unnoticedOnly;
    ui.setToggle(unnoticedBtn, state.unnoticedOnly);
    void refreshFilteredView();
  });
}

dayBtn.addEventListener("click", () => {
  state.updatedWithinDay = !state.updatedWithinDay;
  ui.setToggle(dayBtn, state.updatedWithinDay);
  void refreshFilteredView();
});

weekBtn.addEventListener("click", () => {
  state.updatedWithinWeek = !state.updatedWithinWeek;
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

searchEl.addEventListener("click", () => {
  void refreshFilteredView();
});

qEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    void refreshFilteredView();
  }
});

moreEl.addEventListener("click", () => {
  void fetchEntries({ reset: false });
});

async function bootstrap() {
  renderUndo();
  ui.setToggle(favoredBtn, false);
  if (unnoticedBtn) ui.setToggle(unnoticedBtn, false);
  ui.setToggle(dayBtn, false);
  ui.setToggle(weekBtn, false);
  ui.setToggle(versionBtn, false);
  ui.setToggle(versionNotBtn, false);
  await pollInsStatus();
  await loadTagTree();
  await fetchEntries({ reset: true });
}

void bootstrap();
