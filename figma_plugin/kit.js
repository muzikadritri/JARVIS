// JARVIS design kit — prepended to every job JARVIS sends to Figma.
//
// Generated code calls these instead of the raw Plugin API for the things a
// model reliably gets wrong: loading a font before touching text, setting
// FILL/HUG sizing only after a node has an auto-layout parent, colours in
// 0..1 rather than 0..255, and font styles that differ by family ("Semi Bold"
// in Inter, "SemiBold" in most Google fonts). Everything lives under ONE name,
// `J`, so a job can declare whatever it likes without colliding with the kit.
const J = (() => {
  // Every node this job creates through the kit, so a job that fails halfway
  // can be undone exactly — code.js reports this list with the error.
  const created = (globalThis.__jarvisCreated = []);
  const track = (node) => {
    created.push(node.id);
    return node;
  };

  const theme = {
    bg: "#0B0F17", surface: "#121826", surface2: "#1A2233", text: "#F5F7FB",
    muted: "#9AA4B2", primary: "#4F8CFF", accent: "#22D3EE", onPrimary: "#FFFFFF",
    border: "#263045", radius: 16, headingFont: "Inter", bodyFont: "Inter",
    width: 1440, pagePad: 120,
  };

  const STYLE_ALIASES = {
    "Semi Bold": ["Semi Bold", "SemiBold", "Semibold", "Bold"],
    "SemiBold": ["SemiBold", "Semi Bold", "Semibold", "Bold"],
    "Extra Bold": ["Extra Bold", "ExtraBold", "Black", "Bold"],
    "ExtraBold": ["ExtraBold", "Extra Bold", "Black", "Bold"],
    "Medium": ["Medium", "Regular"],
    "Light": ["Light", "Regular"],
    "Black": ["Black", "Extra Bold", "ExtraBold", "Bold"],
    "Bold": ["Bold", "Semi Bold", "SemiBold"],
    "Regular": ["Regular", "Book", "Normal"],
  };
  const WEIGHT_NAMES = {
    300: "Light", 400: "Regular", 500: "Medium", 600: "Semi Bold",
    700: "Bold", 800: "Extra Bold", 900: "Black",
  };
  const fontCache = new Map();

  // Binary assets JARVIS fetched for this job (photos, as base64), by key.
  const assets = {};
  function setAssets(map) {
    if (map && typeof map === "object") Object.assign(assets, map);
    return assets;
  }

  function setTheme(overrides) {
    if (overrides && typeof overrides === "object") Object.assign(theme, overrides);
    return theme;
  }

  // SolidPaint.color is {r, g, b} ONLY — an `a` key makes Figma reject the
  // paint, so an RGBA object is reduced here and its alpha goes to opacity.
  function hex(color) {
    if (color && typeof color === "object" && "r" in color) return { r: color.r, g: color.g, b: color.b };
    let value = String(color || "#000000").trim().replace(/^#/, "");
    if (value.length === 3 || value.length === 4) value = [...value].map((c) => c + c).join("");
    if (!/^[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(value)) value = "000000";
    const num = parseInt(value.slice(0, 6), 16);
    return { r: ((num >> 16) & 255) / 255, g: ((num >> 8) & 255) / 255, b: (num & 255) / 255 };
  }

  function alphaOf(color) {
    if (color && typeof color === "object") return typeof color.a === "number" ? color.a : 1;
    const value = String(color || "").trim().replace(/^#/, "");
    if (value.length === 8) return parseInt(value.slice(6, 8), 16) / 255;
    if (value.length === 4) return parseInt(value[3] + value[3], 16) / 255;
    return 1;
  }

  function paint(color, opacity) {
    const resolved = theme[color] || color;
    return { type: "SOLID", color: hex(resolved), opacity: opacity ?? alphaOf(resolved) };
  }

  // Linear gradient at `angle` degrees (0 = left→right, 90 = top→bottom).
  function gradient(stops, angle = 135) {
    const rad = (angle * Math.PI) / 180;
    const cos = Math.cos(rad);
    const sin = Math.sin(rad);
    const transform = [
      [cos, sin, 0.5 - 0.5 * cos - 0.5 * sin],
      [-sin, cos, 0.5 + 0.5 * sin - 0.5 * cos],
    ];
    const list = (stops && stops.length ? stops : [theme.primary, theme.accent]).map((stop, i, all) => {
      const color = typeof stop === "string" ? stop : stop.color;
      const position = typeof stop === "object" && stop.position !== undefined ? stop.position : i / Math.max(1, all.length - 1);
      const resolved = theme[color] || color;
      return { position, color: { ...hex(resolved), a: alphaOf(resolved) } };
    });
    return { type: "GRADIENT_LINEAR", gradientTransform: transform, gradientStops: list };
  }

  function shadow(level = 1, color = "#000000") {
    const blur = [0, 12, 24, 48][Math.max(0, Math.min(3, level))];
    const y = [0, 4, 12, 24][Math.max(0, Math.min(3, level))];
    return {
      type: "DROP_SHADOW", color: { ...hex(color), a: 0.18 + level * 0.04 },
      offset: { x: 0, y }, radius: blur, spread: 0, visible: true, blendMode: "NORMAL",
    };
  }

  async function font(family, style = "Regular") {
    const wanted = typeof style === "number" ? WEIGHT_NAMES[style] || "Regular" : style;
    const key = `${family}::${wanted}`;
    if (fontCache.has(key)) return fontCache.get(key);
    const families = [family || theme.bodyFont, "Inter"];
    const styles = STYLE_ALIASES[wanted] || [wanted, "Regular"];
    for (const fam of families) {
      for (const sty of styles) {
        try {
          const name = { family: fam, style: sty };
          await figma.loadFontAsync(name);
          fontCache.set(key, name);
          return name;
        } catch (err) {
          // not available under this exact name — try the next alias
        }
      }
    }
    const fallback = { family: "Inter", style: "Regular" };
    await figma.loadFontAsync(fallback);
    fontCache.set(key, fallback);
    return fallback;
  }

  function padding(node, pad) {
    if (pad === undefined || pad === null) return;
    const [t, r, b, l] = Array.isArray(pad)
      ? (pad.length === 2 ? [pad[0], pad[1], pad[0], pad[1]] : pad.length === 4 ? pad : [pad[0], pad[0], pad[0], pad[0]])
      : [pad, pad, pad, pad];
    node.paddingTop = t; node.paddingRight = r; node.paddingBottom = b; node.paddingLeft = l;
  }

  const ALIGN = { start: "MIN", center: "CENTER", end: "MAX", between: "SPACE_BETWEEN", baseline: "BASELINE" };

  // Models hand over "120px", "100%", "auto" — even objects — where Figma
  // wants a number, and Figma then throws deep inside resize() ("Expected
  // number, received object"). Every numeric option passes through here: a
  // usable number, or absent so the default applies. "100%" for a width
  // means what it says, so it becomes fillWidth.
  const NUMERIC = ["width", "height", "w", "h", "gap", "rowGap", "radius", "strokeWeight",
    "opacity", "size", "lineHeight", "letterSpacing", "box", "x", "y", "angle"];
  function num(value) {
    if (typeof value === "number") return Number.isFinite(value) ? value : undefined;
    if (typeof value === "string") {
      const m = value.trim().match(/^-?\d+(?:\.\d+)?(?=(?:px)?$)/);
      return m ? parseFloat(m[0]) : undefined;
    }
    return undefined;
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
    for (const key of ["width", "height", "w", "h", "box"]) {
      if (key in out && out[key] < 1) delete out[key];
    }
    if ("pad" in out) {
      out.pad = Array.isArray(out.pad) ? out.pad.map((v) => num(v) ?? 0) : num(out.pad);
      if (out.pad === undefined) delete out.pad;
    }
    return out;
  }

  // Sizing can only be applied once the node sits in an auto-layout parent;
  // setting FILL earlier throws. `attach` appends and then applies it.
  function attach(node, parent, opts = {}) {
    const target = parent || null;
    if (target && "appendChild" in target) target.appendChild(node);
    const inAutoLayout = target && "layoutMode" in target && target.layoutMode !== "NONE";
    if (inAutoLayout) {
      if (opts.fillWidth) node.layoutSizingHorizontal = "FILL";
      if (opts.fillHeight) node.layoutSizingVertical = "FILL";
      if (opts.grow) node.layoutGrow = 1;
      if (opts.absolute) node.layoutPositioning = "ABSOLUTE";
    }
    if (opts.x !== undefined) node.x = opts.x;
    if (opts.y !== undefined) node.y = opts.y;
    return node;
  }

  function frame(opts = {}) {
    opts = clean(opts);
    const node = track(figma.createFrame());
    node.name = opts.name || "Frame";
    const dir = opts.dir === undefined ? "VERTICAL" : opts.dir;
    const wraps = Boolean(opts.wrap) && dir === "HORIZONTAL";
    // A wrapping row only wraps when something other than its content decides
    // its width; hugging, it just grows wider. So it fills its parent unless
    // it was given a width.
    const fillWidth = opts.fillWidth ?? (wraps && !opts.width ? true : undefined);
    node.fills = opts.fill === undefined || opts.fill === null ? [] : [].concat(opts.fill).map((f) => (typeof f === "string" ? paint(f) : f));
    // resize() FIRST: on an auto-layout frame it resets both sizing modes to
    // FIXED, so hugging set before it would silently be lost — the page root
    // would stay 100 px tall with every section spilling out below it.
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
    if (opts.stroke) {
      node.strokes = [paint(opts.stroke)];
      node.strokeWeight = opts.strokeWeight || 1;
      node.strokeAlign = "INSIDE";
    }
    if (opts.shadow) node.effects = [shadow(opts.shadow === true ? 1 : opts.shadow)];
    node.clipsContent = Boolean(opts.clip);
    if (opts.opacity !== undefined) node.opacity = opts.opacity;
    return attach(node, opts.parent, { ...opts, fillWidth });
  }

  async function text(content, opts = {}) {
    opts = clean(opts);
    const node = track(figma.createText());
    const family = opts.family || (opts.heading ? theme.headingFont : theme.bodyFont);
    node.fontName = await font(family, opts.weight || (opts.heading ? "Bold" : "Regular"));
    node.characters = String(content ?? "");
    node.fontSize = opts.size || 16;
    node.fills = [paint(opts.color || "text")];
    node.name = opts.name || node.characters.slice(0, 40) || "Text";
    // lineHeight up to 4 is a multiplier (1.5 → 150 %), anything larger is pixels.
    if (opts.lineHeight) {
      node.lineHeight = opts.lineHeight <= 4
        ? { value: opts.lineHeight * 100, unit: "PERCENT" }
        : { value: opts.lineHeight, unit: "PIXELS" };
    }
    if (opts.letterSpacing !== undefined) node.letterSpacing = { value: opts.letterSpacing, unit: "PERCENT" };
    if (opts.upper) node.textCase = "UPPER";
    if (opts.align) node.textAlignHorizontal = { left: "LEFT", center: "CENTER", right: "RIGHT", justify: "JUSTIFIED" }[opts.align] || "LEFT";
    if (opts.width) {
      // resize() fixes both dimensions; HEIGHT afterwards lets it grow with the text.
      node.resize(opts.width, Math.max(1, node.height));
      node.textAutoResize = "HEIGHT";
    }
    attach(node, opts.parent, opts);
    if (opts.fillWidth) node.textAutoResize = "HEIGHT";
    return node;
  }

  function rect(opts = {}) {
    opts = clean(opts);
    const node = track(figma.createRectangle());
    node.name = opts.name || "Rectangle";
    node.resize(opts.w || opts.width || 100, opts.h || opts.height || 100);
    node.fills = [].concat(opts.fill === undefined ? "surface2" : opts.fill).map((f) => (typeof f === "string" ? paint(f) : f));
    if (opts.radius !== undefined) node.cornerRadius = opts.radius === true ? theme.radius : opts.radius;
    return attach(node, opts.parent, opts);
  }

  function ellipse(opts = {}) {
    opts = clean(opts);
    const node = track(figma.createEllipse());
    node.name = opts.name || "Ellipse";
    node.resize(opts.w || opts.size || 48, opts.h || opts.size || 48);
    node.fills = [].concat(opts.fill === undefined ? "primary" : opts.fill).map((f) => (typeof f === "string" ? paint(f) : f));
    return attach(node, opts.parent, opts);
  }

  async function button(label, opts = {}) {
    opts = clean(opts);
    const variant = opts.variant || "primary";
    const styles = {
      primary: { fill: "primary", color: "onPrimary", stroke: null },
      secondary: { fill: "surface2", color: "text", stroke: "border" },
      ghost: { fill: null, color: "text", stroke: "border" },
      light: { fill: "#FFFFFF", color: "#0B0F17", stroke: null },
    }[variant] || { fill: "primary", color: "onPrimary", stroke: null };
    const size = opts.size === "lg" ? [18, 32, 17] : opts.size === "sm" ? [10, 18, 14] : [14, 24, 15];
    const node = frame({
      name: opts.name || `Button / ${label}`, dir: "HORIZONTAL", gap: 8,
      pad: [size[0], size[1]], align: "center", justify: "center",
      fill: styles.fill, radius: opts.radius ?? 999, stroke: styles.stroke,
      parent: opts.parent, fillWidth: opts.fillWidth,
    });
    await text(label, { parent: node, size: size[2], weight: "Semi Bold", color: styles.color });
    return node;
  }

  // An image slot. createImageAsync is not available to every runner, so a
  // tasteful gradient with a caption stands in where a photo would go.
  async function image(opts = {}) {
    opts = clean(opts);
    const node = frame({
      name: opts.name || "Image", dir: "VERTICAL", align: "center", justify: "center",
      width: opts.w || opts.width || 560, height: opts.h || opts.height || 360,
      fill: gradient(opts.colors || [theme.primary, theme.accent], opts.angle ?? 135),
      radius: opts.radius ?? theme.radius, clip: true, parent: opts.parent,
      fillWidth: opts.fillWidth,
    });
    if (opts.label !== false) {
      await text(opts.label || "Image", { parent: node, size: 14, weight: "Medium", color: "#FFFFFFCC" });
    }
    return node;
  }

  async function icon(glyph, opts = {}) {
    opts = clean(opts);
    const box = opts.box || 44;
    const node = frame({
      name: opts.name || "Icon", dir: "VERTICAL", align: "center", justify: "center",
      width: box, height: box, fill: opts.fill || "surface2", radius: opts.radius ?? 12,
      parent: opts.parent,
    });
    await text(glyph || "★", { parent: node, size: opts.size || 20, weight: "Bold", color: opts.color || "primary" });
    return node;
  }

  async function node(id) {
    const found = await figma.getNodeByIdAsync(String(id));
    if (!found) throw new Error(`node ${id} does not exist (deleted, or on another page)`);
    return found;
  }

  // A full-width page section, appended to the page root frame.
  function section(name, opts = {}) {
    opts = clean(opts);
    return frame({
      name, dir: opts.dir || "VERTICAL", gap: opts.gap ?? 32,
      pad: opts.pad ?? [96, theme.pagePad], align: opts.align || "start",
      justify: opts.justify, fill: opts.fill === undefined ? "bg" : opts.fill,
      parent: opts.parent, fillWidth: true,
    });
  }

  function focus(nodes) {
    const list = [].concat(nodes).filter(Boolean);
    if (!list.length) return;
    figma.viewport.scrollAndZoomIntoView(list);
    figma.currentPage.selection = list.filter((n) => n.parent && n.parent.type !== "DOCUMENT");
  }

  function summary(target, extra = {}) {
    return { id: target.id, name: target.name, type: target.type,
             width: Math.round(target.width), height: Math.round(target.height), ...extra };
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
    const num = (v) => (typeof v === "number" ? Math.round(v * 100) / 100 : undefined);
    const out = { name: target.name, type: target.type };
    if ("width" in target) { out.w = Math.round(target.width); out.h = Math.round(target.height); }
    if ("fills" in target) out.fill = describeFills(target.fills);
    if ("strokes" in target && Array.isArray(target.strokes) && target.strokes.length) {
      out.stroke = describeFills(target.strokes);
      out.strokeWeight = num(target.strokeWeight);
    }
    if ("cornerRadius" in target && num(target.cornerRadius)) out.radius = num(target.cornerRadius);
    if ("effects" in target && target.effects.some((e) => e.type === "DROP_SHADOW" && e.visible !== false)) out.shadow = true;
    if ("opacity" in target && target.opacity < 1) out.opacity = num(target.opacity);
    if ("layoutMode" in target && target.layoutMode !== "NONE") {
      out.layout = {
        dir: target.layoutMode, gap: num(target.itemSpacing),
        pad: [target.paddingTop, target.paddingRight, target.paddingBottom, target.paddingLeft].map(num),
        main: target.primaryAxisAlignItems, cross: target.counterAxisAlignItems,
        wrap: target.layoutWrap === "WRAP" || undefined,
      };
    }
    if ("layoutSizingHorizontal" in target) {
      out.sizing = [target.layoutSizingHorizontal, target.layoutSizingVertical].join("/");
    }
    if (target.type === "TEXT") {
      out.text = target.characters.slice(0, 2000);
      out.fontSize = num(target.fontSize);
      if (typeof target.fontName === "object" && target.fontName.family) {
        out.font = `${target.fontName.family} ${target.fontName.style}`;
      }
      if (typeof target.textAlignHorizontal === "string") out.align = target.textAlignHorizontal;
      if (typeof target.lineHeight === "object" && target.lineHeight.unit !== "AUTO") {
        out.lineHeight = `${num(target.lineHeight.value)}${target.lineHeight.unit === "PERCENT" ? "%" : "px"}`;
      }
      if (typeof target.letterSpacing === "object" && target.letterSpacing.value) {
        out.letterSpacing = `${num(target.letterSpacing.value)}${target.letterSpacing.unit === "PERCENT" ? "%" : "px"}`;
      }
      if (typeof target.textCase === "string" && target.textCase !== "ORIGINAL") out.textCase = target.textCase;
    }
    if (depth > 0 && "children" in target && target.children.length) {
      out.children = target.children.slice(0, 80).map((child) => spec(child, depth - 1));
    }
    return out;
  }

  return {
    created, assets, setAssets, theme, setTheme, hex, paint, gradient, shadow, font, frame, text, rect, ellipse,
    button, image, icon, node, section, focus, summary, outline, spec, toHex, attach, padding,
  };
})();
