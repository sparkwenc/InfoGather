const pageSize = 24;

const api = window.InfoAPI;
const ui = window.InfoUI;
const state = window.InfoState.createState();

const metaEl = document.getElementById("meta");
const listEl = document.getElementById("list");
const moreEl = document.getElementById("more");
const qEl = document.getElementById("q");
const searchEl = document.getElementById("search");
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
const dayBtn = document.getElementById("day-btn");
const weekBtn = document.getElementById("week-btn");
const versionBtn = document.getElementById("version-btn");
const versionNotBtn = document.getElementById("version-not-btn");

function insElements() {
  return { insPanel, insBtn, insText, insPercent, insBar, insLog };
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
  if (state.updatedWithinDay) params.set("updated_within_day", "1");
  if (state.updatedWithinWeek) params.set("updated_within_week", "1");
  if (state.versionIs1) params.set("version_is_1", "1");
  if (state.versionIsNot1) params.set("version_is_not_1", "1");
  for (const selector of state.selectedSelectors) {
    params.append("selectors", selector);
  }
  return params;
}

function refreshFilteredView() {
  void Promise.all([
    loadTagTree(),
    fetchEntries({ reset: true })
  ]);
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
  btnEl.disabled = true;
  try {
    await api.setFavored(item.srce_ty, item.srce_id, favored);
    await fetchEntries({ reset: true });
  } catch (err) {
    console.error(err);
    window.alert("收藏更新失败，请稍后重试。");
  } finally {
    btnEl.disabled = false;
  }
}

async function removeEntry(item, btnEl) {
  const ok = window.confirm(`确认删除 ${item.srce_ty}:${item.srce_id} ?`);
  if (!ok) return;

  btnEl.disabled = true;
  try {
    await api.removeEntry(item.srce_ty, item.srce_id);
    await loadTagTree();
    await fetchEntries({ reset: true });
  } catch (err) {
    console.error(err);
    window.alert("删除失败，请稍后重试。");
  } finally {
    btnEl.disabled = false;
  }
}

async function loadTagTree() {
  try {
    const payload = await api.getTagTree(buildFilterParams());
    const root = payload.root || { name: "配置源", group_count: 0, source_count: 0, count: 0 };
    treeRootEl.textContent = `${root.name}（${root.group_count} 类 / ${root.source_count} 源 / ${root.count} 条）`;
    state.treeGroups = Array.isArray(payload.groups) ? payload.groups : [];
    renderTree();
  } catch (err) {
    treeRootEl.textContent = "配置源 (加载失败)";
    treeListEl.innerHTML = '<li class="tree-item"><label>无法读取源列表</label></li>';
    console.error(err);
  }
}

async function fetchEntries({ reset = false } = {}) {
  if (state.loading) {
    if (reset) state.pendingReset = true;
    return;
  }
  state.loading = true;
  moreEl.disabled = true;
  const queryOffset = reset ? 0 : state.offset;

  const params = buildFilterParams();
  params.set("limit", String(pageSize));
  params.set("offset", String(queryOffset));

  try {
    const payload = await api.getEntries(params);
    const items = Array.isArray(payload.items) ? payload.items : [];
    const nextTotal = Number(payload.total || 0);

    if (reset) {
      const fragment = document.createDocumentFragment();
      if (!items.length) {
        const p = document.createElement("p");
        p.className = "empty";
        p.textContent = "没有匹配的条目。";
        fragment.appendChild(p);
      } else {
        items.forEach((item) => {
          const card = ui.makeCard(item, {
            onToggleFavored: updateFavored,
            onRemove: removeEntry
          });
          fragment.appendChild(card);
          ui.renderMath(card);
        });
      }
      listEl.replaceChildren(fragment);
      state.offset = items.length;
      state.total = nextTotal;
    } else {
      items.forEach((item) => {
        const card = ui.makeCard(item, {
          onToggleFavored: updateFavored,
          onRemove: removeEntry
        });
        listEl.appendChild(card);
        ui.renderMath(card);
      });
      state.offset += items.length;
      state.total = nextTotal;
    }
  } catch (err) {
    if (!listEl.children.length) {
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

favoredBtn.addEventListener("click", () => {
  state.favoredOnly = !state.favoredOnly;
  ui.setToggle(favoredBtn, state.favoredOnly);
  refreshFilteredView();
});

dayBtn.addEventListener("click", () => {
  state.updatedWithinDay = !state.updatedWithinDay;
  ui.setToggle(dayBtn, state.updatedWithinDay);
  refreshFilteredView();
});

weekBtn.addEventListener("click", () => {
  state.updatedWithinWeek = !state.updatedWithinWeek;
  ui.setToggle(weekBtn, state.updatedWithinWeek);
  refreshFilteredView();
});

versionBtn.addEventListener("click", () => {
  const next = !state.versionIs1;
  state.versionIs1 = next;
  if (next) {
    state.versionIsNot1 = false;
    ui.setToggle(versionNotBtn, false);
  }
  ui.setToggle(versionBtn, state.versionIs1);
  refreshFilteredView();
});

versionNotBtn.addEventListener("click", () => {
  const next = !state.versionIsNot1;
  state.versionIsNot1 = next;
  if (next) {
    state.versionIs1 = false;
    ui.setToggle(versionBtn, false);
  }
  ui.setToggle(versionNotBtn, state.versionIsNot1);
  refreshFilteredView();
});

clearTagsEl.addEventListener("click", () => {
  if (!state.selectedSelectors.size) return;
  state.selectedSelectors.clear();
  renderTree();
  refreshFilteredView();
});

searchEl.addEventListener("click", () => {
  refreshFilteredView();
});

qEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    refreshFilteredView();
  }
});

moreEl.addEventListener("click", () => {
  void fetchEntries({ reset: false });
});

async function bootstrap() {
  ui.setToggle(favoredBtn, false);
  ui.setToggle(dayBtn, false);
  ui.setToggle(weekBtn, false);
  ui.setToggle(versionBtn, false);
  ui.setToggle(versionNotBtn, false);
  await pollInsStatus();
  await loadTagTree();
  await fetchEntries({ reset: true });
}

void bootstrap();
