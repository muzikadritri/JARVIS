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
  "Instrument Serif": ["Regular", "Italic"],
  "DM Sans": ["Regular", "Medium", "SemiBold", "Bold"],
};
const loadedFonts = new Set();
const images = new Map();
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
      const extra = Object.keys(p.color || {}).filter((k) => !["r", "g", "b"].includes(k));
      if (extra.length) throw new Error(`Invalid SolidPaint color: unexpected key(s) ${extra.join(",")} (RGB only, use opacity)`);
      for (const ch of ["r", "g", "b"]) {
        const v = p.color && p.color[ch];
        if (typeof v !== "number" || v < 0 || v > 1) throw new Error(`Invalid color channel ${ch}=${v} (expected 0..1)`);
      }
    } else if (["GRADIENT_LINEAR", "GRADIENT_RADIAL", "GRADIENT_ANGULAR", "GRADIENT_DIAMOND"].includes(p.type)) {
      if (!Array.isArray(p.gradientStops) || p.gradientStops.length < 2) throw new Error("gradient needs 2+ stops");
      if (!Array.isArray(p.gradientTransform) || p.gradientTransform.length !== 2) throw new Error("bad gradientTransform");
    } else if (p.type === "IMAGE") {
      if (typeof p.imageHash !== "string" || !images.has(p.imageHash)) throw new Error("IMAGE paint with an unknown imageHash");
      if (!["FILL", "FIT", "CROP", "TILE"].includes(p.scaleMode)) throw new Error(`Invalid scaleMode ${p.scaleMode}`);
      for (const [k, v] of Object.entries(p.filters || {})) {
        if (typeof v !== "number" || v < -1 || v > 1) throw new Error(`Invalid image filter ${k}=${v}`);
      }
    } else {
      throw new Error(`unsupported paint type ${p.type}`);
    }
  }
}

