"""
Self-test for the Schleimy-edition skills.

    python tests/test_schleimy_skills.py

Covers core/mcp_client, core/figma_bridge, the Figma plugin + design kit,
actions/figma_designer end to end, core/web_builder, actions/js_studio and
the utility tools. It needs no network, no API key and no Figma: models are
replaced by scripted fakes, MCP servers by a local mock, and Figma itself by
tests/figma_mock_runner.mjs — which loads the REAL plugin main thread
(figma_plugin/code.js) against a mock API that enforces the Plugin API rules.

Node.js is needed for the Figma and JavaScript parts; without it those
sections are reported as skipped, never as passed.

Same shape as providers/_tests.py: plain checks, a count, exit status 1 on
any failure.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = Path(tempfile.mkdtemp(prefix="jarvis_skills_"))
# Every module below computes its storage paths from the home directory at
# import time, so the home directory is moved into the sandbox first.
os.environ["HOME"] = str(SANDBOX)
os.environ["USERPROFILE"] = str(SANDBOX)
os.environ["JARVIS_NO_FIGMA_BRIDGE"] = "1"
for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(var, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
sys.path.insert(0, str(ROOT))

_ok = _fail = _skip = 0
_failures: list[str] = []
HAS_NODE = shutil.which("node") is not None


def check(name: str, cond, detail: str = "") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        _failures.append(name)
        print(f"  FAIL  {name}   {str(detail)[:300]}")


def skip(name: str, why: str) -> None:
    global _skip
    _skip += 1
    print(f"  SKIP  {name}   ({why})")


def section(title: str) -> None:
    print(f"\n{title}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakePlayer:
    def __init__(self):
        self.logs, self.said, self.shown, self.reviews = [], [], [], []

    def write_log(self, text):
        self.logs.append(text)

    def request_say(self, text):
        self.said.append(text)

    def show_content(self, title, text):
        self.shown.append((title, text))

    def show_review(self, title, summary, findings, unclear=None):
        self.reviews.append((title, summary, findings))


class FakeModel:
    """Scripted replacement for core.gemini.text / as_json. Each rule is
    (substring of the prompt, reply or list of replies used in turn); the most
    recently added matching rule wins, so a later step can be more specific."""

    def __init__(self):
        self.rules: list[tuple[str, list]] = []
        self.prompts: list[str] = []

    def on(self, needle: str, *replies):
        self.rules.append((needle, list(replies)))

    def _prompt_text(self, contents) -> str:
        if isinstance(contents, str):
            return contents
        return "\n".join(c for c in contents if isinstance(c, str))

    def text(self, contents, tier=None, config=None, timeout_ms=0, key="", default=""):
        prompt = self._prompt_text(contents)
        self.prompts.append(prompt)
        for needle, replies in reversed(self.rules):
            if needle in prompt:
                return replies.pop(0) if len(replies) > 1 else replies[0]
        return default

    def as_json(self, contents, tier=None, config=None, timeout_ms=0, key="", default=None):
        raw = self.text(contents)
        if not raw:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default


def install_model(model: FakeModel):
    from core import gemini
    gemini.text = model.text
    gemini.as_json = model.as_json


# ─────────────────────────────────────────────────────────────────────────────
#  A mock MCP server: JSON and SSE answers, sessions, a server→client ping
# ─────────────────────────────────────────────────────────────────────────────
class MockMCP:
    def __init__(self, token: str = ""):
        self.token = token
        self.sessions: set[str] = set()
        self.issued = 0
        self.calls: list[dict] = []
        self.expire_next = False
        self.pinged = threading.Event()
        owner = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                return

            def _reply(self, status, body=None, headers=None, sse=False):
                data = b""
                if body is not None:
                    payload = json.dumps(body)
                    data = (f"event: message\ndata: {payload}\n\n".encode() if sse
                            else payload.encode())
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def do_DELETE(self):
                owner.sessions.discard(self.headers.get("Mcp-Session-Id"))
                self._reply(200, {})

            def do_POST(self):
                if owner.token and self.headers.get("Authorization") != f"Bearer {owner.token}":
                    self._reply(401, {"error": "no"}, {"WWW-Authenticate": 'Bearer realm="mock"'})
                    return
                msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "method" not in msg:                        # our answer to the ping
                    owner.pinged.set()
                    self._reply(202)
                    return
                method = msg["method"]
                if method == "notifications/initialized":
                    self._reply(202)
                    return
                if method == "initialize":
                    owner.issued += 1
                    sid = f"s{owner.issued}"
                    owner.sessions.add(sid)
                    self._reply(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {
                        "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                        "serverInfo": {"name": "mock-figma", "version": "9"},
                        "instructions": "be nice"}}, {"Mcp-Session-Id": sid})
                    return
                sid = self.headers.get("Mcp-Session-Id")
                if owner.expire_next or sid not in owner.sessions:
                    owner.expire_next = False
                    owner.sessions.discard(sid)
                    self._reply(404, {"error": "session gone"})
                    return
                owner.calls.append(msg)
                if method == "tools/list":
                    cursor = (msg.get("params") or {}).get("cursor")
                    if not cursor:
                        self._reply(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {
                            "tools": [{"name": "get_screenshot", "description": "shot",
                                       "inputSchema": {"properties": {"nodeId": {}}}}],
                            "nextCursor": "p2"}})
                    else:
                        self._reply(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {
                            "tools": [{"name": "use_figma", "description": "write",
                                       "inputSchema": {"properties": {"code": {}, "fileKey": {}}}}]}})
                    return
                if method == "tools/call":
                    name = msg["params"]["name"]
                    if name == "use_figma":
                        # SSE answer with a server→client ping in front of it.
                        ping = json.dumps({"jsonrpc": "2.0", "id": "srv-1", "method": "ping"})
                        result = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                            "content": [{"type": "text", "text": '{"id": "9:9", "name": "Page"}'}]}})
                        data = f": keepalive\n\ndata: {ping}\n\ndata: {result}\n\n".encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    if name == "fail":
                        self._reply(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {
                            "content": [{"type": "text", "text": "nope"}], "isError": True}})
                        return
                    if name == "get_screenshot":
                        self._reply(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {
                            "content": [{"type": "image", "mimeType": "image/png",
                                         "data": "iVBORw0KGgo="}]}}, sse=True)
                        return
                    self._reply(200, {"jsonrpc": "2.0", "id": msg["id"],
                                      "error": {"code": -32602, "message": f"unknown tool {name}"}})
                    return
                self._reply(200, {"jsonrpc": "2.0", "id": msg["id"],
                                  "error": {"code": -32601, "message": "no such method"}})

        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://localhost:{self.port}/mcp"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def test_mcp_client():
    section("core/mcp_client — Streamable HTTP")
    from core import mcp_client
    mock = MockMCP()
    try:
        c = mcp_client.MCPClient(mock.url, name="mock")
        check("localhost is normalised to 127.0.0.1", "127.0.0.1" in c.url, c.url)
        tools = c.list_tools()
        check("initialize happens implicitly and keeps the session id", c.session_id == "s1", c.session_id)
        check("tools/list follows nextCursor across pages",
              [t["name"] for t in tools] == ["get_screenshot", "use_figma"], tools)
        res = c.call_tool("use_figma", {"code": "1"})
        check("an SSE answer is read and matched by id", '"9:9"' in res.text and not res.is_error, res.text)
        check("a server→client ping on the stream is answered", mock.pinged.wait(3))
        shot = c.call_tool("get_screenshot", {"nodeId": "1:2"})
        check("inline images are exposed", shot.images() == [("image/png", "iVBORw0KGgo=")], shot.content)
        bad = c.call_tool("fail")
        check("isError results are reported as errors", bad.is_error and bad.text == "nope")
        try:
            c.call_tool("missing")
            check("a JSON-RPC error raises MCPError", False)
        except mcp_client.MCPError as e:
            check("a JSON-RPC error raises MCPError", "unknown tool" in str(e), e)
        mock.expire_next = True
        again = c.list_tools()
        check("an expired session (404) re-initialises once and retries",
              len(again) == 2 and c.session_id == "s2", c.session_id)
        c.close()
        check("close ends the session", c.session_id is None and "s2" not in mock.sessions)
    finally:
        mock.stop()

    secured = MockMCP(token="sekret")
    try:
        try:
            mcp_client.MCPClient(secured.url, name="sec").list_tools()
            check("401 raises MCPAuthError", False)
        except mcp_client.MCPAuthError as e:
            check("401 raises MCPAuthError naming the challenge", "Bearer" in str(e), e)
        ok = mcp_client.MCPClient(secured.url, token="sekret", name="sec").list_tools()
        check("a bearer token is sent", len(ok) == 2)
    finally:
        secured.stop()

    dead = mcp_client.MCPClient(f"http://127.0.0.1:{free_port()}/mcp", name="dead", timeout=3)
    try:
        dead.list_tools()
        check("an unreachable server raises MCPUnavailable", False)
    except mcp_client.MCPUnavailable:
        check("an unreachable server raises MCPUnavailable", True)

    mock = MockMCP()
    try:
        mcp_client.save_server("mock", mock.url, description="test")
        servers = mcp_client.load_servers()
        check("save_server persists next to the shipped figma default",
              "mock" in servers and "figma" in servers)
        ok, info = mcp_client.probe("mock")
        check("probe reports the server", ok and "mock-figma" in info, info)
        check("remove_server deletes a user server", mcp_client.remove_server("mock")
              and "mock" not in mcp_client.load_servers())
        mcp_client.remove_server("figma")
        check("removing a shipped default disables it instead",
              mcp_client.load_servers()["figma"]["enabled"] is False)
        mcp_client.save_server("figma", "http://127.0.0.1:3845/mcp")
    finally:
        mock.stop()


# ─────────────────────────────────────────────────────────────────────────────
#  The bridge and a simulated plugin UI that runs jobs in the Node mock
# ─────────────────────────────────────────────────────────────────────────────
class PluginSimulator:
    """Plays ui.html: polls the bridge, hands jobs to the real code.js running
    in tests/figma_mock_runner.mjs, posts results and context back."""

    def __init__(self, port: int):
        self.base = f"http://localhost:{port}"
        self.proc = subprocess.Popen(["node", str(ROOT / "tests" / "figma_mock_runner.mjs")],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _post(self, path, payload):
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(),
                                     method="POST", headers={"Content-Type": "text/plain"})
        urllib.request.urlopen(req, timeout=10).close()

    def _exchange(self, msg) -> dict:
        with self.lock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    raise RuntimeError("mock Figma exited: " + self.proc.stderr.read()[-2000:])
                reply = json.loads(line)
                if reply["type"] == "context":
                    self._post("/context", reply["context"])
                    continue
                return reply

    def dump(self) -> dict:
        return self._exchange({"type": "dump"})["page"]

    def _loop(self):
        # code.js posts its context the moment it loads; ui.html forwards it
        # straight away, so the simulator drains it before the first poll.
        try:
            self.dump()
        except Exception as e:
            print(f"    (plugin simulator start: {e})")
        while not self.stop_flag.is_set():
            try:
                resp = urllib.request.urlopen(self.base + "/next?wait=1", timeout=10)
                if resp.status == 204:
                    resp.close()
                    continue
                job = json.loads(resp.read())
                resp.close()
                reply = self._exchange({"type": "run", "job": job})
                self._post("/result", reply["result"])
            except Exception as e:
                if not self.stop_flag.is_set():
                    print(f"    (plugin simulator: {e})")
                    time.sleep(0.2)

    def stop(self):
        self.stop_flag.set()
        self.proc.terminate()


def test_manifest():
    section("figma_plugin/manifest.json — what Figma accepts on import")
    import ipaddress
    import re
    from urllib.parse import urlsplit
    manifest = json.loads((ROOT / "figma_plugin" / "manifest.json").read_text(encoding="utf-8"))
    net = manifest.get("networkAccess") or {}
    dev = net.get("devAllowedDomains") or []
    check("devAllowedDomains is not empty", bool(dev), net)
    for entry in dev + [d for d in net.get("allowedDomains") or [] if d != "none"]:
        host = urlsplit(entry if "://" in entry else "http://" + entry).hostname or ""
        try:
            ipaddress.ip_address(host)
            is_ip = True
        except ValueError:
            is_ip = False
        # Figma rejects IP literals ("must be a valid URL") — the bug that
        # stopped the plugin importing at all.
        check(f"{entry} uses a host name, not an IP address", not is_ip and bool(host), host)
    html = (ROOT / "figma_plugin" / "ui.html").read_text(encoding="utf-8")
    bases = re.findall(r'"(https?://[^"]+)"', re.search(r"const BASES = \[(.*?)\]", html).group(1))
    allowed = {e.rstrip("/") for e in dev}
    check("every address ui.html fetches is listed in devAllowedDomains",
          bases and all(b.rstrip("/") in allowed for b in bases), (bases, sorted(allowed)))
    for key in ("name", "id", "api", "main", "ui", "editorType"):
        check(f"manifest has '{key}'", key in manifest)
    for key in ("main", "ui"):
        check(f"manifest '{key}' file exists", (ROOT / "figma_plugin" / manifest.get(key, "?")).is_file())


def test_bridge():
    section("core/figma_bridge — guards and round trip")
    from core.figma_bridge import BridgeError, FigmaBridge
    port = free_port()
    bridge = FigmaBridge(port)
    check("the bridge starts on loopback", bridge.start())
    try:
        def status(origin=None, host=None):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/status")
            if origin:
                req.add_header("Origin", origin)
            if host:
                req.add_header("Host", host)
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        check("the plugin's null origin is accepted", status("null") == 200)
        check("a web page's origin is refused", status("https://evil.example") == 403)
        check("a rebinding Host header is refused", status(host=f"evil.example:{port}") == 421)
        if bridge._server_v6 is not None:
            try:
                with urllib.request.urlopen(f"http://[::1]:{port}/status", timeout=5) as r:
                    v6_ok = r.status == 200
            except Exception:
                v6_ok = False
            check("the bridge also answers on IPv6 loopback (localhost may resolve to ::1)", v6_ok)
        else:
            skip("IPv6 loopback", "this machine has no IPv6 loopback")
        with urllib.request.urlopen(f"http://localhost:{port}/status", timeout=5) as r:
            check("the bridge answers at http://localhost — the only address Figma allows", r.status == 200)
        try:
            bridge._last_seen = 0
            bridge.run("return 1", timeout=1)
            check("run without a plugin raises BridgeError", False)
        except BridgeError:
            check("run without a plugin raises BridgeError", True)
    finally:
        bridge.stop()
    check("stop frees the port", not bridge.running)


KIT_SECTION = """
const root = await J.node("{root}");
const sec = J.section("{name}", {{ parent: root, fill: "bg" }});
J.glow({{ parent: sec, color: "primary" }});
const grid = J.row({{ parent: sec, gap: 48 }});
const copy = J.col(grid, 7, {{ gap: 24, name: "Copy" }});
await J.badge("Fresh roast · Berlin", {{ parent: copy, icon: "leaf" }});
await J.text("{headline}", {{ parent: copy, role: "display", name: "Headline" }});
await J.text("Small-batch coffee, roasted to order and delivered in 48 hours.", {{ parent: copy, role: "lead", color: "muted" }});
const actions = J.hstack({{ parent: copy, gap: 12, name: "Actions" }});
await J.button("Shop beans", {{ parent: actions, size: "lg", icon: true }});
await J.button("Our story", {{ parent: actions, variant: "ghost" }});
const media = J.col(grid, 5, {{ name: "Media" }});
await J.image({{ parent: media, asset: "hero", fillWidth: true, height: 480, overlay: true }});
const stats = J.row({{ parent: sec, cols: 3, gap: 24, name: "Stats" }});
for (const [value, label] of [["48h", "roast to door"], ["4.9", "12,400 reviews"], ["14", "origins"]]) {{
  const card = J.card({{ parent: J.col(stats, 1), variant: "outline", fillWidth: true }});
  await J.icon("coffee", {{ parent: card }});
  await J.stat(value, label, {{ parent: card }});
}}
J.focus(sec);
return J.summary(sec);
"""


def test_figma_designer():
    section("actions/figma_designer — live design end to end (mock Figma)")
    if not HAS_NODE:
        skip("figma designer end to end", "Node.js is not installed")
        return
    from core import web_builder
    from core.figma_bridge import FigmaBridge
    import actions.figma_designer as fd

    port = free_port()
    bridge = FigmaBridge(port)
    bridge.start()
    fd._bridge = lambda: bridge
    fd.DESIGNS_DIR = SANDBOX / "designs"
    fd.STATE_PATH = SANDBOX / "state" / "figma.json"
    sim = PluginSimulator(port)
    try:
        check("the simulated plugin connects", bridge.wait_for_plugin(10))
        check("the plugin reports its file context", bridge.context().get("fileName") == "Test File",
              bridge.context())
        watch_player = FakePlayer()
        fd._WATCH_SECONDS = 0.1
        threading.Thread(target=fd._watch_plugin, args=(watch_player,), daemon=True).start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not watch_player.logs:
            time.sleep(0.05)
        check("the activity log says when the Figma plugin connects, naming the file",
              any("Live Designer connected" in l and "Test File" in l for l in watch_player.logs),
              watch_player.logs)

        model = FakeModel()
        model.on("planning a website design", json.dumps({
            "title": "Ember Roasters",
            "concept": "Warm analog dawn light on near-black, editorial and slow.",
            "signature": "oversized display headline beside a large photo",
            "voice": "Calm, specific, a little dry.",
            "theme": {"bg": "#120D0A", "surface": "#1E1612", "surface2": "#2A1F19",
                      "text": "#FBF5EF", "muted": "#C9B8A8", "primary": "#E07A3F",
                      "accent": "#F2C14E", "onPrimary": "#120D0A", "border": "#3A2C24",
                      "radius": 20, "headingFont": "Poppins", "bodyFont": "Comic Sans",
                      "headingWeight": "Semi Bold", "photoGrade": "warm"},
            "images": [{"key": "hero", "query": "coffee roastery dawn", "orientation": "landscape"}],
            "sections": [{"name": "Hero", "layout": "split 7/5", "background": "bg", "images": ["hero"],
                          "copy": {"title": "Roasted this morning.", "ctas": ["Shop beans", "Our story"]}},
                         {"name": "Features", "layout": "stat band", "background": "surface",
                          "copy": {"title": "Why Ember", "items": [{"title": "Fresh", "icon": "coffee"}]}},
                         {"name": "Footer", "layout": "columns", "background": "bg"}]}))
        hero_bad = "const root = await J.node(\"x\");\nconst sec = J.section(\"Hero\", { parent: root ;"
        hero_runtime_bad = "const sec = J.section(\"Hero\", { parent: figma.currentPage });\nundefinedHelper();"
        model.on("section 1 of 3 (", hero_bad, hero_runtime_bad, "__HERO__")
        model.on("section 2 of 3 (", "__FEATURES__")
        model.on("section 3 of 3 (", "__FOOTER__")
        model.on("design director judging", json.dumps({
            "score": 6, "fixes": [{"node_id": "?", "section": "Footer", "issue": "cramped", "fix": "more air"}]}))
        model.on("Apply these design review fixes to the page",
                 'const r = await J.node("{target}");\nr.children[2].name = "Footer (roomy)";\n'
                 'J.focus(r);\nreturn J.summary(r);')
        install_model(model)

        from core.photos import Photo
        import base64
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (640, 400), (190, 110, 60)).save(buf, format="JPEG")
        jpeg_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        wanted_photos = []

        def fake_fetch(wanted, log=print, budget_seconds=60.0):
            wanted_photos.extend(wanted)
            return {"hero": jpeg_b64}, [Photo(query="coffee roastery dawn", width=640, height=400,
                                              title="Roastery at dawn", creator="Ana", license="CC BY 2.0",
                                              source="https://example.org/r", attribution="")]
        real_fetch = fd.photo_source.fetch_assets
        fd.photo_source.fetch_assets = fake_fetch

        real_ask = fd._ask

        def ask(prompt, timeout_ms=0):
            reply = model.text(prompt)
            root_id = (fd._load_state().get("last") or {}).get("root_id") or ""
            if "The page root frame exists" in prompt:
                root_id = prompt.split('J.node("')[1].split('"')[0]
            if "Target node id: " in prompt:
                reply = reply.replace("{target}", prompt.split("Target node id: ")[1].split(".")[0])
            for token, name, headline in (("__HERO__", "Hero", "Roasted this morning."),
                                          ("__FEATURES__", "Features", "Why Ember"),
                                          ("__FOOTER__", "Footer", "Ember Roasters")):
                if reply == token:
                    return KIT_SECTION.format(root=root_id, name=name, headline=headline)
            return reply
        fd._ask = ask

        user_layer = fd._run(fd._BridgeRunner(bridge),
                             'const f = figma.createFrame(); f.name = "User Layer"; return f.id;',
                             "a layer the user made")
        check("a layer of the user's exists before the design", user_layer.get("ok"), user_layer)
        robust = fd._run(fd._BridgeRunner(bridge),
                         'const f = J.frame({ name: "Robust", width: {}, height: "auto", gap: "24px", '
                         'pad: ["16px", 8], radius: "12" });\n'
                         'const r = J.frame({ name: "Row", dir: "HORIZONTAL", align: "baseline", parent: f });\n'
                         'const fixed = J.frame({ name: "Fixed", width: 400 });\n'
                         'const v = J.frame({ name: "Col", align: "baseline", parent: fixed, width: "100%" });\n'
                         'const hugger = J.frame({ name: "Hugger" });\n'
                         'const lost = J.frame({ name: "Lost", parent: hugger, fillWidth: true });\n'
                         'f.fills = [J.paint({ r: 1, g: 0.5, b: 0, a: 0.5 })];\n'
                         'const out = { gap: f.itemSpacing, radius: f.cornerRadius, sizing: f.layoutSizingHorizontal, '
                         'opacity: f.fills[0].opacity, vSizing: v.layoutSizingHorizontal, '
                         'lostSizing: lost.layoutSizingHorizontal, warnings: J.warnings.slice() };\n'
                         'f.remove(); fixed.remove(); hugger.remove();\nreturn out;', "kit robustness")
        check("the kit turns '24px', '12', {} and 'auto' into numbers or defaults instead of throwing",
              robust.get("ok") and robust["result"]["gap"] == 24 and robust["result"]["radius"] == 12
              and robust["result"]["sizing"] == "HUG", robust)
        check("an RGBA colour becomes RGB + opacity, BASELINE on a column falls back, '100%' fills",
              robust.get("ok") and robust["result"]["opacity"] == 0.5 and robust["result"]["vSizing"] == "FILL",
              robust)
        check("FILL inside a parent that only hugs is refused with a warning instead of collapsing",
              robust.get("ok") and robust["result"]["lostSizing"] == "HUG"
              and any("hugs its content" in w for w in robust["result"]["warnings"]), robust)

        player = FakePlayer()
        fd._busy.acquire()
        fd._design_worker({"brief": "landing page for a coffee roastery", "style": "warm, dark"},
                          player)
        fd._ask = real_ask
        fd.photo_source.fetch_assets = real_fetch
        state = fd._load_state().get("last") or {}
        check("the design finished with every section built",
              state.get("sections") == ["Hero", "Features", "Footer"], (state, player.logs[-6:]))
        check("the planner's photo list is fetched", [w["key"] for w in wanted_photos] == ["hero"], wanted_photos)
        check("the concept reaches the section prompts",
              any("Warm analog dawn light" in p and "BUILD NOW" in p for p in model.prompts))
        check("the automatic review ran and its fix was applied",
              (state.get("review") or {}).get("score") == 6 and (state.get("review") or {}).get("applied"),
              state.get("review"))
        check("photo credits are kept with the design", state.get("photos") and "Ana" in state["photos"][0]["credit"],
              state.get("photos"))
        check("an unknown font in the plan is dropped, a known one kept",
              state.get("theme", {}).get("headingFont") == "Poppins"
              and "bodyFont" not in state.get("theme", {}), state.get("theme"))
        check("a syntax error is caught by Node before reaching Figma",
              any("SyntaxError before running" in p for p in model.prompts), "")
        check("a runtime error in Figma is fed back for a fix",
              any("undefinedHelper is not defined" in p for p in model.prompts), "")
        page = sim.dump()
        roots = [c for c in page["children"] if c["name"] == "Ember Roasters — Desktop 1440"]
        check("the page root frame exists in the file", len(roots) == 1, [c["name"] for c in page["children"]])
        names = [c["name"] for c in roots[0].get("children", [])] if roots else []
        check("the failed attempt left no half-built section behind — and the review renamed the footer",
              names == ["Hero", "Features", "Footer (roomy)"], names)
        stray = [c for c in page["children"] if c["name"] == "Hero"]
        check("nodes a failed attempt built outside the root are removed too", not stray,
              [c["name"] for c in page["children"]])
        check("cleanup never touches the user's own layers",
              any(c["name"] == "User Layer" for c in page["children"]),
              [c["name"] for c in page["children"]])
        check("the page root hugs its sections (resize() no longer locks its height)",
              roots and roots[0]["sizing"] == "FIXED/HUG", roots[0]["sizing"] if roots else None)
        png_size = Path(state.get("png", "")).stat().st_size if state.get("png") else 0
        check("a full-size PNG export survives the trip from Figma intact", png_size == 6000, png_size)
        def find(node, name):
            if node.get("name") == name:
                return node
            for child in node.get("children", []):
                hit = find(child, name)
                if hit:
                    return hit
            return {}
        hero_text = find(roots[0]["children"][0], "Headline") if roots else {}
        check("headings use the planned font and weight (Poppins SemiBold, resolved from the real list)",
              hero_text.get("font") == {"family": "Poppins", "style": "SemiBold"}, hero_text.get("font"))
        check("the display role sets the big size", hero_text.get("size") == 104, hero_text.get("size"))
        fills = fd._run(fd._BridgeRunner(bridge),
                        f'const r = await J.node("{roots[0]["id"]}");\n'
                        'return r.findAllWithCriteria({ types: ["FRAME"] })'
                        '.filter((n) => n.name.startsWith("Image/hero")).map((n) => n.fills.map((f) => f.type));',
                        "reading image fills")
        check("the hero shows the real photo (with its overlay); other sections fall back to light",
              fills.get("ok") and fills["result"][0] == ["IMAGE", "GRADIENT_LINEAR"]
              and all(f[0] == "GRADIENT_LINEAR" for f in fills["result"][1:]), fills)
        icon_nodes = fd._run(fd._BridgeRunner(bridge),
                             f'const r = await J.node("{roots[0]["id"]}");\n'
                             'return r.findAll((n) => n.name.startsWith("icon/")).map((n) => n.name);',
                             "reading icons")
        check("icons are real SVG vectors, injected only as used",
              icon_nodes.get("ok") and "icon/coffee" in icon_nodes["result"] and "icon/leaf" in icon_nodes["result"],
              icon_nodes)
        check("the finished design was exported as PNG", Path(state.get("png", "")).is_file(), state.get("png"))
        check("the user is told the design is finished",
              player.said and "Ember Roasters" in player.said[-1], player.said[-1:])

        model.on("MODIFY AN EXISTING DESIGN",
                 'const t = await J.node("{id}");\nt.name = "Hero (dark)";\nJ.focus(t);\nreturn J.summary(t);')
        hero_id = roots[0]["children"][0]["id"]
        real_ask = fd._ask
        fd._ask = lambda prompt, timeout_ms=0: model.text(prompt).replace("{id}", hero_id)
        bridge._set_context({"fileName": "Test File", "selection": [{"id": hero_id}]})
        res = fd.figma_design({"action": "edit", "instruction": "rename the hero"}, player)
        fd._ask = real_ask
        check("edit applies a change to the selected layer", res.startswith("status=done"), res)
        check("the edit is visible in the file",
              any(c["name"] == "Hero (dark)" for c in
                  [r for r in sim.dump()["children"] if r["name"] == "Ember Roasters — Desktop 1440"][0]["children"]))

        res = fd.figma_design({"action": "inspect"}, player)
        check("inspect returns the structure as untrusted data",
              "<<<UNTRUSTED source=figma>>>" in res and "Hero (dark)" in res, res[:200])
        check("the outline keeps children and text — not just the top node",
              '"children"' in res and "Roasted this morning." in res, res[:400])
        res = fd.figma_design({"action": "script", "code": "return figma.currentPage.children.length;"}, player)
        check("script runs raw Plugin API code", res.startswith("status=done") and "result=" in res, res)
        res = fd.figma_design({"action": "script", "code": "const = 1"}, player)
        check("script refuses code that does not parse", res.startswith("status=failed") and "parse" in res, res)
        res = fd.figma_design({"action": "export", "node_id": hero_id, "scale": 1}, player)
        check("export writes a PNG", res.startswith("status=done") and ".png" in res, res)
        res = fd.figma_design({"action": "status"}, player)
        check("status reports the connected plugin", "plugin_connected=true" in res, res)
        res = fd.figma_design({"action": "design"}, player)
        check("design without a brief asks for one", res.startswith("status=ask"), res)
        res = fd.figma_design({"action": "nonsense"}, player)
        check("an unknown action fails cleanly", res.startswith("status=failed"), res)

        res = fd._run(fd._BridgeRunner(bridge),
                      'return J.spec(await J.node("' + roots[0]["id"] + '"), 7);', "spec")
        spec_text = json.dumps(res.get("result"))
        check("J.spec returns the full deep tree for design-to-code",
              res.get("ok") and "Roasted this morning." in spec_text and "[…]" not in spec_text,
              spec_text[:200])

        root_id = roots[0]["id"]
        model.on("design director judging", json.dumps({
            "score": 7, "fixes": [{"node_id": root_id, "issue": "Footer is cramped", "fix": "rename"}]}))
        model.on("Apply these design review fixes",
                 'const f = (await J.node("{root}")).children[2];\nf.name = "Footer (polished)";\n'
                 'const t = await J.node("{root}");\nJ.focus(t);\nreturn J.summary(t);')
        real_ask = fd._ask
        fd._ask = lambda prompt, timeout_ms=0: model.text(prompt).replace("{root}", root_id)
        res = fd.figma_design({"action": "polish", "node_id": root_id}, player)
        fd._ask = real_ask
        check("polish reviews a screenshot and applies the fixes",
              res.startswith("status=done") and "review_score=7" in res, res)
        check("the polish change is visible in the file",
              any(c["name"] == "Footer (polished)" for c in
                  [r for r in sim.dump()["children"] if r["name"] == "Ember Roasters — Desktop 1440"][0]["children"]))

        web_builder.SITES_DIR = SANDBOX / "sites"
        model.on("Build a complete, production-quality static website", SITE_GOOD)
        fd._busy.acquire()
        fd._to_code_worker({"open": False}, player, root_id)
        site_prompt = next((p for p in reversed(model.prompts)
                            if "production-quality static website" in p), "")
        check("to_code hands the real layer spec to the site writer",
              "FIGMA LAYER SPEC" in site_prompt and "Roasted this morning." in site_prompt)
        built = sorted(p.name for p in (SANDBOX / "sites").glob("*/index.html"))
        check("to_code saves a website from the design", bool(built), built)
        check("the user is told the website is ready", "website was generated" in player.said[-1],
              player.said[-1:])
        web_builder.stop_serving()
    finally:
        sim.stop()
        bridge.stop()


SITE_GOOD = """=== FILE: index.html ===
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ember</title><link rel="stylesheet" href="styles.css"></head>
<body><header><button class="menu" aria-expanded="false">Menu</button></header>
<main><h1>Roasted this morning.</h1></main><script type="module" src="app.js"></script></body></html>
=== END FILE ===
=== FILE: styles.css ===
:root { --bg: #120d0a; } body { background: var(--bg); }
=== END FILE ===
=== FILE: app.js ===
const MENU_SELECTOR = ".menu";
/** Toggle the mobile menu. @param {HTMLButtonElement} button @returns {void} */
export function toggle(button) {
  const open = button.getAttribute("aria-expanded") === "true";
  button.setAttribute("aria-expanded", String(!open));
}
document.querySelector(MENU_SELECTOR)?.addEventListener("click", (event) => toggle(event.currentTarget));
=== END FILE ===
=== FILE: ../../escape.js ===
alert(1)
=== END FILE ==="""


def test_web_builder():
    section("core/web_builder — generate, validate, repair, save, serve")
    from core import web_builder
    web_builder.SITES_DIR = SANDBOX / "sites_builder"
    files = web_builder.parse_files(SITE_GOOD)
    check("marker files are parsed and path traversal is dropped",
          sorted(files) == ["app.js", "index.html", "styles.css"], sorted(files))
    if HAS_NODE:
        check("a clean site validates", web_builder.validate(files) == [], web_builder.validate(files))
        broken = dict(files, **{"app.js": "export function x( {\n"})
        probs = web_builder.validate(broken)
        check("a script that does not parse is reported", any("does not parse" in p for p in probs), probs)
    else:
        skip("JavaScript validation", "Node.js is not installed")
    missing = {"index.html": files["index.html"]}
    probs = web_builder.validate(missing)
    check("missing referenced files are reported",
          any("styles.css" in p for p in probs) and any("app.js" in p for p in probs), probs)

    model = FakeModel()
    first = SITE_GOOD.replace('const open = button.getAttribute("aria-expanded") === "true";',
                              "const open = = 1;")
    model.on("Build a complete, production-quality static website", first)
    model.on("These website files have problems", SITE_GOOD.split("=== FILE: index.html")[0]
             + "=== FILE: app.js ===\n" + SITE_GOOD.split("=== FILE: app.js ===\n")[1].split("=== END FILE ===")[0]
             + "=== END FILE ===")
    install_model(model)
    out, problems = web_builder.generate_site("coffee roastery", log=lambda m: None)
    if HAS_NODE:
        check("a broken script is repaired by the model", problems == [] and "=== END" not in out["app.js"],
              problems)
    folder = web_builder.save_site(out, "Ember Roasters")
    check("the site is saved into its own folder", (folder / "index.html").is_file(), folder)
    again = web_builder.save_site(out, "Ember Roasters")
    check("an existing folder is never overwritten", again != folder and again.name.endswith("-2"), again)
    url = web_builder.serve(folder, open_browser=False)
    with urllib.request.urlopen(url + "index.html", timeout=5) as r:
        body = r.read().decode()
    check("the preview server serves the site", "Roasted this morning." in body)
    check("serving the same folder twice reuses the server", web_builder.serve(folder, open_browser=False) == url)
    check("stop_serving stops it", web_builder.stop_serving(folder) == 1 and not web_builder.serving())

    site = SANDBOX / "client_site"
    (site / "img").mkdir(parents=True)
    (site / "node_modules" / "pkg").mkdir(parents=True)
    (site / "index.html").write_text(files["index.html"].replace(
        "<main>", '<main><img src="img/logo.png" alt="Ember">'), encoding="utf-8")
    (site / "styles.css").write_text(files["styles.css"] + "\nheader { background: url('img/bg.png'); }\n")
    (site / "app.js").write_text(files["app.js"])
    (site / "img" / "logo.png").write_bytes(b"\x89PNG")
    (site / "img" / "bg.png").write_bytes(b"\x89PNG")
    (site / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1;\n")
    (site / "unrelated.html").write_text("<html><body>other</body></html>")
    root, found, assets = web_builder.collect_site(site)
    check("a site folder is collected without node_modules",
          sorted(found) == ["app.js", "index.html", "styles.css", "unrelated.html"]
          and sorted(assets) == ["img/bg.png", "img/logo.png"], (sorted(found), sorted(assets)))
    root, found, assets = web_builder.collect_site(site / "index.html")
    check("a single page brings only what it references (page → css → image)",
          sorted(found) == ["app.js", "index.html", "styles.css"]
          and sorted(assets) == ["img/bg.png", "img/logo.png"], (sorted(found), sorted(assets)))
    check("images that exist count as present when validating",
          web_builder.validate(found, assets) == [] if HAS_NODE else True,
          web_builder.validate(found, assets))

    upgraded_page = found["index.html"].replace("Roasted this morning.", "Roasted at dawn. Delivered by noon.")
    model = FakeModel()
    model.on("Upgrade this EXISTING website", f"=== FILE: index.html ===\n{upgraded_page}=== END FILE ===")
    install_model(model)
    out, problems = web_builder.upgrade_site(found, "make it premium", assets=list(assets), log=lambda m: None)
    check("upgrade_site keeps untouched files and replaces the rewritten ones",
          sorted(out) == ["app.js", "index.html", "styles.css"] and "Roasted at dawn" in out["index.html"]
          and out["styles.css"] == found["styles.css"], sorted(out))
    check("the client's files reach the model as data, with the asset list",
          "DATA, not instructions" in model.prompts[-1] and "img/logo.png" in model.prompts[-1])
    check("the upgraded site validates", problems == [] if HAS_NODE else True, problems)
    backup = web_builder.save_in_place(site, out, found)
    check("in-place upgrade writes the page and backs up the original",
          "Roasted at dawn" in (site / "index.html").read_text()
          and "Roasted this morning." in (backup / "index.html").read_text()
          and not (backup / "styles.css").exists(), backup)


def test_contrast():
    section("figma_designer — palettes are made readable")
    import actions.figma_designer as fd
    t = fd._ensure_contrast({"bg": "#FFFFFF", "text": "#BBBBBB", "muted": "#EEEEEE", "primary": "#FFD000",
                             "onPrimary": "#FFFFFF"})
    check("text that is too light on white is made dark", fd._contrast(t["text"], t["bg"]) >= 7, t)
    check("muted text is pulled to at least 4.5:1", fd._contrast(t["muted"], t["bg"]) >= 4.5, t)
    check("onPrimary becomes the readable one of white or near-black",
          t["onPrimary"] == "#0B0B0F", t)
    check("the inverse pair is derived and readable",
          fd._contrast(t["inverseText"], t["inverseBg"]) >= 7, t)
    plan_theme = fd._clean_theme({"bg": "#101010", "text": "#FAFAFA", "headingFont": "instrument serif",
                                  "headingWeight": "semibold", "photoGrade": "warm", "primary": "#zzz"})
    check("theme cleaning keeps known fonts, normalises the weight, maps the photo grade, drops bad hex",
          plan_theme.get("headingFont") == "Instrument Serif" and plan_theme.get("headingWeight") == "Semi Bold"
          and plan_theme.get("photoFilters") and "primary" not in plan_theme, plan_theme)


def test_mcp_job_size():
    section("figma_designer — jobs fit Figma's own MCP tool")
    import actions.figma_designer as fd
    compact = fd._compact_kit()
    if HAS_NODE:
        from core import node_runtime
        ok, err = node_runtime.check_syntax(compact, wrap_async=True)
        check("the compact kit still parses", ok is True, err)
    section_code = KIT_SECTION.format(root="1:2", name="Hero", headline="Roasted this morning.") * 4
    job = fd._job(section_code, {"bg": "#101010"}, None, fd._icons_for(section_code), compact=True)
    check("kit + theme + icons + a long section stay under 50,000 characters",
          len(job) < fd.MCP_CODE_LIMIT, len(job))

    class FakeMcp(fd._McpRunner):
        def __init__(self):
            self.sent = None

        def run(self, code, description, timeout=0):
            self.sent = code
            return {"ok": True}
    runner = FakeMcp()
    res = fd._run(runner, "x();" * 20_000, "too big")
    check("an oversized job is refused locally with a budget the model can act on",
          not res["ok"] and "budget" in res["error"] and runner.sent is None, res)
    found = fd._icons_for('const items = [{"title": "Fresh", "icon": "leaf"}];')
    check("icon names given as data are found", "leaf" in found and "arrow-right" in found, sorted(found))


def test_photos():
    section("core/photos — real photos by search, cached, never fatal")
    import io
    from urllib.parse import parse_qs, urlsplit
    from core import photos
    try:
        from PIL import Image
    except ImportError:
        skip("photos", "Pillow is not installed")
        return
    buf = io.BytesIO()
    Image.new("RGB", (2400, 1200), (200, 120, 60)).save(buf, format="JPEG")
    big_jpeg = buf.getvalue()
    hits = {"search": 0, "image": 0, "commons": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def _send(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            q = parse_qs(url.query)
            base = f"http://127.0.0.1:{self.server.server_address[1]}"
            if url.path == "/v1/images/":
                hits["search"] += 1
                term = q.get("q", [""])[0]
                results = [] if term == "nothing here" else [
                    {"url": base + "/missing.jpg", "width": 4000, "height": 2000, "title": "broken"},
                    {"url": base + "/img/roastery.jpg", "thumbnail": base + "/img/roastery.jpg",
                     "width": 2400, "height": 1200, "title": "Roastery", "creator": "Ana",
                     "license": "by", "license_version": "2.0",
                     "foreign_landing_url": "https://example.org/roastery"}]
                self._send(json.dumps({"results": results}).encode(), "application/json")
            elif url.path == "/w/api.php":
                hits["commons"] += 1
                pages = {"1": {"title": "File:Beans.jpg", "imageinfo": [{
                    "mime": "image/jpeg", "thumburl": base + "/img/roastery.jpg", "width": 2400,
                    "height": 1200, "descriptionurl": "https://commons.example/Beans",
                    "extmetadata": {"Artist": {"value": "Ben"}, "LicenseShortName": {"value": "CC BY-SA 4.0"}}}]}}
                self._send(json.dumps({"query": {"pages": pages}}).encode(), "application/json")
            elif url.path == "/img/roastery.jpg":
                hits["image"] += 1
                self._send(big_jpeg, "image/jpeg")
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    photos.OPENVERSE = base + "/v1/images/"
    photos.COMMONS = base + "/w/api.php"
    photos.CACHE_DIR = SANDBOX / "photos"
    try:
        p = photos.find_photo("coffee roastery", "landscape", log=lambda m: None)
        check("a photo is found, skipping a broken result", p is not None and p.title == "Roastery", p)
        check("it is downscaled to at most 1600 px", p and max(p.width, p.height) == 1600, p and (p.width, p.height))
        check("its licence and creator are kept for the credits", p and p.license == "CC BY 2.0" and p.creator == "Ana")
        before = dict(hits)
        again = photos.find_photo("Coffee  Roastery", "landscape", log=lambda m: None)
        check("the same query is served from the cache", again and hits == before, hits)
        p2 = photos.find_photo("nothing here", "landscape", log=lambda m: None)
        check("Wikimedia Commons is the fallback when Openverse finds nothing",
              p2 is not None and hits["commons"] == 1 and p2.license == "CC BY-SA 4.0", hits)
        assets, found = photos.fetch_assets([{"key": "hero", "query": "coffee roastery"},
                                             {"key": "team", "query": "barista portrait", "orientation": "portrait"}],
                                            log=lambda m: None)
        check("fetch_assets returns base64 per key", set(assets) == {"hero", "team"}
              and base64_ok(assets["hero"]), sorted(assets))
        check("credits list every photo", photos.credits(found).count("•") == 2, photos.credits(found))
    finally:
        srv.shutdown()
    photos.OPENVERSE = f"http://127.0.0.1:{free_port()}/v1/images/"
    photos.COMMONS = f"http://127.0.0.1:{free_port()}/w/api.php"
    check("no network means no photo — and no exception",
          photos.find_photo("mountains at dawn", "landscape", log=lambda m: None) is None)


def base64_ok(text: str) -> bool:
    import base64
    try:
        return base64.b64decode(text, validate=True)[:2] == b"\xff\xd8"
    except Exception:
        return False


def test_js_studio():
    section("actions/js_studio — write, check, run, test, fix, review")
    if not HAS_NODE:
        skip("js_studio", "Node.js is not installed")
        return
    import actions.js_studio as js
    js.CODE_DIR = SANDBOX / "code"
    js.STATE_PATH = SANDBOX / "state" / "js.json"
    player = FakePlayer()
    model = FakeModel()
    good = ("/**\n * Add two numbers.\n * @param {number} a\n * @param {number} b\n * @returns {number}\n */\n"
            "export function add(a, b) {\n  if (typeof a !== \"number\" || typeof b !== \"number\") {\n"
            "    throw new TypeError(\"add expects numbers\");\n  }\n  return a + b;\n}\n\n"
            "if (import.meta.url === `file://${process.argv[1]}`) {\n  console.log(add(2, 3));\n}\n")
    model.on("principal JavaScript engineer. Write ONE complete file", "export function add(a, b) { return a + ; }")
    model.on("does not parse", good)
    tests = ('import test from "node:test";\nimport assert from "node:assert/strict";\n'
             'import { add } from "./adder.mjs";\n'
             'test("adds", () => { assert.equal(add(2, 3), 5); });\n'
             'test("rejects strings", () => { assert.throws(() => add("2", 3), TypeError); });\n')
    model.on("Write a thorough test file", tests)
    install_model(model)
    res = js.js_studio({"action": "write", "description": "add two numbers",
                        "output_path": "adder.mjs", "with_tests": True}, player)
    check("write repairs a syntax error and reports a checked file",
          res.startswith("status=done") and "checked=true" in res, res)
    check("with_tests writes and passes node:test tests", "tests_passed=true" in res, res)
    res = js.js_studio({"action": "run", "file_path": str(js.CODE_DIR / "adder.mjs")}, player)
    check("run executes with Node and returns output as data", res.startswith("status=done") and "5" in res, res)
    res = js.js_studio({"action": "test", "file_path": str(js.CODE_DIR / "adder.mjs")}, player)
    check("test finds the test file beside the module", "passed=true" in res, res)
    res = js.js_studio({"action": "check", "code": "var x = 1; if (x == 2) { eval('1'); }\n"}, player)
    check("check reports var, == and eval", all(r in res for r in ("no-var", "eqeqeq", "no-eval")), res)
    res = js.js_studio({"action": "check", "code": "// var is mentioned only in a comment\nconst y = 'a == b';\n"}, player)
    check("lint ignores comments and strings", "lint=clean" in res, res)
    broken = js.CODE_DIR / "broken.mjs"
    broken.write_text("export const x = ;\n")
    model.on("Fix this JavaScript file", "export const x = 1;\n")
    res = js.js_studio({"action": "fix", "file_path": str(broken)}, player)
    check("fix repairs a file and keeps a backup",
          res.startswith("status=done") and (js.CODE_DIR / "broken.mjs.bak").exists(), res)
    model.on("doing a code review", json.dumps({"summary": "Solid.", "findings": [
        {"heading": "Input", "detail": "ok", "severity": "note", "quote": "", "suggestion": ""}]}))
    res = js.js_studio({"action": "review", "file_path": str(js.CODE_DIR / "adder.mjs")}, player)
    check("review lays findings onto the review panel", res.startswith("status=done") and player.reviews, res)
    res = js.js_studio({"action": "status"}, player)
    check("status reports Node", "node=v" in res, res)

    from core import web_builder
    web_builder.SITES_DIR = SANDBOX / "sites_upgrade"
    site = SANDBOX / "shop"
    (site / "img").mkdir(parents=True)
    page = SITE_GOOD.split("=== FILE: index.html ===\n")[1].split("=== END FILE ===")[0]
    (site / "index.html").write_text(page.replace("<main>", '<main><img src="img/hero.jpg" alt="">'))
    (site / "styles.css").write_text(":root { --bg: #000; }\n")
    (site / "app.js").write_text(SITE_GOOD.split("=== FILE: app.js ===\n")[1].split("=== END FILE ===")[0])
    (site / "img" / "hero.jpg").write_bytes(b"\xff\xd8")
    model.on("Upgrade this EXISTING website",
             "=== FILE: styles.css ===\n:root { --bg: #0b0b0f; --accent: #e07a3f; }\nbody { margin: 0; }\n=== END FILE ===")
    res = js.js_studio({"action": "improve", "file_path": str(site / "index.html")}, player)
    check("'improve' maps to upgrade and starts in the background",
          res.startswith("status=started") and "files=3" in res, res)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not any("upgrade is finished" in x or "failed" in x for x in player.said):
        time.sleep(0.1)
    copies = list((SANDBOX / "sites_upgrade").glob("shop-pro*/styles.css"))
    check("the upgrade is saved as a copy with the rewritten file",
          copies and "--accent" in copies[0].read_text(), copies)
    check("the copy carries the site's images",
          copies and (copies[0].parent / "img" / "hero.jpg").is_file())
    check("the original site is untouched by default", "--accent" not in (site / "styles.css").read_text())
    check("the user is told when the upgrade is done",
          any("upgrade is finished" in x for x in player.said), player.said[-1:])
    web_builder.stop_serving()
    res = js.js_studio({"action": "upgrade", "file_path": str(SANDBOX / "code")}, player)
    check("a folder without an HTML page is refused", res.startswith("status=failed") and "no HTML" in res, res)

    from core.action_loader import discover_actions
    reg = discover_actions(ROOT / "actions", logger=lambda m: None)
    hooked = []
    import actions.figma_designer as fd_mod
    original = fd_mod.attach_ui
    fd_mod.attach_ui = lambda ui: hooked.append(ui)
    try:
        reg.attach_ui(player)
    finally:
        fd_mod.attach_ui = original
    check("the loader hands the HUD to actions that ask for it", hooked == [player], hooked)


def test_tools():
    section("utility tools")
    from actions.calculator import calculator, convert, evaluate
    check("17.5% of 2,340 = 409.5", evaluate("17.5% of 2,340") == 409.5, evaluate("17.5% of 2,340"))
    check("powers, roots and factorial", evaluate("2^10 + sqrt(16) + 5!") == 1148)
    check("trigonometry is in degrees", abs(evaluate("sin(30)") - 0.5) < 1e-12)
    check("attribute access is refused", calculator({"action": "calculate",
                                                     "expression": "().__class__"}).startswith("status=failed"))
    check("huge exponents are refused", calculator({"action": "calculate",
                                                    "expression": "9**9**9"}).startswith("status=failed"))
    check("division by zero is reported", "division by zero" in calculator({"action": "calculate", "expression": "1/0"}))
    check("100 °F = 37.78 °C", round(convert(100, "f", "c"), 2) == 37.78)
    check("26.2 miles in km", round(convert(26.2, "miles", "km"), 2) == 42.16)
    check("mixing dimensions is refused", calculator({"action": "convert", "value": "1", "from_unit": "kg",
                                                      "to_unit": "km"}).startswith("status=failed"))
    res = calculator({"action": "days_between", "date": "2026-01-01", "date2": "2026-12-25"})
    check("days between dates", "days=358" in res, res)

    import actions.quick_notes as qn
    qn.NOTES_PATH = SANDBOX / "notes.json"
    qn.EXPORT_DIR = SANDBOX / "export"
    r1 = qn.quick_notes({"action": "add", "text": "Try the Figma kit #design"})
    qn.quick_notes({"action": "add", "text": "Buy oat milk"})
    check("notes are added with hashtags as tags", "saved=1" in r1 and "tags=design" in r1, r1)
    check("search finds by word", "found=1" in qn.quick_notes({"action": "search", "query": "figma"}))
    check("delete last removes the newest", "deleted=2" in qn.quick_notes({"action": "delete", "note_id": "last"}))
    check("clear asks before deleting", qn.quick_notes({"action": "clear"}).startswith("status=ask"))
    check("export writes Markdown", "exported=" in qn.quick_notes({"action": "export"}))

    from actions.dev_toolbox import dev_toolbox
    check("sha256 of 'abc'", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
          in dev_toolbox({"action": "hash", "text": "abc"}))
    check("base64 round trip", "aGVsbG8=" in dev_toolbox({"action": "base64_encode", "text": "hello"})
          and "hello" in dev_toolbox({"action": "base64_decode", "text": "aGVsbG8="}))
    check("invalid JSON names the position", "line 1" in dev_toolbox({"action": "json", "text": "{x"}))
    res = dev_toolbox({"action": "password", "length": 16})
    check("a password is never returned in the result",
          "copied to clipboard" in res or ("not read out" in res and "password=" not in res), res)
    res = dev_toolbox({"action": "color", "text": "#4F8CFF"})
    check("colour conversion with contrast", "rgb(79, 140, 255)" in res and "best_text=" in res, res)
    check("case conversion", "result=userProfileId" in dev_toolbox({"action": "case", "text": "user profile ID",
                                                                     "mode": "camel"}))
    jwt = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJsdWthcyIsImV4cCI6MX0.sig")
    res = dev_toolbox({"action": "jwt", "text": jwt})
    check("jwt decode flags expiry, never verifies", "expired=true" in res and "NOT verified" in res, res)

    from actions.network_tools import network_tools
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    lport = listener.getsockname()[1]
    res = network_tools({"action": "port", "host": "127.0.0.1", "port": f"{lport},{free_port()}"})
    check("port check tells open from closed", f"{lport}:open" in res and "closed" in res, res)
    listener.close()
    res = network_tools({"action": "dns", "host": "localhost"})
    check("dns resolves localhost", res.startswith("status=done"), res)
    check("a hostile host string is refused",
          network_tools({"action": "ping", "host": "-oProxyCommand=x"}).startswith("status=failed"))

    srv = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (BaseHTTPRequestHandler,), {
        "log_message": lambda *a: None,
        "do_GET": lambda self: (self.send_response(301 if self.path == "/" else 200),
                                self.send_header("Location", "/ok") if self.path == "/" else None,
                                self.send_header("Content-Length", "0"), self.end_headers())}))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    res = network_tools({"action": "http", "url": f"http://127.0.0.1:{srv.server_address[1]}/"})
    check("http follows redirects and reports the final status",
          res.startswith("status=done") and "status=200" in res and "301->" in res, res)
    srv.shutdown()

    if shutil.which("git"):
        import actions.git_helper as gh
        gh.STATE_PATH = SANDBOX / "state" / "git.json"
        repo = SANDBOX / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
        (repo / "a.txt").write_text("hello\n")
        res = gh.git_helper({"action": "status", "repo_path": str(repo)})
        check("git status counts untracked files", "untracked=1" in res, res)
        res = gh.git_helper({"action": "commit", "message": "Add a", "add_all": True})
        check("git commit remembers the repository", res.startswith("status=done") and "files=1" in res, res)
        check("git log shows the commit", "Add a" in gh.git_helper({"action": "log"}))
    else:
        skip("git_helper", "git is not installed")


def test_discovery():
    section("discovery — every new tool loads through the real loader")
    from core.action_loader import discover_actions
    reg = discover_actions(ROOT / "actions", logger=lambda m: None)
    for name in ("figma_design", "js_studio", "mcp_tools", "network_tools", "calculator",
                 "quick_notes", "dev_toolbox", "git_helper"):
        check(f"{name} is discovered", reg.has(name))
    decls = {d["name"]: d for d in reg.get_tool_declarations()}
    check("new tools carry a tier and a brief for small-model budgets",
          all(decls[n].get("tier") == "extra" and decls[n].get("brief")
              for n in ("figma_design", "js_studio", "calculator") if n in decls))
    prompt = (ROOT / "core" / "prompt.txt").read_text(encoding="utf-8")
    check("the prompt names both creators", "Lukas Chiller" in prompt and "Schleimy" in prompt)


def main() -> int:
    tests = (test_mcp_client, test_manifest, test_bridge, test_contrast, test_mcp_job_size, test_photos, test_figma_designer, test_web_builder,
             test_js_studio, test_tools, test_discovery)
    for fn in tests:
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f"{fn.__name__} ran to completion", False, f"{type(e).__name__}: {e}")
    print(f"\n{_ok} passed, {_fail} failed, {_skip} skipped")
    if _failures:
        print("Failed:\n  - " + "\n  - ".join(_failures))
    shutil.rmtree(SANDBOX, ignore_errors=True)
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
