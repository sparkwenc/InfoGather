let cardSequence = 0;
  const dateFormatter = new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short"
  });

  function fmtDate(iso) {
    try {
      return dateFormatter.format(new Date(iso));
    } catch {
      return iso || "";
    }
  }

  function renderMath(node) {
    if (!window.renderMathInElement) return;
    if (!/(\$|\\\(|\\\[)/.test(node.textContent || "")) return;
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
    const suffix = selected ? `，已选源 ${selected} 个` : "";
    if (Number.isFinite(state.total)) {
      const shown = Math.min(state.offset, state.total);
      metaEl.textContent = `共 ${state.total} 条，当前显示 ${shown} 条${suffix}`;
    } else {
      metaEl.textContent = `当前显示 ${state.offset} 条${suffix}`;
    }
  }

  function renderInsJob(elements, job) {
    const {
      insPanel, insBtn, insPercent, insProgress, insText,
      insLog, insLogShell, insLogCount
    } = elements;
    const insBtnLabel = insBtn.querySelector("#ins-btn-label");

    if (!job || (job.state === "idle" && !job.started_at)) {
      insPanel.hidden = true;
      insBtn.disabled = false;
      insBtnLabel.textContent = "拉取更新";
      return;
    }

    insPanel.hidden = false;
    insPanel.dataset.state = job.state || "idle";
    const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
    insPercent.textContent = `${progress}%`;
    insProgress.value = progress;

    let prefix = "状态";
    if (job.state === "running") prefix = "拉取中";
    if (job.state === "succeeded") prefix = "已完成";
    if (job.state === "failed") prefix = "失败";
    insText.textContent = `${prefix}: ${job.message || ""}`;
    const logs = Array.isArray(job.logs) ? job.logs : [];
    const rows = logs.map((message) => {
      const row = document.createElement("div");
      row.className = "ins-log-item";
      if (message.includes("失败")) row.dataset.tone = "failed";
      else if (
        message.includes("完成")
        || message.includes("写入:")
        || /: 拉取 \d+ 条/.test(message)
      ) {
        row.dataset.tone = "success";
      } else if (message.includes("使用缓存") || message.includes("无更新")) {
        row.dataset.tone = "cached";
      }
      const marker = document.createElement("span");
      marker.className = "ins-log-marker";
      marker.setAttribute("aria-hidden", "true");
      const text = document.createElement("span");
      text.textContent = message;
      row.append(marker, text);
      return row;
    });
    insLog.replaceChildren(...rows);
    insLogShell.hidden = logs.length === 0;
    insLogCount.textContent = String(logs.length);
    if (logs.length) insLog.scrollTop = insLog.scrollHeight;

    const running = job.state === "running";
    insBtn.disabled = running;
    insBtnLabel.textContent = running ? "拉取中..." : "拉取更新";
  }

  function safeHttpUrl(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    try {
      const url = new URL(value, window.location.href);
      return ["http:", "https:"].includes(url.protocol) ? url.href : null;
    } catch {
      return null;
    }
  }

  function makeCard(item) {
    const c = item.content || {};
    const favoredValue = Number(item.favored || 0);
    const noticedValue = Number(item.noticed || 0);
    const title = String(c.titl || "无标题");
    const card = document.createElement("article");
    card.className = "card";
    card.dataset.sourceType = String(item.srce_ty || "");
    card.dataset.sourceId = String(item.srce_id || "");
    card._liveItem = item;

    const head = document.createElement("div");
    head.className = "card-head";

    const h = document.createElement("h2");
    h.className = "title";
    h.id = `entry-title-${++cardSequence}`;
    card.setAttribute("aria-labelledby", h.id);
    const link = safeHttpUrl(c.link);
    if (link) {
      const a = document.createElement("a");
      a.href = link;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = title;
      a.setAttribute("aria-label", `${title}（在新标签页打开）`);
      h.appendChild(a);
    } else {
      h.textContent = title;
    }

    const favBtn = document.createElement("button");
    favBtn.type = "button";
    favBtn.className = favoredValue === 1 ? "fav-btn on" : "fav-btn";
    favBtn.dataset.action = "favored";
    favBtn.dataset.favored = String(favoredValue);
    favBtn.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
      + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z"/></svg>'
      + `<span>${favoredValue === 1 ? "已收藏" : "收藏"}</span>`;
    favBtn.setAttribute("aria-pressed", String(favoredValue === 1));
    favBtn.setAttribute(
      "aria-label",
      favoredValue === 1 ? `取消收藏《${title}》` : `收藏《${title}》`
    );
    const noticeBtn = document.createElement("button");
    noticeBtn.type = "button";
    noticeBtn.className = noticedValue === 1 ? "notice-btn on" : "notice-btn";
    noticeBtn.dataset.action = "noticed";
    noticeBtn.dataset.noticed = String(noticedValue);
    noticeBtn.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
      + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<circle cx="12" cy="12" r="9"/><path d="M8.5 12.5l2.5 2.5 4.5-5"/></svg>'
      + `<span>${noticedValue === 1 ? "已读" : "未读"}</span>`;
    noticeBtn.setAttribute("aria-pressed", String(noticedValue === 1));
    noticeBtn.setAttribute(
      "aria-label",
      noticedValue === 1 ? `标记《${title}》为未读` : `标记《${title}》为已读`
    );
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "del-btn";
    delBtn.dataset.action = "remove";
    delBtn.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
      + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 002 2h6a2 2 0 002-2l1-12'
      + 'M9 7V5a2 2 0 012-2h2a2 2 0 012 2v2"/></svg>'
      + "<span>移除</span>";
    delBtn.setAttribute("aria-label", `移除《${title}》`);
    const actions = document.createElement("div");
    actions.className = "card-actions";
    actions.append(noticeBtn, favBtn, delBtn);

    head.append(h, actions);

    const m1 = document.createElement("p");
    m1.className = "line";
    m1.textContent = [
      "作者: " + (c.auth || "-"),
      "来源: " + (c.source || (item.srce_ty || "-") + ":" + (item.srce_id || "-")),
      "版本: v" + (item.version ?? "-")
    ].join("  ·  ");

    const m2 = document.createElement("p");
    m2.className = "line";
    m2.textContent = "更新时间: " + fmtDate(item.updated);

    const abst = document.createElement("p");
    abst.className = "abstract";
    abst.textContent = c.abst || "";

    card.append(head, m1, m2, abst);

    const visibleTags = Array.isArray(c.tags)
      ? c.tags.filter((tag) => !String(tag).startsWith("source:"))
      : [];
    if (visibleTags.length) {
      const ul = document.createElement("ul");
      ul.className = "tags";
      visibleTags.forEach((tag) => {
        const li = document.createElement("li");
        li.className = "tag";
        li.textContent = tag;
        ul.appendChild(li);
      });
      card.appendChild(ul);
    }
    renderMath(card);
    return card;
  }

  function renderTree(treeListEl, state, handlers) {
    const openGroups = new Map(Array.from(
      treeListEl.querySelectorAll("details[data-source-group]"),
      (details) => [details.dataset.sourceGroup, details.open]
    ));
    const groups = [];
    if (!state.treeGroups.length) {
      const empty = document.createElement("li");
      empty.className = "tree-item tree-empty";
      empty.textContent = "暂无来源";
      treeListEl.replaceChildren(empty);
      return;
    }

    state.treeGroups.forEach((group, groupIndex) => {
      const groupLi = document.createElement("li");
      groupLi.className = "tree-item";

      const details = document.createElement("details");
      details.dataset.sourceGroup = group.name;
      details.open = openGroups.get(group.name) ?? true;
      const title = document.createElement("summary");
      title.className = "tree-group-title";

      const meta = document.createElement("span");
      meta.className = "tree-group-meta";
      meta.textContent = `${group.name} (${group.count})`;

      title.appendChild(meta);
      details.appendChild(title);

      const childUl = document.createElement("ul");
      childUl.className = "tree-children";

      const children = Array.isArray(group.children) ? group.children : [];
      children.forEach((node, childIndex) => {
        const selectorValue = node.selector_value || node.name || "";
        const selector = `tag:${selectorValue}`;

        const li = document.createElement("li");
        li.className = "tree-item";

        const label = document.createElement("label");
        const left = document.createElement("span");
        left.className = "tag-name";

        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.id = `source-${groupIndex}-${childIndex}`;
        checkbox.checked = state.selectedSelectors.has(selector);
        label.htmlFor = checkbox.id;
        checkbox.addEventListener("change", () => {
          handlers.onSelectorChange(selector, checkbox.checked);
        });

        const text = document.createElement("span");
        text.textContent = String(selectorValue).startsWith("source:")
          ? node.name
          : `${node.name} (${selectorValue})`;

        const count = document.createElement("span");
        count.className = "tag-count";
        count.textContent = String(node.count ?? 0);

        left.append(checkbox, text);
        label.append(left, count);
        li.appendChild(label);
        childUl.appendChild(li);
      });

      details.appendChild(childUl);
      groupLi.appendChild(details);
      groups.push(groupLi);
    });
    treeListEl.replaceChildren(...groups);
  }

export { setToggle, setMeta, renderInsJob, makeCard, renderTree };
