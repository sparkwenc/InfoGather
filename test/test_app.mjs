import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../src/infogather/web/js/app.js", import.meta.url), "utf8");

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function element() {
  const listeners = new Map();
  return {
    children: [],
    dataset: {},
    textContent: "",
    classList: { toggle() {} },
    addEventListener(event, handler) { listeners.set(event, handler); },
    click() { listeners.get("click")?.(); },
    setAttribute() {},
    removeAttribute() {},
    contains() { return false; },
    matches() { return false; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    replaceChildren(...children) { this.children = children; },
    append(...children) { this.children.push(...children); }
  };
}

const emptyPage = { items: [], total: 0, has_more: false, next_cursor: null };

async function application(overrides = {}) {
  const elements = new Map();
  const document = {
    hidden: true,
    activeElement: null,
    createElement: element,
    addEventListener() {},
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    }
  };
  const api = {
    getInsStatus: async () => ({ job: { state: "idle" } }),
    getTagTree: async () => ({ groups: [] }),
    getEntries: async () => emptyPage,
    setFavored: async () => ({ ok: true, state_rev: 1 }),
    ...overrides
  };
  const context = vm.createContext({
    api, document, AbortController, AbortSignal, URLSearchParams,
    console: { error() {} },
    window: { alert() {} },
    ui: {
      setMeta() {}, renderTree() {}, renderInsJob() {}, makeCard: element,
      setToggle(button, active) { button.pressed = active; }
    },
    setTimeout() { throw new Error("Unexpected polling timer"); },
    clearTimeout() {}
  });
  vm.runInContext(source.replace(/^import .*;\n/gm, "") + `
    globalThis.app = { state, fetchEntries, updateFlag, pauseInsPolling, refreshFilteredView };
  `, context);
  // Let initialization settle while the document is hidden.
  await new Promise(setImmediate);
  document.hidden = false;
  return { ...context.app, api, document, elements };
}

test("hiding the page cancels load-more and discards a late response", async () => {
  const pending = deferred();
  let signal;
  const app = await application({
    getEntries: (_params, requestSignal) => {
      signal = requestSignal;
      return pending.promise;
    }
  });
  app.state.offset = 24;
  app.state.cursor = "page-two";
  const loading = app.fetchEntries();
  app.document.hidden = true;
  app.pauseInsPolling();
  assert.equal(signal.aborted, true);
  pending.resolve({ items: [{}], has_more: true, next_cursor: "page-three" });
  await loading;
  assert.equal(app.state.offset, 24);
  assert.equal(app.state.cursor, "page-two");
  assert.equal(app.state.entriesRequest, null);
  assert.equal(app.state.refreshOnVisible, true);
});

test("a flag change finishes an interrupted filter refresh", async () => {
  const pending = deferred();
  const calls = [];
  const app = await application({
    getEntries: (params, signal) => {
      calls.push({ params, signal });
      return calls.length === 1 ? pending.promise : Promise.resolve(emptyPage);
    }
  });
  app.state.appliedQuery = "new filter";
  app.state.total = 99;
  const refresh = app.refreshFilteredView();
  await app.updateFlag({ srce_ty: "arXiv", srce_id: "1", favored: 0 }, "favored", 1, element());
  assert.equal(calls.length, 2);
  assert.equal(calls[0].signal.aborted, true);
  assert.equal(calls[1].params.get("q"), "new filter");
  assert.equal(app.state.total, 0);
  assert.equal(app.state.lastUndo.currentRevision, 1);
  pending.resolve({ ...emptyPage, total: 99 });
  await refresh;
  assert.equal(app.state.total, 0);
});

test("a flag change cancels pagination and the next load can proceed", async () => {
  const pending = deferred();
  const signals = [];
  const app = await application({
    getEntries: (_params, signal) => {
      signals.push(signal);
      return signals.length === 1 ? pending.promise : Promise.resolve(emptyPage);
    }
  });
  const loading = app.fetchEntries();
  await app.updateFlag({ srce_ty: "arXiv", srce_id: "1", favored: 0 }, "favored", 1, element());
  assert.equal(signals[0].aborted, true);
  assert.equal(signals.length, 2);
  pending.resolve({ items: [{}], has_more: true, next_cursor: "stale" });
  await loading;
  assert.equal(app.state.offset, 0);
  assert.equal(await app.fetchEntries(), true);
  assert.equal(signals[2].aborted, false);
});

test("filter buttons preserve independent states and enforce exclusive pairs", async () => {
  const app = await application();
  const click = async (id) => {
    app.elements.get(id).click();
    await new Promise(setImmediate);
  };
  await click("favored-btn");
  await click("unnoticed-btn");
  for (const [first, firstField, second, secondField] of [
    ["day-btn", "updatedWithinDay", "week-btn", "updatedWithinWeek"],
    ["version-btn", "versionIs1", "version-not-btn", "versionIsNot1"]
  ]) {
    await click(first);
    assert.equal(app.state[firstField], true);
    await click(second);
    assert.equal(app.state[firstField], false);
    assert.equal(app.elements.get(first).pressed, false);
    assert.equal(app.state[secondField], true);
    await click(second);
    assert.equal(app.state[secondField], false);
  }
  assert.equal(app.state.favoredOnly, true);
  assert.equal(app.state.unnoticedOnly, true);
});
