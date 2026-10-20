// JARVIS design kit — prepended to every job JARVIS sends to Figma.
//
// Generated code calls these instead of the raw Plugin API for the things a
// model reliably gets wrong — fonts loaded before text, FILL only inside room
// to fill, resize() before sizing, colours in 0..1, font style names that
// differ per family — and for the things that separate a designed page from a
// template: a type scale, a 12-column grid, real photos, SVG icons, layered
// shadows, glass and light. Everything lives under ONE name, `J`, so a job can
// declare whatever it likes without colliding with the kit.
//
// Rules this file follows come from Figma's own plugin guidance (figma-use):
// resize() resets auto-layout sizing, HUG parents collapse FILL children,
// wrapping text needs HEIGHT auto-resize before FILL, SOLID paint colours are
// {r,g,b} only, font style names are discovered rather than guessed.
const J = (() => {
  // Every node this job creates through the kit, so a job that fails halfway
  // can be undone exactly — code.js reports this list with the error.
  const created = (globalThis.__jarvisCreated = []);
  const warnings = [];
  const warn = (message) => {
    if (warnings.length < 60) warnings.push(String(message));
  };
  const track = (node) => {
    created.push(node.id);
    return node;
  };

  const theme = {
    bg: "#0B0F17", surface: "#121826", surface2: "#1A2233", text: "#F5F7FB",
    muted: "#9AA4B2", primary: "#4F8CFF", accent: "#22D3EE", onPrimary: "#FFFFFF",
    border: "#263045", inverseBg: "", inverseText: "", radius: 16,
    headingFont: "Inter", bodyFont: "Inter", headingWeight: "Bold",
    width: 1440, pagePad: 120, sectionPad: 128, photoFilters: null,
  };

  const STYLE_ALIASES = {
    "Semi Bold": ["Semi Bold", "SemiBold", "Semibold", "DemiBold", "Bold"],
    "SemiBold": ["SemiBold", "Semi Bold", "Semibold", "DemiBold", "Bold"],
    "Extra Bold": ["Extra Bold", "ExtraBold", "Black", "Bold"],
    "ExtraBold": ["ExtraBold", "Extra Bold", "Black", "Bold"],
    "Medium": ["Medium", "Regular"],
    "Light": ["Light", "Regular"],
    "Black": ["Black", "Extra Bold", "ExtraBold", "Bold"],
    "Bold": ["Bold", "Semi Bold", "SemiBold"],
    "Regular": ["Regular", "Book", "Normal", "Roman"],
    "Italic": ["Italic", "Regular Italic", "Regular"],
  };
  const WEIGHT_NAMES = {
    300: "Light", 400: "Regular", 500: "Medium", 600: "Semi Bold",
    700: "Bold", 800: "Extra Bold", 900: "Black",
  };
  const fontCache = new Map();
  let fontIndex = null;

  // Binary assets JARVIS fetched for this job (photos, as base64) and the SVG
  // bodies of the icons this job uses, both by key.
  const assets = {};
  const icons = {};
  const hashes = {};
  function setAssets(map) {
    if (map && typeof map === "object") Object.assign(assets, map);
    return assets;
  }
  function setIcons(map) {
    if (map && typeof map === "object") Object.assign(icons, map);
    return icons;
  }

  function setTheme(overrides) {
    if (overrides && typeof overrides === "object") Object.assign(theme, overrides);
    if (!theme.inverseBg) theme.inverseBg = theme.text;
    if (!theme.inverseText) theme.inverseText = theme.bg;
    return theme;
  }
  setTheme({});

  const mobile = () => theme.width < 600;

  // Which theme surface each frame paints, so text and components placed on
  // an inverse or primary band pick readable colours by themselves.
  const surfaceOf = new Map();
  function tone(parent) {
    let p = parent;
    while (p && !surfaceOf.has(p.id)) p = p.parent;
    return p ? surfaceOf.get(p.id) : "bg";
  }
  function onColor(parent, key) {
    const t = tone(parent);
    if (t === "inverseBg") {
      if (key === "text") return "inverseText";
      if (key === "muted") return paint(theme.inverseText, 0.72);
      if (key === "border") return paint(theme.inverseText, 0.22);
    }
    if (t === "primary") {
      if (key === "text" || key === "muted" || key === "primary") return "onPrimary";
      if (key === "border") return paint(theme.onPrimary, 0.3);
    }
    return key;
  }

  // ── colour ────────────────────────────────────────────────────────────────
  // SolidPaint.color is {r, g, b} ONLY — an `a` key makes Figma reject the
  // paint, so an RGBA object is reduced here and its alpha goes to opacity.
  function hex(color) {
    if (color && typeof color === "object" && "r" in color) return { r: color.r, g: color.g, b: color.b };
    let value = String(color || "#000000").trim().replace(/^#/, "");
    if (value.length === 3 || value.length === 4) value = [...value].map((c) => c + c).join("");
    if (!/^[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(value)) value = "000000";
    const n = parseInt(value.slice(0, 6), 16);
    return { r: ((n >> 16) & 255) / 255, g: ((n >> 8) & 255) / 255, b: (n & 255) / 255 };
  }

  function alphaOf(color) {
    if (color && typeof color === "object") return typeof color.a === "number" ? color.a : 1;
    const value = String(color || "").trim().replace(/^#/, "");
    if (value.length === 8) return parseInt(value.slice(6, 8), 16) / 255;
    if (value.length === 4) return parseInt(value[3] + value[3], 16) / 255;
    return 1;
  }

  const resolve = (color) => (typeof color === "string" && theme[color]) || color;

  function luminance(color) {
    const c = hex(resolve(color));
    const lin = (v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
    return 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  }
  const isDark = (color) => luminance(color) < 0.2;

  function paint(color, opacity) {
    if (color && typeof color === "object" && typeof color.type === "string") return color;
    const resolved = resolve(color);
    return { type: "SOLID", color: hex(resolved), opacity: opacity ?? alphaOf(resolved) };
  }
  const paints = (fill) =>
    fill === undefined || fill === null ? [] : [].concat(fill)
      .filter((f) => f && f !== "transparent" && f !== "none").map((f) => paint(f));

  // Linear gradient at `angle` degrees (0 = left→right, 90 = top→bottom), or
  // with {css: true} a CSS angle (90deg = left→right). {type: "radial"} makes a
  // soft radial light; scale below 1 makes it larger than the shape.
  function gradient(stops, angle = 135, opts = {}) {
    const list = (stops && stops.length ? stops : [theme.primary, theme.accent]).map((stop, i, all) => {
      const color = typeof stop === "string" || (stop && "r" in stop) ? stop : stop.color;
      const position = stop && typeof stop === "object" && stop.position !== undefined
        ? stop.position : i / Math.max(1, all.length - 1);
      const resolved = resolve(color);
      return { position, color: { ...hex(resolved), a: alphaOf(resolved) } };
    });
    if (opts.type === "radial") {
      const [sx, sy] = opts.scale || [0.7, 0.7];
      const [cx, cy] = opts.center || [0.5, 0.5];
      return {
        type: "GRADIENT_RADIAL",
        gradientTransform: [[sx, 0, 0.5 - sx * cx], [0, sy, 0.5 - sy * cy]],
        gradientStops: list,
      };
    }
    const a = opts.css ? (num(angle) ?? 180) - 90 : (num(angle) ?? 135);
    const rad = (a * Math.PI) / 180;
    const cos = Math.cos(rad);
    const sin = Math.sin(rad);
    return {
      type: "GRADIENT_LINEAR",
      gradientTransform: [[cos, sin, 0.5 - 0.5 * cos - 0.5 * sin], [-sin, cos, 0.5 + 0.5 * sin - 0.5 * cos]],
      gradientStops: list,
    };
  }

  // Layered, low-alpha elevation: a contact shadow plus an ambient one. One
  // heavy shadow is what makes a design look like a 2015 template.
  const SHADOW_LAYERS = {
    1: [[0, 1, 2, 0.05], [0, 4, 12, 0.06]],
    2: [[0, 1, 2, 0.05], [0, 12, 32, 0.08]],
    3: [[0, 2, 4, 0.06], [0, 24, 64, 0.12]],
  };
  function shadows(level = 2, color = "#0B1220") {
    const layers = SHADOW_LAYERS[Math.max(1, Math.min(3, num(level) || 2))];
    return layers.map(([x, y, blur, a]) => ({
      type: "DROP_SHADOW", color: { ...hex(resolve(color)), a }, offset: { x, y },
      radius: blur, spread: 0, visible: true, blendMode: "NORMAL",
    }));
  }
  function shadow(level = 2, color = "#0B1220") {
    return shadows(level, color)[1];
  }
  const innerHighlight = () => ({
    type: "INNER_SHADOW", color: { r: 1, g: 1, b: 1, a: 0.06 }, offset: { x: 0, y: 1 },
    radius: 0, spread: 0, visible: true, blendMode: "NORMAL",
  });

  // ── fonts ─────────────────────────────────────────────────────────────────
  // Style names differ per family and per file, so they are looked up in what
  // this Figma actually has, never guessed. A fallback is reported, not hidden.
  async function availableFonts() {
    if (fontIndex) return fontIndex;
    fontIndex = new Map();
    try {
      for (const f of await figma.listAvailableFontsAsync()) {
        const { family, style } = f.fontName;
        if (!fontIndex.has(family)) fontIndex.set(family, []);
        fontIndex.get(family).push(style);
      }
    } catch (err) {
      fontIndex = null;
    }
    return fontIndex;
  }
  const norm = (s) => String(s).toLowerCase().replace(/[\s_-]/g, "");

  async function font(family, style = "Regular") {
    const wanted = typeof style === "number" ? WEIGHT_NAMES[style] || "Regular" : String(style || "Regular");
    const key = `${family}::${wanted}`;
    if (fontCache.has(key)) return fontCache.get(key);
    const aliases = STYLE_ALIASES[wanted] || [wanted, "Regular"];
    const families = [family || theme.bodyFont, theme.bodyFont, "Inter"];
    const index = await availableFonts();
    for (const fam of families) {
      let candidates = aliases;
      if (index) {
        const styles = index.get(fam);
        if (!styles) continue;
        const pick = aliases.map((a) => styles.find((s) => norm(s) === norm(a))).find(Boolean)
          || styles.find((s) => norm(s) === "regular") || styles[0];
        candidates = [pick];
      }
      for (const sty of candidates) {
        try {
          const name = { family: fam, style: sty };
          await figma.loadFontAsync(name);
          if (fam !== family || norm(sty) !== norm(wanted)) warn(`font ${family} ${wanted} → ${fam} ${sty}`);
          fontCache.set(key, name);
          return name;
        } catch (err) {
          // not loadable under this name — try the next one
        }
      }
    }
    const fallback = { family: "Inter", style: "Regular" };
    await figma.loadFontAsync(fallback);
    warn(`font ${family} ${wanted} → Inter Regular`);
    fontCache.set(key, fallback);
    return fallback;
  }

  // The type scale. Roles keep hierarchy consistent across sections that are
  // written one at a time: [desktop size, mobile size], line height as a
  // multiplier, letter spacing in percent, and a readable measure for prose.
  const RAMP = {
    display: { font: "heading", size: [104, 52], lh: 0.98, ls: -4, trim: true },
    hero: { font: "heading", size: [80, 44], lh: 1.02, ls: -3.5, trim: true },
    h1: { font: "heading", size: [64, 40], lh: 1.05, ls: -3, trim: true },
    h2: { font: "heading", size: [48, 32], lh: 1.1, ls: -2 },
    h3: { font: "heading", size: [32, 24], lh: 1.2, ls: -1 },
    h4: { font: "heading", size: [24, 20], lh: 1.3, ls: -0.5 },
    stat: { font: "heading", size: [64, 40], lh: 1, ls: -3 },
    quote: { font: "heading", size: [32, 24], lh: 1.25, ls: -1, weight: "Medium" },
    lead: { font: "body", size: [22, 18], lh: 1.5, ls: -0.5, weight: "Regular", max: 640 },
    body: { font: "body", size: [18, 16], lh: 1.6, ls: 0, weight: "Regular", max: 640 },
    small: { font: "body", size: [15, 14], lh: 1.5, ls: 0, weight: "Regular" },
    eyebrow: { font: "body", size: [13, 12], lh: 1.2, ls: 12, weight: "Semi Bold", upper: true },
    button: { font: "body", size: [16, 15], lh: 1, ls: 0, weight: "Semi Bold" },
    caption: { font: "body", size: [13, 12], lh: 1.4, ls: 0, weight: "Medium" },
  };

  // ── option hygiene ────────────────────────────────────────────────────────
  // Models hand over "120px", "100%", "auto" — even objects — where Figma
  // wants a number, and Figma then throws deep inside resize() ("Expected
  // number, received object"). Every numeric option passes through here: a
  // usable number, or absent so the default applies. "100%" for a width
  // means what it says, so it becomes fillWidth. Gaps and paddings snap to
  // the spacing scale when they are close, which keeps the rhythm even.
  const NUMERIC = ["width", "height", "w", "h", "gap", "rowGap", "radius", "strokeWeight",
    "opacity", "size", "box", "x", "y", "angle", "maxWidth", "minWidth", "max", "blur", "span", "cols"];
  const SPACE = [0, 4, 8, 12, 16, 24, 32, 40, 48, 64, 80, 96, 128, 160];
  function num(value) {
    if (typeof value === "number") return Number.isFinite(value) ? value : undefined;
    if (typeof value === "string") {
      const m = value.trim().match(/^-?\d+(?:\.\d+)?(?=(?:px)?$)/);
      return m ? parseFloat(m[0]) : undefined;
    }
    return undefined;
  }
  function snap(value) {
    if (typeof value !== "number" || value > 160 || value < 0) return value;
    const nearest = SPACE.reduce((a, b) => (Math.abs(b - value) < Math.abs(a - value) ? b : a));
    return Math.abs(nearest - value) <= Math.max(2, value * 0.25) ? nearest : value;
  }
  function clean(opts) {
    const out = { ...(opts || {}) };
    if (out.width === "100%" || out.w === "100%") out.fillWidth = true;
    if (out.height === "100%" || out.h === "100%") out.fillHeight = true;
    for (const key of NUMERIC) {
      if (!(key in out) || out[key] === true) continue;
      if (key === "size" && /^(sm|md|lg)$/.test(String(out[key]))) continue;
      const value = num(out[key]);
      if (value === undefined) delete out[key];
      else out[key] = value;
    }
    for (const key of ["width", "height", "w", "h", "box", "maxWidth", "minWidth"]) {
      if (key in out && out[key] < 1) delete out[key];
    }
    for (const key of ["gap", "rowGap"]) if (key in out) out[key] = snap(out[key]);
    if ("pad" in out) {
      out.pad = Array.isArray(out.pad) ? out.pad.map((v) => snap(num(v) ?? 0)) : snap(num(out.pad));
      if (out.pad === undefined) delete out.pad;
    }
    return out;
  }

  function padding(node, pad) {
    if (pad === undefined || pad === null) return;
    const [t, r, b, l] = Array.isArray(pad)
      ? (pad.length === 2 ? [pad[0], pad[1], pad[0], pad[1]] : pad.length === 4 ? pad : [pad[0], pad[0], pad[0], pad[0]])
      : [pad, pad, pad, pad];
    node.paddingTop = t; node.paddingRight = r; node.paddingBottom = b; node.paddingLeft = l;
  }

  const ALIGN = {
    start: "MIN", center: "CENTER", end: "MAX", between: "SPACE_BETWEEN", baseline: "BASELINE", stretch: "MIN",
  };
  const isAuto = (n) => n && "layoutMode" in n && n.layoutMode !== "NONE";

  // A FILL child inside a HUG parent collapses to nothing (figma-use gotcha),
  // so a parent that hugs is widened to FILL its own parent where it can.
  function makeRoomX(parent) {
    if (!parent || !("layoutSizingHorizontal" in parent) || parent.layoutSizingHorizontal !== "HUG") return true;
    const grand = parent.parent;
    if (isAuto(grand) && parent.layoutPositioning !== "ABSOLUTE" && makeRoomX(grand)) {
      parent.layoutSizingHorizontal = "FILL";
      return true;
    }
    return false;
  }

  // Sizing can only be applied once the node sits in an auto-layout parent;
  // setting FILL earlier throws. `attach` appends and then applies it.
  function attach(node, parent, opts = {}) {
    if (!parent || !("appendChild" in parent)) return node;
    parent.appendChild(node);
    if (isAuto(parent) && opts.absolute) {
      // Decoration (glows, oversized numerals, floating badges): out of the
      // flow, positioned, never FILL — Figma refuses FILL on absolute nodes.
      node.layoutPositioning = "ABSOLUTE";
      if (opts.x !== undefined) node.x = opts.x;
      if (opts.y !== undefined) node.y = opts.y;
      try {
        node.constraints = opts.constraints || { horizontal: "MIN", vertical: "MIN" };
      } catch (err) {
        warn(`${node.name}: constraints not set`);
      }
      return node;
    }
    if (isAuto(parent)) {
      const row = parent.layoutMode === "HORIZONTAL";
      // "grow" in a column that hugs its height means "take the full width";
      // layoutGrow there would squash the child to its empty size.
      const growsAcross = opts.grow && !row && parent.primaryAxisSizingMode !== "FIXED";
      if (node.type === "TEXT" && (opts.fillWidth || growsAcross || (opts.grow && row))) node.textAutoResize = "HEIGHT";
      if (opts.fillWidth || growsAcross) {
        if (makeRoomX(parent)) node.layoutSizingHorizontal = "FILL";
        else warn(`${node.name}: fillWidth inside a parent that hugs its content`);
      }
      const verticalRoom = (row ? parent.counterAxisSizingMode : parent.primaryAxisSizingMode) === "FIXED";
      if (opts.fillHeight && verticalRoom) node.layoutSizingVertical = "FILL";
      if (opts.grow && row) {
        if (makeRoomX(parent)) node.layoutSizingHorizontal = "FILL";
        else warn(`${node.name}: grow inside a row that hugs its width`);
      } else if (opts.grow && !row && parent.primaryAxisSizingMode === "FIXED") {
        node.layoutSizingVertical = "FILL";
      }
      for (const key of ["maxWidth", "minWidth"]) {
        if (opts[key]) {
          try {
            node[key] = opts[key];
          } catch (err) {
            warn(`${node.name}: ${key} unsupported here`);
          }
        }
      }
    } else {
      if (opts.x !== undefined) node.x = opts.x;
      if (opts.y !== undefined) node.y = opts.y;
    }
    return node;
  }

  // ── building blocks ───────────────────────────────────────────────────────
  function frame(opts = {}) {
    opts = clean(opts);
    const node = track(figma.createFrame());
    node.name = opts.name || "Frame";
    const dir = opts.dir === undefined ? "VERTICAL" : opts.dir;
    const wraps = Boolean(opts.wrap) && dir === "HORIZONTAL";
    // A wrapping row only wraps when something other than its content decides
    // its width; hugging, it just grows wider. So it fills its parent unless
    // it was given a width.
    // SPACE_BETWEEN only spreads items when the row has width to share.
    const spreads = dir === "HORIZONTAL" && opts.justify === "between";
    const fillWidth = opts.fillWidth ?? ((wraps || spreads) && !opts.width ? true : undefined);
    let fill = opts.fill;
    if (opts.glass && fill === undefined) fill = paint("surface", 0.6);
    node.fills = paints(fill);
    if (typeof fill === "string" && theme[fill] !== undefined) surfaceOf.set(node.id, fill);
    // resize() FIRST: on an auto-layout frame it resets both sizing modes to
    // FIXED, so hugging set before it would silently be lost.
    if (opts.width || opts.height) {
      node.resize(opts.width || node.width || 100, opts.height || node.height || 100);
    }
    if (dir !== "NONE") {
      node.layoutMode = dir;
      node.itemSpacing = opts.gap ?? 0;
      padding(node, opts.pad ?? 0);
      node.primaryAxisAlignItems = ALIGN[opts.justify] || "MIN";
      const cross = ALIGN[opts.align] || "MIN";
      // BASELINE exists only on horizontal frames; SPACE_BETWEEN only on the main axis.
      node.counterAxisAlignItems =
        cross === "SPACE_BETWEEN" || (cross === "BASELINE" && dir !== "HORIZONTAL") ? "MIN" : cross;
      if (wraps) {
        node.layoutWrap = "WRAP";
        node.counterAxisSpacing = opts.rowGap ?? opts.gap ?? 0;
      }
      node.layoutSizingHorizontal = opts.width ? "FIXED" : "HUG";
      node.layoutSizingVertical = opts.height ? "FIXED" : "HUG";
    }
    if (opts.radius !== undefined) node.cornerRadius = opts.radius === true ? theme.radius : opts.radius;
    if (opts.smooth) {
      try {
        node.cornerSmoothing = 0.6;
      } catch (err) {
        warn("corner smoothing unsupported");
      }
    }
    const strokeColor = opts.stroke || (opts.glass ? "#FFFFFF24" : null);
    if (strokeColor) {
      node.strokes = [paint(strokeColor)];
      node.strokeWeight = opts.strokeWeight || 1;
      node.strokeAlign = "INSIDE";
    }
    const effects = [];
    if (opts.shadow) {
      const level = opts.shadow === true ? 2 : opts.shadow;
      // On dark backgrounds a drop shadow is invisible mud; a hairline and an
      // inner highlight read as depth instead.
      if (isDark("bg") && !opts.glass) {
        if (!strokeColor) {
          node.strokes = [paint("border")];
          node.strokeWeight = 1;
          node.strokeAlign = "INSIDE";
        }
        effects.push(innerHighlight());
      } else {
        effects.push(...shadows(level));
      }
    }
    if (opts.glass) effects.push({ type: "BACKGROUND_BLUR", radius: opts.blur || 24, visible: true });
    if (effects.length) node.effects = effects;
    if (opts.grain) {
      try {
        node.effects = [...node.effects, {
          type: "NOISE", noiseType: "MONOTONE", color: { r: 0, g: 0, b: 0, a: 0.08 },
          blendMode: "NORMAL", noiseSize: 0.6, density: 0.6, visible: true,
        }];
      } catch (err) {
        warn("grain unsupported in this Figma");
      }
    }
    node.clipsContent = Boolean(opts.clip);
    if (opts.opacity !== undefined) node.opacity = opts.opacity;
    return attach(node, opts.parent, { ...opts, fillWidth });
  }

  const stack = (opts = {}) => frame({ name: "Stack", ...opts, dir: "VERTICAL" });
  const hstack = (opts = {}) => frame({ name: "Row", align: "center", ...opts, dir: "HORIZONTAL" });

  function parseLineHeight(value) {
    if (typeof value === "string" && value.trim().endsWith("%")) {
      return { value: parseFloat(value), unit: "PERCENT" };
    }
    const n = num(value);
    if (n === undefined) return null;
    return n <= 4 ? { value: n * 100, unit: "PERCENT" } : { value: n, unit: "PIXELS" };
  }
  function parseSpacing(value) {
    if (typeof value === "string" && value.trim().endsWith("px")) return { value: parseFloat(value), unit: "PIXELS" };
    const n = typeof value === "number" ? value : parseFloat(value);
    return Number.isFinite(n) ? { value: n, unit: "PERCENT" } : null;
  }

  async function text(content, opts = {}) {
    const rawLineHeight = opts && opts.lineHeight;
    const rawSpacing = opts && opts.letterSpacing;
    opts = clean(opts);
    const role = opts.role || (opts.heading ? "h2" : null);
    const ramp = RAMP[role] || null;
    if (role && !ramp) warn(`unknown text role "${role}"`);
    const node = track(figma.createText());
    const headingFace = ramp ? ramp.font === "heading" : Boolean(opts.heading);
    const family = opts.family || (headingFace ? theme.headingFont : theme.bodyFont);
    const weight = opts.weight || (ramp ? ramp.weight || (headingFace ? theme.headingWeight : "Regular")
      : (opts.heading ? theme.headingWeight : "Regular"));
    node.fontName = await font(family, weight);
    const maxWidth = opts.maxWidth ?? (ramp && ramp.max && !opts.width ? ramp.max : undefined);
    const wraps = Boolean(opts.width || opts.fillWidth || maxWidth);
    // HEIGHT before the characters, so the text wraps instead of running out
    // as one endless line, and before FILL, which TEXT ignores otherwise.
    if (wraps) node.textAutoResize = "HEIGHT";
    node.characters = String(content ?? "");
    node.fontSize = opts.size || (ramp ? ramp.size[mobile() ? 1 : 0] : 16);
    const color = opts.color ? (["text", "muted"].includes(opts.color) ? onColor(opts.parent, opts.color) : opts.color)
      : onColor(opts.parent, "text");
    node.fills = [paint(color)];
    node.name = opts.name || node.characters.slice(0, 40) || "Text";
    const lh = parseLineHeight(rawLineHeight ?? (ramp ? ramp.lh : undefined));
    if (lh) node.lineHeight = lh;
    const ls = rawSpacing !== undefined ? parseSpacing(rawSpacing) : (ramp ? parseSpacing(ramp.ls) : null);
    if (ls) node.letterSpacing = ls;
    if (opts.upper ?? (ramp && ramp.upper)) node.textCase = "UPPER";
    if (opts.align) {
      node.textAlignHorizontal = { left: "LEFT", center: "CENTER", right: "RIGHT", justify: "JUSTIFIED" }[opts.align] || "LEFT";
    }
    if (opts.trim ?? (ramp && ramp.trim)) {
      try {
        node.leadingTrim = "CAP_HEIGHT";
      } catch (err) {
        // older Figma: no leading trim; spacing is simply a little looser
      }
    }
    if (opts.width) {
      // resize() fixes both dimensions; HEIGHT afterwards lets it grow with the text.
      node.resize(opts.width, Math.max(1, node.height));
      node.textAutoResize = "HEIGHT";
    }
    const fill = opts.fillWidth || (maxWidth && !opts.width);
    attach(node, opts.parent, { ...opts, fillWidth: fill, maxWidth });
    if (wraps && node.width < 4 && node.characters.length > 2) {
      const p = node.parent;
      const room = p && p.width ? p.width - (p.paddingLeft || 0) - (p.paddingRight || 0) : 480;
      const w = Math.max(160, Math.min(maxWidth || room, room));
      node.layoutSizingHorizontal = "FIXED";
      node.resize(w, Math.max(1, node.height));
      node.textAutoResize = "HEIGHT";
      warn(`${node.name}: collapsed text repaired to ${Math.round(w)}px`);
    }
    const p = node.parent;
    if (!wraps && !opts.nowrap && isAuto(p) && p.layoutMode === "VERTICAL"
        && node.layoutPositioning !== "ABSOLUTE" && node.characters.length > 2) {
      // A one-line heading wider than its column wraps instead of running out
      // under the next column or past the clipped section edge.
      let box = p;
      while (box && box.layoutSizingHorizontal === "HUG" && isAuto(box.parent)) box = box.parent;
      const room = box && box.width ? box.width - (box.paddingLeft || 0) - (box.paddingRight || 0) : contentWidth();
      if (node.width > room + 1) {
        if (!opts.align) node.textAlignHorizontal = { CENTER: "CENTER", MAX: "RIGHT" }[p.counterAxisAlignItems] || "LEFT";
        node.textAutoResize = "HEIGHT";
        if (makeRoomX(p)) {
          node.layoutSizingHorizontal = "FILL";
        } else {
          node.layoutSizingHorizontal = "FIXED";
          node.resize(Math.max(160, room), Math.max(1, node.height));
          node.textAutoResize = "HEIGHT";
        }
        warn(`${node.name}: long line wrapped to fit`);
      }
    }
    return node;
  }

  function rect(opts = {}) {
    opts = clean(opts);
    const node = track(figma.createRectangle());
    node.name = opts.name || "Rectangle";
    node.resize(opts.w || opts.width || 100, opts.h || opts.height || 100);
    node.fills = paints(opts.fill === undefined ? "surface2" : opts.fill);
    if (opts.radius !== undefined) node.cornerRadius = opts.radius === true ? theme.radius : opts.radius;
    return attach(node, opts.parent, opts);
  }

  function ellipse(opts = {}) {
    opts = clean(opts);
    const node = track(figma.createEllipse());
    node.name = opts.name || "Ellipse";
    node.resize(opts.w || opts.size || 48, opts.h || opts.size || 48);
    node.fills = paints(opts.fill === undefined ? "primary" : opts.fill);
    return attach(node, opts.parent, opts);
  }

  // Ambient light: a large blurred radial glow, absolute inside its parent.
  // Gradients as light, never as rainbow-filled boxes.
  function glow(opts = {}) {
    opts = clean(opts);
    const size = opts.size || 640;
    const color = resolve(opts.color || "primary");
    const node = ellipse({
      name: opts.name || "Glow", size, parent: opts.parent, absolute: true,
      x: opts.x ?? Math.round((theme.width - size) / 2), y: opts.y ?? -Math.round(size / 3),
      fill: gradient([{ color: { ...hex(color), a: opts.strength ?? 0.55 }, position: 0 },
        { color: { ...hex(color), a: 0 }, position: 1 }], 0, { type: "radial", scale: [1, 1] }),
    });
    node.effects = [{ type: "LAYER_BLUR", radius: opts.blur || 120, visible: true }];
    return node;
  }

  function imagePaint(key, opts = {}) {
    if (!key) return null;
    let hash = hashes[key];
    if (!hash) {
      const source = assets[key];
      if (!source) return null;
      try {
        if (typeof source === "object" && source.hash) hash = source.hash;
        else hash = figma.createImage(figma.base64Decode(String(source).replace(/^data:[^,]+,/, ""))).hash;
      } catch (err) {
        warn(`photo ${key}: ${err.message || err}`);
        return null;
      }
      hashes[key] = hash;
    }
    const p = { type: "IMAGE", imageHash: hash, scaleMode: opts.fit === "contain" ? "FIT" : "FILL" };
    const filters = opts.filters || theme.photoFilters;
    if (filters) p.filters = filters;
    return p;
  }

  // A photo slot. With {asset: "hero"} it shows the real photo JARVIS fetched;
  // without one, a soft palette gradient — never a caption saying "Image".
  async function image(opts = {}) {
    opts = clean(opts);
    const photo = imagePaint(opts.asset, opts);
    if (opts.asset && !photo) warn(`no photo for "${opts.asset}" — gradient used`);
    const fills = photo ? [photo] : [gradient(opts.colors || ["surface2", "primary"], opts.angle ?? 135)];
    if (opts.overlay) {
      // Darkens the lower part so light text on the photo stays readable.
      fills.push(gradient([{ color: "#00000000", position: 0 }, { color: "#000000B3", position: 1 }], 90));
    }
    const node = frame({
      name: opts.name || (opts.asset ? `Image/${opts.asset}` : "Image"), dir: "VERTICAL",
      align: "center", justify: "center",
      width: opts.w || opts.width || (opts.fillWidth || (mobile() && !opts.absolute) ? undefined : 560),
      height: opts.h || opts.height || 360,
      fill: fills, radius: opts.radius ?? theme.radius, smooth: opts.smooth, clip: true,
      parent: opts.parent, grow: opts.grow, absolute: opts.absolute,
      fillWidth: opts.fillWidth ?? (mobile() && !opts.w && !opts.width && !opts.absolute ? true : undefined),
      x: opts.x, y: opts.y, shadow: opts.shadow,
    });
    if (typeof opts.label === "string" && opts.label) {
      await text(opts.label, { parent: node, role: "caption", color: "#FFFFFFCC" });
    }
    return node;
  }
  const photo = image;

  // An icon: a real SVG from the icon set when the name is known, drawn in the
  // theme colour; the old glyph tile otherwise. {tile: false} gives the bare icon.
  async function icon(name, opts = {}) {
    opts = clean(opts);
    const key = String(name || "").toLowerCase().trim();
    const body = icons[key];
    if (body && typeof figma.createNodeFromSvg === "function") {
      const size = opts.size || 22;
      const color = toHex(hex(resolve(opts.color || "primary")));
      const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="${color}" stroke-width="${opts.strokeWidth || 1.75}" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
      const vector = track(figma.createNodeFromSvg(svg));
      vector.name = `icon/${key}`;
      vector.fills = [];
      vector.resize(size, size);
      if (opts.tile === false || opts.bare) return attach(vector, opts.parent, opts);
      const box = opts.box || Math.round(size * 2);
      const tile = frame({
        name: opts.name || `Icon/${key}`, dir: "VERTICAL", align: "center", justify: "center",
        width: box, height: box, fill: opts.fill || "surface2", radius: opts.radius ?? Math.round(box * 0.28),
        parent: opts.parent, absolute: opts.absolute, x: opts.x, y: opts.y,
      });
      tile.appendChild(vector);
      return tile;
    }
    if (key && !body) warn(`icon "${key}" is not in the icon set — glyph used`);
    if (opts.tile === false || opts.bare) return null;
    const box = opts.box || 44;
    const tile = frame({
      name: opts.name || "Icon", dir: "VERTICAL", align: "center", justify: "center",
      width: box, height: box, fill: opts.fill || "surface2", radius: opts.radius ?? 12,
      parent: opts.parent,
    });
    await text(key.length <= 2 ? key : "★", { parent: tile, size: opts.size || 20, weight: "Bold", color: opts.color || "primary" });
    return tile;
  }

  async function button(label, opts = {}) {
    opts = clean(opts);
    let variant = opts.variant || "primary";
    const band = tone(opts.parent);
    if (band === "primary" && variant === "primary") variant = luminance("primary") > 0.4 ? "dark" : "light";
    if (band === "inverseBg" && variant === "secondary") variant = "ghost";
    const styles = {
      primary: { fill: "primary", color: "onPrimary", stroke: null },
      secondary: { fill: "surface2", color: "text", stroke: "border" },
      ghost: { fill: null, color: "text", stroke: "border" },
      light: { fill: "#FFFFFF", color: "#0B0F17", stroke: null },
      dark: { fill: "#0B0F17", color: "#FFFFFF", stroke: null },
      link: { fill: null, color: "primary", stroke: null },
    }[variant] || { fill: "primary", color: "onPrimary", stroke: null };
    const size = opts.size === "lg" ? [18, 30, 17] : opts.size === "sm" ? [10, 18, 14] : [15, 24, 15];
    const node = frame({
      name: opts.name || `Button/${label}`, dir: "HORIZONTAL", gap: 10,
      pad: variant === "link" ? 0 : [size[0], size[1]], align: "center", justify: "center",
      fill: styles.fill, radius: opts.radius ?? 999, stroke: styles.stroke,
      parent: opts.parent, fillWidth: opts.fillWidth,
    });
    const labelColor = opts.labelColor || (styles.fill ? styles.color : onColor(opts.parent, styles.color));
    if (!styles.fill && styles.stroke) node.strokes = [paint(onColor(opts.parent, styles.stroke))];
    await text(label, { parent: node, role: "button", size: size[2], color: labelColor });
    if (opts.icon) await icon(opts.icon === true ? "arrow-right" : opts.icon, { parent: node, tile: false, size: 18, color: labelColor });
    return node;
  }

  // A small pill label: "New", "Most popular", "Case study".
  async function badge(label, opts = {}) {
    opts = clean(opts);
    const variant = opts.variant || "soft";
    const look = {
      soft: { fill: paint(opts.color || "primary", 0.14), color: opts.color || "primary", stroke: null },
      outline: { fill: null, color: onColor(opts.parent, "text"), stroke: onColor(opts.parent, "border") },
      solid: { fill: opts.color || "primary", color: "onPrimary", stroke: null },
      glass: { fill: paint("#FFFFFF", 0.1), color: "text", stroke: "#FFFFFF33" },
    }[variant] || { fill: paint("primary", 0.14), color: "primary", stroke: null };
    const node = frame({
      name: opts.name || `Badge/${label}`, dir: "HORIZONTAL", gap: 6, pad: [6, 12], align: "center",
      fill: look.fill, stroke: look.stroke, radius: 999, parent: opts.parent,
      absolute: opts.absolute, x: opts.x, y: opts.y,
    });
    if (opts.icon) await icon(opts.icon, { parent: node, tile: false, size: 14, color: look.color });
    await text(label, { parent: node, role: "caption", color: look.color });
    return node;
  }

  // A card container. Variants: plain, elevated, outline, glass, inverse.
  function card(opts = {}) {
    opts = clean(opts);
    const variant = opts.variant || "outline";
    const look = {
      plain: { fill: "surface" },
      elevated: { fill: "surface", shadow: 2 },
      outline: { fill: "surface", stroke: "border" },
      glass: { glass: true },
      inverse: { fill: "inverseBg" },
    }[variant] || { fill: "surface", stroke: "border" };
    return frame({
      name: opts.name || "Card", dir: opts.dir || "VERTICAL", gap: opts.gap ?? 16, pad: opts.pad ?? 32,
      radius: opts.radius ?? true, smooth: true, clip: opts.clip, ...look,
      ...(opts.fill !== undefined ? { fill: opts.fill } : {}),
      parent: opts.parent, width: opts.width, height: opts.height, fillWidth: opts.fillWidth,
      grow: opts.grow, align: opts.align, justify: opts.justify,
    });
  }

  // A big number with its label — "4.9★ average rating", "120k listeners".
  async function stat(value, label, opts = {}) {
    opts = clean(opts);
    const node = stack({ name: opts.name || `Stat/${label}`, gap: 8, parent: opts.parent, grow: opts.grow });
    await text(String(value), { parent: node, role: "stat", color: opts.color || "text" });
    await text(String(label), { parent: node, role: "small", color: opts.labelColor || "muted" });
    return node;
  }

  // A round avatar with initials — for testimonials without photos.
  async function avatar(name, opts = {}) {
    opts = clean(opts);
    const size = opts.size || 44;
    const initials = String(name || "?").split(/\s+/).map((p) => p[0] || "").join("").slice(0, 2).toUpperCase();
    const photoPaint = imagePaint(opts.asset, opts);
    const node = frame({
      name: opts.name || `Avatar/${name}`, dir: "VERTICAL", align: "center", justify: "center",
      width: size, height: size, radius: size / 2, clip: true, parent: opts.parent,
      fill: photoPaint || gradient([opts.color || "primary", "accent"], 135),
    });
    if (!photoPaint) await text(initials, { parent: node, size: Math.round(size * 0.38), weight: "Semi Bold", color: "onPrimary" });
    return node;
  }

  function divider(opts = {}) {
    opts = clean(opts);
    const line = rect({ name: opts.name || "Divider", w: 100, h: opts.weight || 1, fill: opts.color || "border", parent: opts.parent });
    if (isAuto(line.parent)) {
      if (makeRoomX(line.parent)) line.layoutSizingHorizontal = "FILL";
    }
    return line;
  }

  // ── layout ────────────────────────────────────────────────────────────────
  // A full-width page section, appended to the page root frame.
  function section(name, opts = {}) {
    opts = clean(opts);
    const sec = frame({
      name, dir: opts.dir || "VERTICAL", gap: opts.gap ?? (mobile() ? 32 : 48),
      pad: opts.pad ?? 0, align: opts.align || "start",
      justify: opts.justify, fill: opts.fill === undefined ? "bg" : opts.fill,
      parent: opts.parent, fillWidth: true, clip: opts.clip ?? true, grain: opts.grain,
    });
    // The theme's own padding is applied as is — snapping 120 to 128 would put
    // the content edge off the grid and off contentWidth().
    if (opts.pad === undefined) padding(sec, [mobile() ? 72 : theme.sectionPad, theme.pagePad]);
    return sec;
  }

  const contentWidth = () => theme.width - 2 * theme.pagePad;

  // Content kept to a readable width: put containers in a section with
  // align: "center" to centre them.
  function container(opts = {}) {
    opts = clean(opts);
    const max = Math.min(opts.max || 1200, contentWidth());
    return frame({
      name: opts.name || "Container", dir: opts.dir || "VERTICAL", gap: opts.gap ?? 24,
      align: opts.align, justify: opts.justify, parent: opts.parent,
      ...(mobile() ? { fillWidth: true } : { width: max }),
    });
  }

  // The 12-column grid: J.row({parent, cols: 12, gap: 24}) then J.col(row, 7)
  // and J.col(row, 5) for a 7/5 split. On mobile rows stack.
  const rowMeta = new Map();
  function row(opts = {}) {
    opts = clean(opts);
    const node = frame({
      name: opts.name || "Grid", dir: mobile() ? "VERTICAL" : "HORIZONTAL", gap: opts.gap ?? 24,
      align: opts.align || "start", parent: opts.parent, fillWidth: true,
    });
    rowMeta.set(node.id, { cols: opts.cols || 12, gap: opts.gap ?? 24, used: 0 });
    return node;
  }
  function col(parentRow, span, opts = {}) {
    opts = clean(opts);
    const meta = rowMeta.get(parentRow && parentRow.id) || { cols: 12, gap: 24, used: 0 };
    const s = Math.max(1, Math.min(meta.cols, num(span) || 1));
    meta.used += s;
    if (meta.used > meta.cols) warn(`${parentRow.name}: columns add up to ${meta.used} of ${meta.cols}`);
    if (mobile()) {
      return frame({ name: opts.name || `Col/${s}`, dir: opts.dir || "VERTICAL", gap: opts.gap ?? 16,
        align: opts.align, justify: opts.justify, pad: opts.pad, fill: opts.fill, radius: opts.radius,
        parent: parentRow, fillWidth: true, ...opts.frame });
    }
    let inner = parentRow.width - (parentRow.paddingLeft || 0) - (parentRow.paddingRight || 0);
    if (!(inner > 200)) inner = contentWidth();
    const colWidth = (inner - meta.gap * (meta.cols - 1)) / meta.cols;
    const width = Math.floor(colWidth * s + meta.gap * (s - 1));
    return frame({
      name: opts.name || `Col/${s}`, dir: opts.dir || "VERTICAL", gap: opts.gap ?? 16,
      align: opts.align, justify: opts.justify, pad: opts.pad, fill: opts.fill, radius: opts.radius,
      parent: parentRow, width, ...opts.frame,
    });
  }

  async function node(id) {
    const found = await figma.getNodeByIdAsync(String(id));
    if (!found) throw new Error(`node ${id} does not exist (deleted, or on another page)`);
    return found;
  }

  function focus(nodes) {
    const list = [].concat(nodes).filter(Boolean);
    if (!list.length) return;
    figma.viewport.scrollAndZoomIntoView(list);
    figma.currentPage.selection = list.filter((n) => n.parent && n.parent.type !== "DOCUMENT");
  }

  function summary(target, extra = {}) {
    return {
      id: target.id, name: target.name, type: target.type,
      width: Math.round(target.width), height: Math.round(target.height),
      created: created.length, warnings: warnings.slice(0, 20), ...extra,
    };
  }

  // A compact outline of a subtree, for reading a design back.
  function outline(target, depth = 3) {
    const out = { id: target.id, name: target.name, type: target.type };
    if ("width" in target) { out.w = Math.round(target.width); out.h = Math.round(target.height); }
    if (target.type === "TEXT") out.text = target.characters.slice(0, 160);
    if ("layoutMode" in target && target.layoutMode !== "NONE") out.layout = target.layoutMode;
    if (depth > 0 && "children" in target) {
      out.children = target.children.slice(0, 60).map((child) => outline(child, depth - 1));
    }
    return out;
  }

  function toHex(color, opacity = 1) {
    const part = (v) => Math.round(Math.max(0, Math.min(1, v)) * 255).toString(16).padStart(2, "0");
    const alpha = opacity < 1 ? part(opacity) : "";
    return `#${part(color.r)}${part(color.g)}${part(color.b)}${alpha}`.toUpperCase();
  }

  function describeFills(fills) {
    if (!Array.isArray(fills)) return undefined;
    const visible = fills.filter((f) => f.visible !== false);
    if (!visible.length) return undefined;
    const top = visible[visible.length - 1];
    if (top.type === "SOLID") return toHex(top.color, top.opacity ?? 1);
    if (top.type && top.type.startsWith("GRADIENT")) {
      return `gradient(${top.gradientStops.map((s) => toHex(s.color, s.color.a ?? 1)).join(", ")})`;
    }
    return top.type === "IMAGE" ? "image" : top.type.toLowerCase();
  }

  // Everything a developer needs to rebuild a subtree as HTML/CSS: layout,
  // spacing, sizing, colours and type. Mixed values (figma.mixed) are skipped
  // rather than reported as symbols nobody can use.
  function spec(target, depth = 6) {
    const n = (v) => (typeof v === "number" ? Math.round(v * 100) / 100 : undefined);
    const out = { name: target.name, type: target.type };
    if ("width" in target) { out.w = Math.round(target.width); out.h = Math.round(target.height); }
    if ("fills" in target) out.fill = describeFills(target.fills);
    if ("strokes" in target && Array.isArray(target.strokes) && target.strokes.length) {
      out.stroke = describeFills(target.strokes);
      out.strokeWeight = n(target.strokeWeight);
    }
    if ("cornerRadius" in target && n(target.cornerRadius)) out.radius = n(target.cornerRadius);
    if ("effects" in target && Array.isArray(target.effects)) {
      if (target.effects.some((e) => e.type === "DROP_SHADOW" && e.visible !== false)) out.shadow = true;
      if (target.effects.some((e) => e.type === "BACKGROUND_BLUR")) out.backdropBlur = true;
      if (target.effects.some((e) => e.type === "LAYER_BLUR")) out.blur = true;
    }
    if ("opacity" in target && target.opacity < 1) out.opacity = n(target.opacity);
    if ("layoutPositioning" in target && target.layoutPositioning === "ABSOLUTE") {
      out.absolute = { x: Math.round(target.x), y: Math.round(target.y) };
    }
    if ("layoutMode" in target && target.layoutMode !== "NONE") {
      out.layout = {
        dir: target.layoutMode, gap: n(target.itemSpacing),
        pad: [target.paddingTop, target.paddingRight, target.paddingBottom, target.paddingLeft].map(n),
        main: target.primaryAxisAlignItems, cross: target.counterAxisAlignItems,
        wrap: target.layoutWrap === "WRAP" || undefined,
      };
    }
    if ("layoutSizingHorizontal" in target) {
      out.sizing = [target.layoutSizingHorizontal, target.layoutSizingVertical].join("/");
    }
    if (target.type === "TEXT") {
      out.text = target.characters.slice(0, 2000);
      out.fontSize = n(target.fontSize);
      if (typeof target.fontName === "object" && target.fontName.family) {
        out.font = `${target.fontName.family} ${target.fontName.style}`;
      }
      if (typeof target.textAlignHorizontal === "string") out.align = target.textAlignHorizontal;
      if (typeof target.lineHeight === "object" && target.lineHeight.unit !== "AUTO") {
        out.lineHeight = `${n(target.lineHeight.value)}${target.lineHeight.unit === "PERCENT" ? "%" : "px"}`;
      }
      if (typeof target.letterSpacing === "object" && target.letterSpacing.value) {
        out.letterSpacing = `${n(target.letterSpacing.value)}${target.letterSpacing.unit === "PERCENT" ? "%" : "px"}`;
      }
      if (typeof target.textCase === "string" && target.textCase !== "ORIGINAL") out.textCase = target.textCase;
    }
    if (depth > 0 && "children" in target && target.children.length) {
      out.children = target.children.slice(0, 80).map((child) => spec(child, depth - 1));
    }
    return out;
  }

  // What a careful designer checks before handing a page over: text that
  // collapsed or overflows, the wrong font, placeholder copy, photo slots left
  // empty, layers nobody named. Returns issues with ids for a targeted fix.
  function audit(target, opts = {}) {
    const issues = [];
    const add = (n, kind, detail) => {
      if (issues.length < 60) issues.push({ id: n.id, kind, name: n.name, detail });
    };
    const fonts = new Set([theme.headingFont, theme.bodyFont, ...(opts.fonts || [])]);
    const all = (types) => ("findAllWithCriteria" in target
      ? target.findAllWithCriteria({ types })
      : target.findAll((n) => types.includes(n.type)));
    const PLACEHOLDER = /lorem|ipsum|^(title|heading|button|image|text|subtitle)$|feature \d|your (company|text|name)/i;
    for (const t of all(["TEXT"])) {
      if (t.width < 4 && t.characters.length > 2) add(t, "collapsed-text", "text has no width");
      if (PLACEHOLDER.test(t.characters.trim())) add(t, "placeholder", t.characters.slice(0, 40));
      const family = typeof t.fontName === "object" ? t.fontName.family : null;
      if (family && !fonts.has(family)) add(t, "wrong-font", family);
    }
    let unnamed = 0;
    const frames = all(["FRAME"]);
    if (target.type === "FRAME") frames.push(target);
    for (const f of frames) {
      if (/^(Frame|Rectangle|Group)( \d+)?$/.test(f.name)) unnamed += 1;
      if (f.name.startsWith("Image/") && Array.isArray(f.fills) && !f.fills.some((p) => p.type === "IMAGE")) {
        add(f, "image-placeholder", f.name);
      }
      // A frame that hugs grows with its content and cannot overflow; only
      // fixed and filled frames can push content past their edge.
      if (!("children" in f) || !isAuto(f)) continue;
      const right = f.width - (f.paddingRight || 0) + 1;
      const bottom = f.height - (f.paddingBottom || 0) + 1;
      for (const c of f.children) {
        if (c.layoutPositioning === "ABSOLUTE" || c.visible === false) continue;
        if (f.layoutSizingHorizontal !== "HUG" && c.x + c.width > right) {
          add(c, "overflow", `${f.clipsContent ? "cropped by" : "wider than"} ${f.name}`);
        }
        if (f.layoutSizingVertical !== "HUG" && c.y + c.height > bottom) {
          add(c, "overflow-y", `${f.clipsContent ? "cropped by" : "taller than"} ${f.name}`);
        }
      }
    }
    if (unnamed > 3) issues.push({ id: target.id, kind: "unnamed", name: target.name, detail: `${unnamed} layers named "Frame"` });
    return { issues, warnings: warnings.slice(0, 20) };
  }

  return {
    created, warnings, assets, setAssets, icons, setIcons, theme, setTheme, RAMP,
    hex, paint, gradient, shadow, shadows, font, frame, stack, hstack, text, rect, ellipse, glow,
    image, photo, icon, button, badge, card, stat, avatar, divider, section, container, row, col,
    node, focus, summary, outline, spec, audit, toHex, attach, padding, luminance,
  };
})();