// As in the Figma builds that predate NOISE: an unknown effect type throws.
const EFFECT_TYPES = ["DROP_SHADOW", "INNER_SHADOW", "LAYER_BLUR", "BACKGROUND_BLUR"];
function checkEffects(effects) {
  if (!Array.isArray(effects)) throw new Error("effects must be an array");
  for (const e of effects) {
    if (!EFFECT_TYPES.includes(e.type)) throw new Error(`Invalid effect type ${e.type}`);
    if (typeof e.radius !== "number" || e.radius < 0) throw new Error(`effect radius must be >= 0`);
    if (typeof e.visible !== "boolean") throw new Error("effect.visible is required");
    if (e.type.endsWith("SHADOW")) {
      if (!e.color || typeof e.color.a !== "number") throw new Error("shadow color needs r,g,b,a");
      if (!e.offset || typeof e.offset.x !== "number") throw new Error("shadow offset is required");
      if (typeof e.blendMode !== "string") throw new Error("shadow blendMode is required");
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
    this.strokes = []; this._effects = []; this.opacity = 1;
    this.constraints = { horizontal: "MIN", vertical: "MIN" };
    this.layoutGrow = 0;
    this._sizingH = "FIXED"; this._sizingV = "FIXED";
    registry.set(this.id, this);
  }
  get fills() { return this._fills; }
  set fills(v) { checkPaints(v); this._fills = v; }
  get effects() { return this._effects; }
  set effects(v) { checkEffects(v); this._effects = v; }
  get layoutPositioning() { return this._positioning || "AUTO"; }
  set layoutPositioning(v) {
    if (v === "ABSOLUTE" && !(this.parent && this.parent.layoutMode && this.parent.layoutMode !== "NONE")) {
      throw new Error("ABSOLUTE requires an auto-layout parent");
    }
    this._positioning = v;
  }
  get maxWidth() { return this._maxWidth ?? null; }
  set maxWidth(v) {
    const auto = (n) => n && n.layoutMode && n.layoutMode !== "NONE";
    if (!auto(this) && !auto(this.parent)) throw new Error("maxWidth requires auto layout");
    this._maxWidth = v;
  }
  findAll(fn) {
    const out = [];
    const walk = (n) => { for (const c of n.children || []) { if (fn(c)) out.push(c); walk(c); } };
    walk(this);
    return out;
  }
  findAllWithCriteria({ types }) { return this.findAll((n) => types.includes(n.type)); }
  findOne(fn) { return this.findAll(fn)[0] || null; }
  resize(w, h) {
    for (const [prop, v] of [["width", w], ["height", h]]) {
      if (typeof v !== "number") {
        throw new Error(`in resize: Property "${prop}" failed validation: Expected number, received ${typeof v}`);
      }
    }
    if (!(w >= 0.01 && h >= 0.01)) throw new Error(`resize: invalid size ${w}x${h}`);
    this.width = w; this.height = h;
    // As in Figma: resize() fixes the size. On an auto-layout frame both
    // sizing modes become FIXED; on text, auto-resize is switched off.
    if (this.layoutMode && this.layoutMode !== "NONE") {
      this._primaryAxisSizingMode = "FIXED";
      this._counterAxisSizingMode = "FIXED";
      this._sizingH = "FIXED";
      this._sizingV = "FIXED";
    }
    if (this.type === "TEXT") this._textAutoResize = "NONE";
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
    if (v === "FILL" && this.layoutPositioning === "ABSOLUTE") throw new Error("FILL cannot be set on absolute positioned auto-layout children");
    const selfAuto = this.layoutMode && this.layoutMode !== "NONE";
    if (v === "HUG" && !selfAuto && this.type !== "TEXT") throw new Error("HUG can only be set on auto-layout frames and text");
    if (axis === "H") this._sizingH = v; else this._sizingV = v;
  }
  async exportAsync(settings) {
    if (!settings || settings.format !== "PNG") throw new Error("mock exports PNG only");
    // Big enough that its base64 is far past any accidental string limit.
    const bytes = new Uint8Array(6000);
    bytes.set([137, 80, 78, 71, 13, 10, 26, 10]);
    return bytes;
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
    this.cornerRadius = 0; this.clipsContent = true; this.strokeWeight = 1; this.cornerSmoothing = 0;
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
// As in Figma: BASELINE exists only on horizontal auto layout.
{
  const base = Object.getOwnPropertyDescriptor(MockFrame.prototype, "counterAxisAlignItems");
  Object.defineProperty(MockFrame.prototype, "counterAxisAlignItems", {
    configurable: true,
    get: base.get,
    set(v) {
      if (v === "BASELINE" && this.layoutMode !== "HORIZONTAL") {
        throw new Error('"BASELINE" can only be set on horizontal auto-layout frames');
      }
      base.set.call(this, v);
    },
  });
}

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
    this.leadingTrim = "NONE";
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
  getNodeById() {
    throw new Error("in getNodeById: Cannot call with documentAccess: dynamic-page. Use figma.getNodeByIdAsync instead.");
  },
  createImage(bytes) {
    if (!(bytes instanceof Uint8Array) || bytes.length < 4) throw new Error("createImage expects image bytes");
    const hash = `img${images.size + 1}`;
    images.set(hash, bytes.length);
    return { hash };
  },
  base64Decode(text) { return new Uint8Array(Buffer.from(String(text), "base64")); },
  async listAvailableFontsAsync() {
    return Object.entries(FONTS).flatMap(([family, styles]) => styles.map((style) => ({ fontName: { family, style } })));
  },
  createNodeFromSvg(svg) {
    const text = String(svg);
    for (const attr of ["viewBox", "width", "height"]) {
      if (!new RegExp(`<svg[^>]*\\s${attr}=`).test(text)) throw new Error(`SVG needs a ${attr} attribute`);
    }
    if (!/<(path|circle|rect|line|polyline|polygon)\b/.test(text)) throw new Error("SVG has no shapes");
    const f = new MockFrame();
    f.name = "Svg";
    page.appendChild(f);
    const v = new MockNode("VECTOR");
    f.appendChild(v);
    return f;
  },
  getImageByHash(hash) { return images.has(hash) ? { hash } : null; },
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
