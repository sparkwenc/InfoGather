import { readFile } from "node:fs/promises";
import { performance } from "node:perf_hooks";

class FakeClassList {
  constructor(element) {
    this.element = element;
  }

  toggle(name, active) {
    const names = new Set(this.element.className.split(/\s+/).filter(Boolean));
    if (active ?? !names.has(name)) names.add(name);
    else names.delete(name);
    this.element.className = Array.from(names).join(" ");
  }
}

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.className = "";
    this.classList = new FakeClassList(this);
    this._textContent = "";
    this.hidden = false;
  }

  get textContent() {
    return this._textContent + this.children.map(
      (child) => typeof child === "string" ? child : child.textContent || ""
    ).join("");
  }

  set textContent(value) {
    this._textContent = String(value);
    this.children = [];
  }

  append(...children) {
    this.children.push(...children);
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  replaceChildren(...children) {
    this.children = children;
  }

  addEventListener() {}

  setAttribute(name, value) {
    this[name] = String(value);
  }

  querySelector(selector) {
    if (!selector.startsWith("#")) return null;
    const id = selector.slice(1);
    return this.children.find((child) => child.id === id) || null;
  }

  querySelectorAll() {
    return [];
  }
}

globalThis.document = {
  createElement: (tagName) => new FakeElement(tagName)
};
let mathRenderCount = 0;
globalThis.window = {
  location: { href: "http://127.0.0.1:8787/" },
  renderMathInElement() {
    mathRenderCount += 1;
  }
};

const uiPath = new URL("../src/infogather/web/js/ui.js", import.meta.url);
const source = await readFile(uiPath, "utf8");
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const ui = await import(moduleUrl);

function median(values) {
  return values.sort((left, right) => left - right)[Math.floor(values.length / 2)];
}

function benchmark(iterations, operation) {
  for (let index = 0; index < Math.min(iterations, 100); index += 1) operation();
  const samples = [];
  for (let sample = 0; sample < 5; sample += 1) {
    const started = performance.now();
    for (let index = 0; index < iterations; index += 1) operation();
    samples.push(performance.now() - started);
  }
  return Number(median(samples).toFixed(3));
}

const entry = {
  srce_ty: "arXiv",
  srce_id: "2601.00001",
  version: 1,
  favored: 0,
  noticed: 0,
  updated: "2026-03-01T00:00:00+00:00",
  content: {
    link: "https://arxiv.org/abs/2601.00001",
    titl: "Representative paper title",
    auth: "First Author, Second Author",
    abst: "Representative abstract text ".repeat(40),
    tags: ["math.AG", "math.NT", "arXiv"]
  }
};
const treeState = {
  selectedSelectors: new Set(),
  treeGroups: [
    {
      name: "arXiv",
      count: 25_000,
      children: Array.from({ length: 17 }, (_, index) => ({
        name: `Source ${index}`,
        selector_value: `math.${index}`,
        count: 1_000 + index
      }))
    },
    {
      name: "Journals",
      count: 205,
      children: Array.from({ length: 4 }, (_, index) => ({
        name: `Journal ${index}`,
        selector_value: `source:Journals:journal-${index}`,
        count: 50 + index
      }))
    }
  ]
};
const tree = new FakeElement("ul");
const insLabel = new FakeElement("span");
insLabel.id = "ins-btn-label";
const insButton = new FakeElement("button");
insButton.append(insLabel);
const insElements = {
  insPanel: new FakeElement("section"),
  insBtn: insButton,
  insProgress: new FakeElement("progress"),
  insText: new FakeElement("span")
};

ui.makeCard({
  ...entry,
  content: { ...entry.content, titl: "Formula $x$" }
});
if (mathRenderCount !== 1) throw new Error("formula card skipped math rendering");

const measurements = {
  frontend_cards_1000_ms: benchmark(1000, () => ui.makeCard(entry)),
  frontend_trees_1000_ms: benchmark(1000, () => ui.renderTree(
    tree,
    treeState,
    { onSelectorChange() {} }
  )),
  frontend_status_1000_ms: benchmark(1000, () => ui.renderInsJob(
    insElements,
    { state: "running", progress: 50, message: "Source: 拉取 50 条" }
  ))
};

process.stdout.write(JSON.stringify(measurements));
