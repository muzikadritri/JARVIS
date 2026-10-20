// JARVIS Live Designer — the plugin's main thread.
//
// JARVIS writes Figma Plugin API JavaScript; this file runs it inside the open
// file and reports exactly what happened. The UI iframe (ui.html) does the
// networking, because the main thread has no fetch of its own — it only ever
// talks to the iframe through postMessage.
//
// Every job is the body of an async function with `figma` in scope, the same
// contract as Figma's own `use_figma` MCP tool, so code written for one runs
// unchanged on the other.

const PLUGIN_VERSION = "1.0";
const MAX_SELECTION = 20;

figma.showUI(__html__, { width: 320, height: 260, themeColors: true, title: "JARVIS Live Designer" });

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

function formatValue(value) {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(serialize(value));
  } catch (err) {
    return String(value);
  }
}

// A node handed back raw would serialise as an empty object, or throw on a
// removed node. The model needs ids and names to refer to what it just made.
// The depth limit only guards against cycles: J.outline and J.spec return
// deliberately deep trees, and cutting them at a few levels loses the design.
const MAX_DEPTH = 40;

function serialize(value, depth = 0) {
  if (value === null || value === undefined) return null;
  if (depth > MAX_DEPTH) return "[…]";
  const kind = typeof value;
  if (kind === "string") return value.length > 4000 ? value.slice(0, 4000) + "…" : value;
  if (kind === "number" || kind === "boolean") return value;
  if (kind === "function" || kind === "symbol") return `[${kind}]`;
  if (Array.isArray(value)) return value.slice(0, 500).map((v) => serialize(v, depth + 1));
  if (kind === "object" && typeof value.id === "string" && typeof value.type === "string") {
    const out = { id: value.id, name: value.name, type: value.type };
    if ("width" in value && "height" in value) {
      out.width = Math.round(value.width);
      out.height = Math.round(value.height);
    }
    return out;
  }
  if (kind === "object") {
    const out = {};
    for (const key of Object.keys(value).slice(0, 200)) {
      if (value[key] !== undefined) out[key] = serialize(value[key], depth + 1);
    }
    return out;
  }
  return String(value);
}

function describeError(err) {
  if (!err) return "unknown error";
  const name = err.name || "Error";
  const message = err.message || String(err);
  const stack = typeof err.stack === "string" ? err.stack.split("\n").slice(0, 4).join(" | ") : "";
  return stack && !stack.includes(message) ? `${name}: ${message} (${stack})` : `${name}: ${message}`;
}

function withTimeout(promise, ms) {
  if (!ms || ms <= 0) return promise;
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`timed out after ${ms} ms`)), ms);
  });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}

function compile(code) {
  try {
    return new AsyncFunction("figma", code);
  } catch (err) {
    // A syntax error is the code's fault and must be reported as such. Any
    // other failure here means the sandbox refused the Function constructor,
    // and indirect eval is the one other way in.
    if (err instanceof SyntaxError) throw err;
    return (0, eval)(`(async function (figma) {\n${code}\n})`);
  }
}

async function execute(job) {
  const logs = [];
  const original = { log: console.log, warn: console.warn, error: console.error };
  for (const level of ["log", "warn", "error"]) {
    console[level] = (...args) => {
      if (logs.length < 200) logs.push(`${level}: ${args.map(formatValue).join(" ")}`.slice(0, 500));
      original[level].apply(console, args);
    };
  }
  const started = Date.now();
  if (job.description) figma.notify(`JARVIS: ${job.description}`.slice(0, 120), { timeout: 2500 });
  try {
    const fn = compile(String(job.code || ""));
    const value = await withTimeout(fn(figma), Number(job.timeoutMs) || 60000);
    return { id: job.id, ok: true, result: serialize(value), logs, ms: Date.now() - started };
  } catch (err) {
    return { id: job.id, ok: false, error: describeError(err), logs, ms: Date.now() - started };
  } finally {
    console.log = original.log;
    console.warn = original.warn;
    console.error = original.error;
  }
}

// Reading a property the plugin has no permission for throws instead of
// returning null, and one throw here would stop JARVIS ever seeing the file.
function safely(read, fallback = null) {
  try {
    const value = read();
    return value === undefined ? fallback : value;
  } catch (err) {
    return fallback;
  }
}

function currentContext() {
  const selection = figma.currentPage.selection.slice(0, MAX_SELECTION).map((node) => ({
    id: node.id,
    name: node.name,
    type: node.type,
    width: "width" in node ? Math.round(node.width) : null,
    height: "height" in node ? Math.round(node.height) : null,
  }));
  return {
    fileName: safely(() => figma.root.name),
    fileKey: safely(() => figma.fileKey),
    page: figma.currentPage.name,
    pageId: figma.currentPage.id,
    selection,
    user: safely(() => (figma.currentUser ? figma.currentUser.name : null)),
    pluginVersion: PLUGIN_VERSION,
  };
}

function sendContext() {
  try {
    figma.ui.postMessage({ type: "context", context: currentContext() });
  } catch (err) {
    console.error("JARVIS context:", err);
  }
}

figma.on("selectionchange", sendContext);
figma.on("currentpagechange", sendContext);

figma.ui.onmessage = async (msg) => {
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "run" && msg.job) {
    const result = await execute(msg.job);
    figma.ui.postMessage({ type: "result", result });
    sendContext();
  } else if (msg.type === "context-request") {
    sendContext();
  } else if (msg.type === "close") {
    figma.closePlugin();
  }
};

sendContext();
