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
        self.base = f"http://127.0.0.1:{port}"
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
const sec = J.section("{name}", {{ parent: root, align: "center", gap: 24 }});
await J.text("{headline}", {{ parent: sec, size: 64, heading: true, align: "center" }});
await J.text("Small-batch coffee, roasted to order and delivered in 48 hours.", {{ parent: sec, size: 20, color: "muted", fillWidth: true, lineHeight: 1.5 }});
const row = J.frame({{ parent: sec, dir: "HORIZONTAL", gap: 12, wrap: true }});
await J.button("Shop beans", {{ parent: row, size: "lg" }});
await J.button("Our story", {{ parent: row, variant: "ghost" }});
const grid = J.frame({{ parent: sec, dir: "HORIZONTAL", gap: 24, fillWidth: true }});
for (const title of ["Fresh", "Fair", "Fast"]) {{
  const card = J.frame({{ parent: grid, gap: 12, pad: 24, fill: "surface", radius: true, stroke: "border", grow: true }});
  await J.icon("☕", {{ parent: card }});
  await J.text(title, {{ parent: card, size: 22, weight: "Semi Bold" }});
}}
await J.image({{ parent: sec, h: 420, fillWidth: true, label: "Roastery" }});
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

        model = FakeModel()
        model.on("planning a website design", json.dumps({
            "title": "Ember Roasters",
            "theme": {"bg": "#120D0A", "surface": "#1E1612", "surface2": "#2A1F19",
                      "text": "#FBF5EF", "muted": "#C9B8A8", "primary": "#E07A3F",
                      "accent": "#F2C14E", "onPrimary": "#120D0A", "border": "#3A2C24",
                      "radius": 20, "headingFont": "Poppins", "bodyFont": "Comic Sans"},
            "sections": [{"name": "Hero", "brief": "headline and buttons"},
                         {"name": "Features", "brief": "three cards"},
                         {"name": "Footer", "brief": "links"}]}))
        hero_bad = "const root = await J.node(\"x\");\nconst sec = J.section(\"Hero\", { parent: root ;"
        hero_runtime_bad = "const sec = J.section(\"Hero\", { parent: figma.currentPage });\nundefinedHelper();"
        model.on("section 1 of 3: Hero", hero_bad, hero_runtime_bad, "__HERO__")
        model.on("section 2 of 3: Features", "__FEATURES__")
        model.on("section 3 of 3: Footer", "__FOOTER__")
        install_model(model)

        real_ask = fd._ask

        def ask(prompt, timeout_ms=0):
            reply = model.text(prompt)
            root_id = (fd._load_state().get("last") or {}).get("root_id") or ""
            if "The page root frame exists" in prompt:
                root_id = prompt.split('J.node("')[1].split('"')[0]
            for token, name, headline in (("__HERO__", "Hero", "Roasted this morning."),
                                          ("__FEATURES__", "Features", "Why Ember"),
                                          ("__FOOTER__", "Footer", "Ember Roasters")):
                if reply == token:
                    return KIT_SECTION.format(root=root_id, name=name, headline=headline)
            return reply
        fd._ask = ask

        player = FakePlayer()
        fd._busy.acquire()
        fd._design_worker({"brief": "landing page for a coffee roastery", "style": "warm, dark"},
                          player)
        fd._ask = real_ask
        state = fd._load_state().get("last") or {}
        check("the design finished with every section built",
              state.get("sections") == ["Hero", "Features", "Footer"], (state, player.logs[-6:]))
        check("an unknown font in the plan is dropped, a known one kept",
              state.get("theme", {}).get("headingFont") == "Poppins"
              and "bodyFont" not in state.get("theme", {}), state.get("theme"))
        check("a syntax error is caught by Node before reaching Figma",
              any("SyntaxError before running" in p for p in model.prompts), "")
        check("a runtime error in Figma is fed back for a fix",
              any("undefinedHelper is not defined" in p for p in model.prompts), "")
        page = sim.dump()
        roots = [c for c in page["children"] if c["name"] == "Ember Roasters"]
        check("the page root frame exists in the file", len(roots) == 1, [c["name"] for c in page["children"]])
        names = [c["name"] for c in roots[0].get("children", [])] if roots else []
        check("the failed attempt left no half-built section behind",
              names == ["Hero", "Features", "Footer"], names)
        stray = [c for c in page["children"] if c["name"] == "Hero"]
        check("nodes a failed attempt built outside the root are removed too", not stray,
              [c["name"] for c in page["children"]])
        hero_text = roots[0]["children"][0]["children"][0] if names else {}
        check("headings use the planned font (Poppins, alias-mapped style)",
              hero_text.get("font", {}).get("family") == "Poppins", hero_text.get("font"))
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
                  [r for r in sim.dump()["children"] if r["name"] == "Ember Roasters"][0]["children"]))

        res = fd.figma_design({"action": "inspect"}, player)
        check("inspect returns the structure as untrusted data",
              "<<<UNTRUSTED source=figma>>>" in res and "Hero (dark)" in res, res[:200])
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
        model.on("principal product designer reviewing", json.dumps({
            "score": 7, "fixes": [{"issue": "Footer is cramped", "fix": "Rename Footer to Footer (roomy)"}]}))
        model.on("Apply these design review fixes",
                 'const f = (await J.node("{root}")).children[2];\nf.name = "Footer (roomy)";\n'
                 'const t = await J.node("{root}");\nJ.focus(t);\nreturn J.summary(t);')
        real_ask = fd._ask
        fd._ask = lambda prompt, timeout_ms=0: model.text(prompt).replace("{root}", root_id)
        res = fd.figma_design({"action": "polish", "node_id": root_id}, player)
        fd._ask = real_ask
        check("polish reviews a screenshot and applies the fixes",
              res.startswith("status=done") and "review_score=7" in res, res)
        check("the polish change is visible in the file",
              any(c["name"] == "Footer (roomy)" for c in
                  [r for r in sim.dump()["children"] if r["name"] == "Ember Roasters"][0]["children"]))

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
    tests = (test_mcp_client, test_bridge, test_figma_designer, test_web_builder,
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
