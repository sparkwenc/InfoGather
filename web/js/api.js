(function (global) {
  async function parsePayload(resp) {
    return await resp.json().catch(() => ({}));
  }

  function makeHttpError(resp, payload) {
    return new Error(payload.error || `HTTP ${resp.status}`);
  }

  function buildQuery(params) {
    if (params instanceof URLSearchParams) return params.toString();
    return new URLSearchParams(params || {}).toString();
  }

  async function requestJson(url, options, { allowStatuses = [] } = {}) {
    const resp = await fetch(url, options);
    const payload = await parsePayload(resp);
    if (!resp.ok && !allowStatuses.includes(resp.status)) {
      throw makeHttpError(resp, payload);
    }
    return { resp, payload };
  }

  async function postJson(url, body) {
    const { resp, payload } = await requestJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    if (!payload.ok) {
      throw makeHttpError(resp, payload);
    }
    return payload;
  }

  async function getInsStatus() {
    const { payload } = await requestJson("/api/ins/status");
    return payload;
  }

  async function runIns() {
    const { resp, payload } = await requestJson(
      "/api/ins/run",
      { method: "POST" },
      { allowStatuses: [409] }
    );
    return { status: resp.status, payload };
  }

  async function getTagTree(params) {
    const query = buildQuery(params);
    const suffix = query ? `?${query}` : "";
    const { payload } = await requestJson(`/api/tag-tree${suffix}`);
    return payload;
  }

  async function getEntries(params) {
    const query = buildQuery(params);
    const { payload } = await requestJson(`/api/entries?${query}`);
    return payload;
  }

  async function setFavored(srceTy, srceId, favored) {
    return await postJson("/api/favored", {
      srce_ty: srceTy,
      srce_id: srceId,
      favored: favored
    });
  }

  async function setNoticed(srceTy, srceId, noticed) {
    return await postJson("/api/noticed", {
      srce_ty: srceTy,
      srce_id: srceId,
      noticed: noticed
    });
  }

  async function removeEntry(srceTy, srceId) {
    return await postJson("/api/remove-entry", {
      srce_ty: srceTy,
      srce_id: srceId
    });
  }

  global.InfoAPI = {
    getInsStatus,
    runIns,
    getTagTree,
    getEntries,
    setFavored,
    setNoticed,
    removeEntry
  };
})(window);
