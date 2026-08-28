function makeHttpError(resp, payload) {
  const error = new Error(payload.error || `HTTP ${resp.status}`);
  error.status = resp.status;
  return error;
}

async function requestJson(url, options, { allowStatuses = [] } = {}) {
  const resp = await fetch(url, options);
  const payload = await resp.json().catch(() => ({}));
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
  if (!payload.ok) throw makeHttpError(resp, payload);
  return payload;
}

async function getInsStatus(signal) {
  const { payload } = await requestJson("/api/ins/status", { signal });
  return payload;
}

async function runIns() {
  const { payload } = await requestJson(
    "/api/ins/run",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    },
    { allowStatuses: [409] }
  );
  return payload;
}

async function getTagTree(params, signal) {
  const { payload } = await requestJson(`/api/tag-tree?${params}`, { signal });
  return payload;
}

async function getEntries(params, signal) {
  const { payload } = await requestJson(`/api/entries?${params}`, { signal });
  return payload;
}

function setFavored(srceTy, srceId, favored, expectedFavored, revision) {
  return postJson("/api/favored", {
    srce_ty: srceTy,
    srce_id: srceId,
    favored,
    expected_favored: expectedFavored,
    expected_revision: revision
  });
}

function setNoticed(srceTy, srceId, noticed, expectedNoticed, revision) {
  return postJson("/api/noticed", {
    srce_ty: srceTy,
    srce_id: srceId,
    noticed,
    expected_noticed: expectedNoticed,
    expected_revision: revision
  });
}

function removeEntry(srceTy, srceId) {
  return postJson("/api/remove-entry", { srce_ty: srceTy, srce_id: srceId });
}

function restoreEntry(undoToken) {
  return postJson("/api/restore-entry", { undo_token: undoToken });
}

export {
  getInsStatus,
  runIns,
  getTagTree,
  getEntries,
  setFavored,
  setNoticed,
  removeEntry,
  restoreEntry
};
