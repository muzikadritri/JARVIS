"""
core/figma_bridge.py — the live line between JARVIS and an open Figma file.

WHY THIS EXISTS NEXT TO FIGMA'S OWN MCP SERVER
    Figma's MCP server comes in two forms. The desktop one (127.0.0.1:3845)
    READS: screenshots, design context, metadata. Writing to the canvas —
    `use_figma` — is offered only by the remote server at mcp.figma.com, and
    that server accepts only the clients in Figma's MCP catalog. A desktop
    assistant of our own is not one of them, so the official write path is
    closed to it no matter how it authenticates.

    A Figma plugin is not restricted that way. Any Figma user can import one
    from a manifest on disk, it runs inside the open file with the full Plugin
    API, and it is allowed to talk to localhost. So `figma_plugin/` is a small
    plugin that asks this module for work, runs it, and posts back what
    happened. The work is the same thing `use_figma` takes — Plugin API
    JavaScript — so a design written for one runs on the other unchanged.

THE WIRE
    The plugin's UI iframe long-polls; nothing ever connects INTO Figma.

        GET  /status            who is this (the plugin's handshake)
        GET  /next?wait=20      the next job, or 204 when none arrived in time
        POST /result            {id, ok, result, error, logs, ms}
        POST /context           {fileName, fileKey, page, selection, user}

    There is deliberately no endpoint that SUBMITS code. Jobs are queued from
    inside this process only, so a web page on the same machine cannot make
    Figma run anything — at most it could poll for a job, and requests from a
    real web origin are refused anyway: the plugin iframe's origin is `null`,
    and every request carrying any other Origin is rejected, as is any Host
    header that is not this loopback address (DNS rebinding).

    Bodies are sent as text/plain by the plugin so the browser does not
    preflight them; OPTIONS is still answered, including the Private Network
    Access header Chromium asks for before it lets a page reach loopback.

WHY "localhost" AND TWO SOCKETS
    Figma's manifest takes host names only in `devAllowedDomains` — an entry
    like http://127.0.0.1:3846 is rejected as "not a valid URL" when the plugin
    is imported — so the plugin can only ever ask for http://localhost:3846.
    `localhost` resolves to ::1 before 127.0.0.1 on Windows and on many Linux
    setups. Chromium falls back from one to the other, but the bridge listens
    on both anyway, so nothing depends on that fallback.
"""
from __future__ import annotations

import errno
import json
import os
import socket
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

DEFAULT_PORT = 3846
BRIDGE_NAME = "jarvis-figma-bridge"
BRIDGE_VERSION = "1.0"

# A plugin that has not polled for this long is treated as gone. The poll
# itself waits up to 20 s, so the window is the poll plus generous slack.
_ALIVE_SECONDS = 30.0
_MAX_WAIT = 25.0


class BridgeError(Exception):
    pass


class _Server(ThreadingHTTPServer):
    """One owner per port. HTTPServer turns SO_REUSEADDR on, and on Windows
    that lets a SECOND process bind the same port — two JARVIS windows would
    then split the plugin's polls between them and each see half the jobs.
    Windows gets SO_EXCLUSIVEADDRUSE instead, so the second one is told the
    port is taken."""
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class _ServerV6(_Server):
    address_family = socket.AF_INET6


class _Job:
    __slots__ = ("id", "code", "description", "timeout_ms", "created",
                 "taken", "done", "result")

    def __init__(self, code: str, description: str, timeout_ms: int):
        self.id = uuid.uuid4().hex[:12]
        self.code = code
        self.description = description
        self.timeout_ms = int(timeout_ms)
        self.created = time.monotonic()
        self.taken = False
        self.done = threading.Event()
        self.result: dict = {}


