(function (global) {
  async function parsePayload(resp) {
    return await resp.json().catch(() => ({}));
  }

  function makeHttpError(resp, payload) {
    return new Error(payload.error || `HTTP ${resp.status}`);
  }

  async function getInsStatus() {
    const resp = await fetch("/api/ins/status");
    if (!resp.ok) {
      const payload = await parsePayload(resp);
      throw makeHttpError(resp, payload);
    }
    return await parsePayload(resp);
  }

  async function runIns() {
    const resp = await fetch("/api/ins/run", { method: "POST" });
    const payload = await parsePayload(resp);
    if (!resp.ok && resp.status !== 409) {
      throw makeHttpError(resp, payload);
    }
    return { status: resp.status, payload };
  }

  async function getTagTree() {
    const resp = await fetch("/api/tag-tree");
    const payload = await parsePayload(resp);
    if (!resp.ok) {
      throw makeHttpError(resp, payload);
    }
    return payload;
  }

  async function getEntries(params) {
    const query = params instanceof URLSearchParams ? params.toString() : new URLSearchParams(params).toString();
    const resp = await fetch(`/api/entries?${query}`);
    const payload = await parsePayload(resp);
    if (!resp.ok) {
      throw makeHttpError(resp, payload);
    }
    return payload;
  }

  async function setFavored(srceTy, srceId, favored) {
    const resp = await fetch("/api/favored", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        srce_ty: srceTy,
        srce_id: srceId,
        favored: favored
      })
    });
    const payload = await parsePayload(resp);
    if (!resp.ok || !payload.ok) {
      throw makeHttpError(resp, payload);
    }
    return payload;
  }

  async function removeEntry(srceTy, srceId) {
    const resp = await fetch("/api/remove-entry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        srce_ty: srceTy,
        srce_id: srceId
      })
    });
    const payload = await parsePayload(resp);
    if (!resp.ok || !payload.ok) {
      throw makeHttpError(resp, payload);
    }
    return payload;
  }

  global.InfoAPI = {
    getInsStatus,
    runIns,
    getTagTree,
    getEntries,
    setFavored,
    removeEntry
  };
})(window);
