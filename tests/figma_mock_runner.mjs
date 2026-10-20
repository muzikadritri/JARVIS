// A stand-in for the Figma desktop app, for the test suite.
//
// It loads the REAL plugin main thread (figma_plugin/code.js) against a mock
// `figma` global that enforces the Plugin API rules the design kit exists to
// get right — fonts loaded before text, FILL sizing only inside auto layout,
// colour channels in 0..1, valid enum values — so a kit or plugin regression
// fails here instead of in somebody's open file.
//
// Protocol (JSON lines): stdin  {"type":"run","job":{...}} | {"type":"dump"}
//                        stdout {"type":"result",...} | {"type":"context",...} | {"type":"dump",...}
import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const pluginCode = readFileSync(path.join(here, "..", "figma_plugin", "code.js"), "utf8");

const FONTS = {
  Inter: ["Light", "Regular", "Medium", "Semi Bold", "Bold", "Extra Bold", "Black"],
  Poppins: ["Regular", "Medium", "SemiBold", "Bold"],
};
const loadedFonts = new Set();
const registry = new Map();
let nextId = 2;

const ENUMS = {
  layoutMode: ["NONE", "HORIZONTAL", "VERTICAL"],
  primaryAxisAlignItems: ["MIN", "CENTER", "MAX", "SPACE_BETWEEN"],
  counterAxisAlignItems: ["MIN", "CENTER", "MAX", "BASELINE"],
  primaryAxisSizingMode: ["FIXED", "AUTO"],
  counterAxisSizingMode: ["FIXED", "AUTO"],
  textAutoResize: ["NONE", "WIDTH_AND_HEIGHT", "HEIGHT", "TRUNCATE"],
  textAlignHorizontal: ["LEFT", "CENTER", "RIGHT", "JUSTIFIED"],
  textCase: ["ORIGINAL", "UPPER", "LOWER", "TITLE"],
  layoutWrap: ["NO_WRAP", "WRAP"],
  layoutPositioning: ["AUTO", "ABSOLUTE"],
  strokeAlign: ["INSIDE", "OUTSIDE", "CENTER"],
};

function checkPaints(paints) {
  if (!Array.isArray(paints)) throw new Error("fills must be an array");
  for (const p of paints) {
    if (p.type === "SOLID") {
      for (const ch of ["r", "g", "b"]) {
        const v = p.color && p.color[ch];
        if (typeof v !== "number" || v < 0 || v > 1) throw new Error(`Invalid color channel ${ch}=${v} (expected 0..1)`);
      }
    } else if (p.type === "GRADIENT_LINEAR") {
      if (!Array.isArray(p.gradientStops) || p.gradientStops.length < 2) throw new Error("gradient needs 2+ stops");
      if (!Array.isArray(p.gradientTransform) || p.gradientTransform.length !== 2) throw new Error("bad gradientTransform");
    } else {
      throw new Error(`unsupported paint type ${p.type}`);
    }
  }
}

class MockNode {
  constructor(type) {
    this.id = `1:${nextId++}`;
    this.type = type;
    this.name = type;
    this.parent = null;
    this.x = 0; this.y = 0; this.width = 100; this.height = 100;
    this._fills = [];
    this.strokes = []; this.effects = []; this.opacity = 1;
    this.layoutGrow = 0;
    this._sizingH = "FIXED"; this._sizingV = "FIXED";
    this.layoutPositioning = "AUTO";
    registry.set(this.id, this);
  }
  get fills() { return this._fills; }
  set fills(v) { checkPaints(v); this._fills = v; }
  resize(w, h) {
    if (!(w >= 0.01 && h >= 0.01)) throw new Error(`resize: invalid size ${w}x${h}`);
    this.width = w; this.height = h;
  }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this);
    this.parent = null;
    registry.delete(this.id);
    for (const child of this.children || []) child.remove();
  }
  get layoutSizingHorizontal() { return this._sizingH; }
  set layoutSizingHorizontal(v) { this._setSizing("H", v); }
  get layoutSizingVertical() { return this._sizingV; }
  set layoutSizingVertical(v) { this._setSizing("V", v); }
  _setSizing(axis, v) {
    if (!["FIXED", "HUG", "FILL"].includes(v)) throw new Error(`invalid sizing ${v}`);
    const parentAuto = this.parent && this.parent.layoutMode && this.parent.layoutMode !== "NONE";
    if (v === "FILL" && !parentAuto) throw new Error("FILL can only be set on children of auto-layout frames");
    const selfAuto = this.layoutMode && this.layoutMode !== "NONE";
    if (v === "HUG" && !selfAuto && this.type !== "TEXT") throw new Error("HUG can only be set on auto-layout frames and text");
    if (axis === "H") this._sizingH = v; else this._sizingV = v;
  }
  async exportAsync(settings) {
    if (!settings || settings.format !== "PNG") throw new Error("mock exports PNG only");
    return new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13]);
  }
}