class FigmaBridge:
    def __init__(self, port: int = DEFAULT_PORT):
        self.port = int(port)
        self._cond = threading.Condition()
        self._queue: deque[_Job] = deque()
        self._jobs: dict[str, _Job] = {}
        self._last_seen = 0.0
        self._context: dict = {}
        self._server: Optional[ThreadingHTTPServer] = None
        self._server_v6: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._start_error = ""

    # ── server lifecycle ─────────────────────────────────────────────────────
    def start(self) -> bool:
        if self._server is not None:
            return True
        bridge = self

        class Handler(_Handler):
            owner = bridge

        try:
            server = _Server(("127.0.0.1", self.port), Handler)
        except OSError as e:
            self._start_error = (f"port {self.port} is already in use or blocked "
                                 f"({e.strerror or e})")
            print(f"[FigmaBridge] cannot listen: {self._start_error}")
            return False
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever,
                                        name="figma-bridge", daemon=True)
        self._thread.start()
        self._start_error = ""
        # IPv6 loopback is a second door to the same bridge, not a requirement:
        # a machine with IPv6 switched off simply keeps the IPv4 one.
        try:
            v6 = _ServerV6(("::1", self.port), Handler)
            v6.daemon_threads = True
            self._server_v6 = v6
            threading.Thread(target=v6.serve_forever, name="figma-bridge-v6",
                             daemon=True).start()
        except OSError as e:
            if e.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)):
                # Something else answers on [::1]:port, and a browser trying
                # ::1 first would reach it instead of JARVIS. Say so plainly.
                print(f"[FigmaBridge] WARNING: [::1]:{self.port} belongs to another "
                      f"program — the Figma plugin may connect to it instead")
            else:
                print(f"[FigmaBridge] IPv6 loopback unavailable ({e.strerror or e}) — "
                      f"IPv4 only")
        print(f"[FigmaBridge] listening on http://localhost:{self.port}")
        return True

    def stop(self) -> None:
        for attr in ("_server", "_server_v6"):
            server = getattr(self, attr)
            setattr(self, attr, None)
            if server is not None:
                server.shutdown()
                server.server_close()
        with self._cond:
            for job in list(self._jobs.values()):
                if not job.done.is_set():
                    job.result = {"ok": False, "error": "the bridge was stopped"}
                    job.done.set()
            self._jobs.clear()
            self._queue.clear()
            self._cond.notify_all()

    @property
    def start_error(self) -> str:
        return self._start_error

    @property
    def running(self) -> bool:
        return self._server is not None

    # ── state the action reads ───────────────────────────────────────────────
    def connected(self) -> bool:
        """A plugin polled recently — or is busy running a job right now, when
        it does not poll and must not be mistaken for one that went away."""
        if not self.running:
            return False
        if (time.monotonic() - self._last_seen) < _ALIVE_SECONDS:
            return True
        with self._cond:
            return any(j.taken and not j.done.is_set() for j in self._jobs.values())

    def context(self) -> dict:
        with self._cond:
            return dict(self._context)

    def wait_for_plugin(self, seconds: float) -> bool:
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end:
            if self.connected():
                return True
            time.sleep(0.25)
        return self.connected()

    # ── running code in Figma ────────────────────────────────────────────────
    def run(self, code: str, description: str = "", timeout: float = 60.0) -> dict:
        """Queue Plugin API JavaScript and wait for the plugin's answer.

        Returns {ok, result, error, logs, ms}. Never raises for a failure in
        Figma — that is data for the caller, which usually hands the error
        back to the model that wrote the code so it can fix it. Raises
        BridgeError only when the job could not be delivered at all.
        """
        if not self.running and not self.start():
            raise BridgeError(f"the Figma bridge is not running: {self._start_error}")
        if not self.connected():
            raise BridgeError("the JARVIS plugin is not running in Figma")
        job = _Job(code, description, int(timeout * 1000))
        with self._cond:
            self._jobs[job.id] = job
            self._queue.append(job)
            self._cond.notify_all()
        # Slack beyond the plugin's own timeout covers the poll round trip.
        finished = job.done.wait(timeout + 15.0)
        with self._cond:
            self._jobs.pop(job.id, None)
            if not finished:
                try:
                    self._queue.remove(job)
                except ValueError:
                    pass
        if not finished:
            raise BridgeError("Figma did not answer in time — is the plugin "
                              "window still open?" if not job.taken else
                              "the code was still running in Figma when the "
                              "deadline passed")
        return job.result

    # ── called by the HTTP handler ───────────────────────────────────────────
    def _take(self, wait: float) -> Optional[_Job]:
        end = time.monotonic() + min(max(wait, 0.0), _MAX_WAIT)
        with self._cond:
            self._last_seen = time.monotonic()
            while True:
                while self._queue:
                    job = self._queue.popleft()
                    if job.id in self._jobs and not job.done.is_set():
                        job.taken = True
                        return job
                remaining = end - time.monotonic()
                if remaining <= 0 or self._server is None:
                    return None
                self._cond.wait(remaining)
                self._last_seen = time.monotonic()

    def _finish(self, payload: dict) -> bool:
        jid = str(payload.get("id") or "")
        with self._cond:
            self._last_seen = time.monotonic()
            job = self._jobs.get(jid)
        if job is None:
            return False
        job.result = {
            "ok": bool(payload.get("ok")),
            "result": payload.get("result"),
            "error": str(payload.get("error") or ""),
            "logs": list(payload.get("logs") or [])[:200],
            # Ids a failed job created before it threw — what cleanup removes.
            "created": [str(i) for i in (payload.get("created") or [])],
            "ms": payload.get("ms"),
        }
        job.done.set()
        return True

    def _set_context(self, payload: dict) -> None:
        with self._cond:
            self._last_seen = time.monotonic()
            self._context = {k: payload.get(k) for k in
                             ("fileName", "fileKey", "page", "pageId",
                              "selection", "user", "pluginVersion")}
            self._context["updated"] = time.time()


