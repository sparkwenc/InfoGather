(function (global) {
  function fmtDate(iso) {
    try {
      return new Intl.DateTimeFormat("zh-CN", {
        dateStyle: "medium",
        timeStyle: "short"
      }).format(new Date(iso));
    } catch {
      return iso || "";
    }
  }

  function renderMath(node) {
    if (!window.renderMathInElement) return;
    window.renderMathInElement(node, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "$", right: "$", display: false },
        { left: "\\(", right: "\\)", display: false },
        { left: "\\[", right: "\\]", display: true }
      ],
      throwOnError: false,
      strict: "ignore"
    });
  }

  function setToggle(btn, active) {
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  }

  function setMeta(metaEl, state) {
    const selected = state.selectedSelectors.size;
    const shown = Math.min(state.offset, state.total);
    const suffix = selected ? `，已选源 ${selected} 个` : "";
    metaEl.textContent = `共 ${state.total} 条，当前显示 ${shown} 条${suffix}`;
  }

  function setMoreVisible(moreEl, state) {
    moreEl.hidden = state.offset >= state.total;
  }

  function renderInsJob(elements, job) {
    const { insPanel, insBtn, insPercent, insBar, insText, insLog } = elements;

    if (!job || (job.state === "idle" && !job.started_at)) {
      insPanel.hidden = true;
      insBtn.disabled = false;
      insBtn.textContent = "拉取更新";
      return;
    }

    insPanel.hidden = false;
    const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
    insPercent.textContent = `${progress}%`;
    insBar.style.width = `${progress}%`;

    let prefix = "状态";
    if (job.state === "running") prefix = "拉取中";
    if (job.state === "succeeded") prefix = "已完成";
    if (job.state === "failed") prefix = "失败";
    insText.textContent = `${prefix}: ${job.message || ""}`;

    const logs = Array.isArray(job.logs) ? job.logs : [];
    insLog.textContent = logs.slice(-3).join("\n");

    const running = job.state === "running";
    insBtn.disabled = running;
    insBtn.textContent = running ? "拉取中..." : "拉取更新";
  }

  function makeCard(item, handlers) {
    const c = item.content || {};
    const favoredValue = Number(item.favored || 0);
    const noticedValue = Number(item.noticed || 0);
    const card = document.createElement("article");
    card.className = "card";

    const head = document.createElement("div");
    head.className = "card-head";

    const h = document.createElement("h2");
    h.className = "title";
    const a = document.createElement("a");
    a.href = c.link || "#";
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = c.titl || "(No title)";
    h.appendChild(a);

    const favBtn = document.createElement("button");
    favBtn.type = "button";
    favBtn.className = favoredValue === 1 ? "fav-btn on" : "fav-btn";
    favBtn.dataset.favored = String(favoredValue);
    favBtn.textContent = favoredValue === 1 ? "已收藏" : "收藏";
    favBtn.addEventListener("click", async () => {
      const current = Number(favBtn.dataset.favored || 0);
      const next = current === 1 ? 0 : 1;
      await handlers.onToggleFavored(item, next, favBtn);
    });

    const noticeBtn = document.createElement("button");
    noticeBtn.type = "button";
    noticeBtn.className = noticedValue === 1 ? "notice-btn on" : "notice-btn";
    noticeBtn.dataset.noticed = String(noticedValue);
    noticeBtn.textContent = noticedValue === 1 ? "已读" : "未读";
    noticeBtn.addEventListener("click", async () => {
      const current = Number(noticeBtn.dataset.noticed || 0);
      const next = current === 1 ? 0 : 1;
      await handlers.onToggleNoticed(item, next, noticeBtn);
    });

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "del-btn";
    delBtn.textContent = "移除";
    delBtn.addEventListener("click", async () => {
      await handlers.onRemove(item, delBtn);
    });

    const actions = document.createElement("div");
    actions.className = "card-actions";
    actions.appendChild(noticeBtn);
    actions.appendChild(favBtn);
    actions.appendChild(delBtn);

    head.appendChild(h);
    head.appendChild(actions);

    const m1 = document.createElement("p");
    m1.className = "line";
    m1.textContent = [
      "作者: " + (c.auth || "-"),
      "来源: " + (item.srce_ty || "-") + ":" + (item.srce_id || "-"),
      "版本: v" + (item.version ?? "-"),
      "已读: " + (item.noticed ? "是" : "否"),
      "收藏: " + (item.favored ? "是" : "否")
    ].join("  ·  ");

    const m2 = document.createElement("p");
    m2.className = "line";
    m2.textContent = "更新时间: " + fmtDate(item.updated);

    const abst = document.createElement("p");
    abst.className = "abstract";
    abst.textContent = c.abst || "";

    card.appendChild(head);
    card.appendChild(m1);
    card.appendChild(m2);
    card.appendChild(abst);

    if (Array.isArray(c.tags) && c.tags.length) {
      const ul = document.createElement("ul");
      ul.className = "tags";
      c.tags.forEach((tag) => {
        const li = document.createElement("li");
        li.className = "tag";
        li.textContent = tag;
        ul.appendChild(li);
      });
      card.appendChild(ul);
    }
    return card;
  }

  function renderTree(treeListEl, state, handlers) {
    const fragment = document.createDocumentFragment();
    if (!state.treeGroups.length) {
      const empty = document.createElement("li");
      empty.className = "tree-item";
      empty.innerHTML = '<label>暂无源</label>';
      treeListEl.replaceChildren(empty);
      return;
    }

    state.treeGroups.forEach((group) => {
      const groupKey = String(group.name || "");
      const isCollapsed = state.collapsedGroups.has(groupKey);
      const groupLi = document.createElement("li");
      groupLi.className = "tree-item";

      const title = document.createElement("div");
      title.className = isCollapsed ? "tree-group-title collapsed" : "tree-group-title";

      const meta = document.createElement("span");
      meta.className = "tree-group-meta";
      meta.textContent = `${group.name} (${group.count})`;

      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "tree-group-toggle";
      toggle.textContent = isCollapsed ? "+" : "−";
      toggle.title = isCollapsed ? "展开" : "折叠";
      toggle.addEventListener("click", () => {
        handlers.onToggleGroup(groupKey);
      });

      title.appendChild(meta);
      title.appendChild(toggle);
      groupLi.appendChild(title);

      const childUl = document.createElement("ul");
      childUl.className = "tree-children";
      childUl.hidden = isCollapsed;

      const children = Array.isArray(group.children) ? group.children : [];
      children.forEach((node) => {
        const selectorType = node.selector_type || "tag";
        const selectorValue = node.selector_value || node.name || "";
        const selector = `${selectorType}:${selectorValue}`;

        const li = document.createElement("li");
        li.className = "tree-item";

        const label = document.createElement("label");
        const left = document.createElement("span");
        left.className = "tag-name";

        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = state.selectedSelectors.has(selector);
        checkbox.addEventListener("change", () => {
          handlers.onSelectorChange(selector, checkbox.checked);
        });

        const text = document.createElement("span");
        text.textContent = selectorType === "tag"
          ? `${node.name} (${selectorValue})`
          : String(node.name);

        const count = document.createElement("span");
        count.className = "tag-count";
        count.textContent = String(node.count ?? 0);

        left.appendChild(checkbox);
        left.appendChild(text);
        label.appendChild(left);
        label.appendChild(count);
        li.appendChild(label);
        childUl.appendChild(li);
      });

      groupLi.appendChild(childUl);
      fragment.appendChild(groupLi);
    });
    treeListEl.replaceChildren(fragment);
  }

  global.InfoUI = {
    setToggle,
    setMeta,
    setMoreVisible,
    renderMath,
    renderInsJob,
    makeCard,
    renderTree
  };
})(window);