function enumProp(cls, prop, initial) {
  const key = `_${prop}`;
  Object.defineProperty(cls.prototype, prop, {
    configurable: true,
    get() { return this[key] ?? initial; },
    set(v) {
      if (!ENUMS[prop].includes(v)) throw new Error(`Invalid value for ${prop}: ${v}`);
      this[key] = v;
    },
  });
}

class MockFrame extends MockNode {
  constructor() {
    super("FRAME");
    this.children = [];
    this.itemSpacing = 0; this.counterAxisSpacing = 0;
    this.paddingTop = 0; this.paddingRight = 0; this.paddingBottom = 0; this.paddingLeft = 0;
    this.cornerRadius = 0; this.clipsContent = true; this.strokeWeight = 1;
    this._fills = [{ type: "SOLID", color: { r: 1, g: 1, b: 1 } }];
  }
  appendChild(node) {
    if (node.parent) node.parent.children = node.parent.children.filter((c) => c !== node);
    node.parent = this;
    this.children.push(node);
  }
  insertChild(index, node) {
    this.appendChild(node);
    this.children.splice(this.children.indexOf(node), 1);
    this.children.splice(index, 0, node);
  }
}
const FRAME_DEFAULTS = {
  layoutMode: "NONE", primaryAxisAlignItems: "MIN", counterAxisAlignItems: "MIN",
  primaryAxisSizingMode: "FIXED", counterAxisSizingMode: "FIXED", strokeAlign: "INSIDE",
};
for (const [prop, initial] of Object.entries(FRAME_DEFAULTS)) enumProp(MockFrame, prop, initial);
// As in Figma: an AUTO sizing mode on an axis reads back as HUG on that axis.
for (const [prop, primary] of [["primaryAxisSizingMode", true], ["counterAxisSizingMode", false]]) {
  const base = Object.getOwnPropertyDescriptor(MockFrame.prototype, prop);
  Object.defineProperty(MockFrame.prototype, prop, {
    get: base.get,
    set(v) {
      base.set.call(this, v);
      const vertical = (this.layoutMode === "VERTICAL") === primary;
      if (v === "AUTO") { if (vertical) this._sizingV = "HUG"; else this._sizingH = "HUG"; }
    },
  });
}
Object.defineProperty(MockFrame.prototype, "layoutWrap", {
  get() { return this._layoutWrap || "NO_WRAP"; },
  set(v) {
    if (!ENUMS.layoutWrap.includes(v)) throw new Error(`Invalid layoutWrap ${v}`);
    if (v === "WRAP" && this.layoutMode !== "HORIZONTAL") throw new Error("WRAP requires HORIZONTAL layout");
    this._layoutWrap = v;
  },
});