class _Handler(BaseHTTPRequestHandler):
    owner: FigmaBridge = None  # set on the subclass built in start()
    server_version = f"{BRIDGE_NAME}/{BRIDGE_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return  # the poll would fill the console twice a minute, forever

    # ── guards ───────────────────────────────────────────────────────────────
    def _allowed(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        port = self.owner.port
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
            self._send(421, {"ok": False, "error": "wrong host"})
            return False
        origin = self.headers.get("Origin")
        if origin not in (None, "null"):
            self._send(403, {"ok": False, "error": "origin not allowed"})
            return False
        return True

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")

    def _send(self, status: int, body: Optional[dict] = None) -> None:
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        if body is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        # 32 MB: a full-page PNG export travels back as base64 in a result.
        if length <= 0 or length > 32 * 1024 * 1024:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    # ── verbs ────────────────────────────────────────────────────────────────
    def do_OPTIONS(self) -> None:
        if not self._allowed():
            return
        self._send(204)

    def do_GET(self) -> None:
        if not self._allowed():
            return
        url = urlsplit(self.path)
        if url.path == "/status":
            self.owner._last_seen = time.monotonic()
            self._send(200, {"ok": True, "name": BRIDGE_NAME,
                             "version": BRIDGE_VERSION})
            return
        if url.path == "/next":
            try:
                wait = float(parse_qs(url.query).get("wait", ["20"])[0])
            except ValueError:
                wait = 20.0
            job = self.owner._take(wait)
            if job is None:
                self._send(204)
                return
            self._send(200, {"id": job.id, "code": job.code,
                             "description": job.description,
                             "timeoutMs": job.timeout_ms})
            return
        self._send(404, {"ok": False, "error": "unknown path"})

    def do_POST(self) -> None:
        if not self._allowed():
            return
        url = urlsplit(self.path)
        body = self._body()
        if url.path == "/result":
            known = self.owner._finish(body)
            self._send(200, {"ok": known})
            return
        if url.path == "/context":
            self.owner._set_context(body)
            self._send(200, {"ok": True})
            return
        self._send(404, {"ok": False, "error": "unknown path"})


_bridge: Optional[FigmaBridge] = None
_bridge_lock = threading.Lock()


def get_bridge(port: Optional[int] = None) -> FigmaBridge:
    """The process-wide bridge, started on first use."""
    global _bridge
    with _bridge_lock:
        if _bridge is None or (port and _bridge.port != int(port)):
            if _bridge is not None:
                _bridge.stop()
            _bridge = FigmaBridge(int(port or DEFAULT_PORT))
        _bridge.start()
        return _bridge