class MockText extends MockNode {
  constructor() {
    super("TEXT");
    this._fontName = { family: "Inter", style: "Regular" };
    this._characters = "";
    this.fontSize = 12;
    this.lineHeight = { unit: "AUTO" };
    this.letterSpacing = { value: 0, unit: "PERCENT" };
    this._sizingH = "HUG"; this._sizingV = "HUG";
  }
  get fontName() { return this._fontName; }
  set fontName(v) {
    if (!loadedFonts.has(`${v.family}::${v.style}`)) throw new Error(`unloaded font "${v.family} ${v.style}"`);
    this._fontName = v;
  }
  get characters() { return this._characters; }
  set characters(v) {
    const f = this._fontName;
    if (!loadedFonts.has(`${f.family}::${f.style}`)) throw new Error(`Cannot write to node with unloaded font "${f.family} ${f.style}"`);
    this._characters = String(v);
    this.width = Math.max(1, this._characters.length * 7);
    this.height = 20;
  }
}
for (const prop of ["textAutoResize", "textAlignHorizontal", "textCase"]) {
  enumProp(MockText, prop, { textAutoResize: "WIDTH_AND_HEIGHT", textAlignHorizontal: "LEFT", textCase: "ORIGINAL" }[prop]);
}

const page = new MockFrame();
page.type = "PAGE"; page.name = "Page 1"; page.id = "0:1";
registry.set("0:1", page);
let selection = [];
Object.defineProperty(page, "selection", {
  get() { return selection; },
  set(v) {
    for (const n of v) if (!registry.has(n.id)) throw new Error("selection contains a removed node");
    selection = v;
  },
});

const outbox = (msg) => process.stdout.write(JSON.stringify(msg) + "\n");
let onmessage = null;
const listeners = {};

globalThis.figma = {
  root: { name: "Test File" },
  fileKey: undefined,
  get currentUser() {
    const manifest = JSON.parse(readFileSync(path.join(here, "..", "figma_plugin", "manifest.json"), "utf8"));
    if (!(manifest.permissions || []).includes("currentuser")) throw new Error("currentuser permission required");
    return { name: "Tester" };
  },
  currentPage: page,
  mixed: Symbol("mixed"),
  showUI() {},
  ui: {
    postMessage: (msg) => outbox(msg),
    set onmessage(fn) { onmessage = fn; },
    get onmessage() { return onmessage; },
  },
  on(event, cb) { (listeners[event] ||= []).push(cb); },
  notify() { return { cancel() {} }; },
  closePlugin() {},
  createFrame() { const f = new MockFrame(); page.appendChild(f); return f; },
  createText() { const t = new MockText(); page.appendChild(t); return t; },
  createRectangle() { const r = new MockNode("RECTANGLE"); r.cornerRadius = 0; page.appendChild(r); return r; },
  createEllipse() { const e = new MockNode("ELLIPSE"); page.appendChild(e); return e; },
  async loadFontAsync(font) {
    const styles = FONTS[font.family];
    if (!styles || !styles.includes(font.style)) throw new Error(`The font "${font.family} ${font.style}" could not be loaded`);
    loadedFonts.add(`${font.family}::${font.style}`);
  },
  async getNodeByIdAsync(id) { return registry.get(id) || null; },
  async setCurrentPageAsync() {},
  viewport: { scrollAndZoomIntoView() {} },
  base64Encode(bytes) { return Buffer.from(bytes).toString("base64"); },
};
globalThis.__html__ = "";

(0, eval)(pluginCode);

function dump(node, depth = 0) {
  const out = { id: node.id, type: node.type, name: node.name };
  if (node.type === "TEXT") { out.text = node.characters; out.font = node.fontName; out.size = node.fontSize; }
  if (node.layoutMode) out.layout = node.layoutMode;
  out.sizing = `${node.layoutSizingHorizontal}/${node.layoutSizingVertical}`;
  if (node.children && depth < 8) out.children = node.children.map((c) => dump(c, depth + 1));
  return out;
}

const rl = createInterface({ input: process.stdin });
rl.on("line", async (line) => {
  if (!line.trim()) return;
  const msg = JSON.parse(line);
  if (msg.type === "dump") {
    outbox({ type: "dump", page: dump(page) });
  } else if (onmessage) {
    await onmessage(msg);
  }
});
