"""
core/mcp_client.py — a Model Context Protocol client over Streamable HTTP.

WHAT THIS IS FOR
    MCP is how design tools, IDEs and services expose "tools" to an assistant.
    Figma's desktop app runs one at http://127.0.0.1:3845/mcp the moment its
    MCP server is switched on in Dev Mode, and most other vendors now ship one
    too. A single small client means every one of them is one entry in
    `config/mcp_servers.json` rather than one integration each.

    Stdlib only, on purpose: it has to work on a fresh clone, inside a frozen
    build and in the background thread of a tool call, and `urllib` is the one
    HTTP client that is guaranteed to be there in all three.

THE PROTOCOL, AS MUCH OF IT AS AN ASSISTANT NEEDS
    Every message is JSON-RPC 2.0, POSTed to ONE endpoint. The server answers
    either with plain JSON or with an SSE stream (`text/event-stream`) whose
    events carry JSON-RPC messages; the response to our request is the one with
    our `id`. Anything else on that stream is either a notification (ignored)
    or a request from the server (answered: `ping` with an empty result,
    everything else with "method not found", because this client offers the
    server no sampling, roots or elicitation).

        initialize            -> server info, capabilities, instructions
        notifications/initialized
        tools/list            -> paged with `cursor`
        tools/call            -> {content: [...], isError, structuredContent}
        resources/list, resources/read

    A server may hand out an `Mcp-Session-Id` during initialize. It goes on
    every later request, and a 404 on a request that carried one means the
    session expired: the client re-initialises once and retries.

WHY LOOPBACK BYPASSES THE PROXY
    `HTTP_PROXY` is commonly set machine-wide on managed Windows and macOS.
    urllib honours it, and a proxy cannot reach 127.0.0.1 on THIS machine — the
    result is a timeout that reads exactly like "Figma's MCP server is off".
    Loopback addresses therefore go out with no proxy at all, and `localhost`
    is normalised to 127.0.0.1 because Windows resolves it to ::1 first while
    the servers bind IPv4 only.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "JARVIS"
CLIENT_VERSION = "schleimy-1.0"

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent
SERVERS_PATH = _BASE / "config" / "mcp_servers.json"

# Shipped defaults. A server listed in the user's file with the same name wins,
# so these are only what a fresh install starts from.
DEFAULT_SERVERS: dict[str, dict] = {
    "figma": {
        "url": "http://127.0.0.1:3845/mcp",
        "token": "",
        "headers": {},
        "enabled": True,
        "description": "Figma desktop app MCP server (Dev Mode → enable MCP "
                       "server). Reads designs: screenshots, design context, "
                       "metadata, variables.",
    },
}

_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


class MCPError(Exception):
    """Anything that stopped a request from producing a result."""


class MCPAuthError(MCPError):
    """The server wants credentials this client does not have."""


class MCPUnavailable(MCPError):
    """Nothing answered at the address — the server is off or unreachable."""


@dataclass
class ToolResult:
    content: list[dict] = field(default_factory=list)
    is_error: bool = False
    structured: Any = None
    raw: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        """All text parts joined. Images and resources are summarised by type,
        because the caller of a voice tool can do nothing with base64."""
        out = []
        for part in self.content:
            kind = part.get("type")
            if kind == "text":
                out.append(str(part.get("text", "")))
            elif kind == "resource":
                res = part.get("resource") or {}
                if "text" in res:
                    out.append(str(res.get("text", "")))
                else:
                    out.append(f"[resource {res.get('uri', '')}]")
            elif kind == "resource_link":
                out.append(f"[resource {part.get('uri', '')}]")
            elif kind in ("image", "audio"):
                out.append(f"[{kind} {part.get('mimeType', '')}]")
        if not out and self.structured is not None:
            out.append(json.dumps(self.structured, ensure_ascii=False)[:8000])
        return "\n".join(t for t in out if t).strip()

    def images(self) -> list[tuple[str, str]]:
        """(mime_type, base64_data) for every inline image part."""
        return [(p.get("mimeType", "image/png"), p.get("data", ""))
                for p in self.content
                if p.get("type") == "image" and p.get("data")]


def _normalise_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise MCPError("no server URL configured")
    if "://" not in url:
        url = "http://" + url
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if host == "localhost":
        netloc = "127.0.0.1" + (f":{parts.port}" if parts.port else "")
        parts = parts._replace(netloc=netloc)
    return urllib.parse.urlunsplit(parts)


def _is_loopback(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in _LOOPBACK or host.startswith("127.")


def _iter_sse(resp, deadline: float):
    """JSON payloads of the `data:` events on an SSE response.

    Events end at a blank line and `data:` may span several lines, which are
    joined with newlines per the SSE spec. Comment lines (`:` — keep-alives)
    and other fields are skipped. The deadline is checked between lines: a
    server that keeps the stream open but never answers our id must not hold a
    tool call forever.
    """
    data_lines: list[str] = []
    while True:
        if time.monotonic() > deadline:
            raise MCPError("the server did not answer before the deadline")
        raw = resp.readline()
        if not raw:
            if data_lines:
                yield "\n".join(data_lines)
            return
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "data":
            data_lines.append(value)


class MCPClient:
    """One server, one session. Thread-safe: calls are serialised by a lock,
    because a voice assistant never needs two calls in flight to the same
    server and the session id must not race."""

    def __init__(self, url: str, token: str = "", headers: Optional[dict] = None,
                 timeout: float = 30.0, name: str = "server"):
        self.url = _normalise_url(url)
        self.name = name
        self.token = (token or "").strip()
        self.extra_headers = dict(headers or {})
        self.timeout = float(timeout)
        self.session_id: Optional[str] = None
        self.protocol_version = PROTOCOL_VERSION
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self.instructions: str = ""
        self._initialized = False
        self._next_id = 0
        self._lock = threading.RLock()
        handlers = []
        if _is_loopback(self.url):
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)

    # ── transport ────────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": f"{CLIENT_NAME}/{CLIENT_VERSION}",
        }
        if self._initialized:
            h["MCP-Protocol-Version"] = self.protocol_version
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        h.update({str(k): str(v) for k, v in self.extra_headers.items()})
        return h

    def _post(self, message: dict, timeout: Optional[float] = None):
        body = json.dumps(message).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers=self._headers())
        try:
            return self._opener.open(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            if e.code in (401, 403):
                hint = e.headers.get("WWW-Authenticate", "") if e.headers else ""
                raise MCPAuthError(
                    f"{self.name}: the server refused access (HTTP {e.code}). "
                    f"{'It asks for: ' + hint if hint else 'A token is required.'}"
                ) from e
            if e.code == 404 and self.session_id:
                raise _SessionExpired() from e
            raise MCPError(f"{self.name}: HTTP {e.code} {detail}".strip()) from e
        except urllib.error.URLError as e:
            raise MCPUnavailable(
                f"{self.name}: nothing answered at {self.url} ({e.reason})") from e
        except (TimeoutError, OSError) as e:
            raise MCPUnavailable(f"{self.name}: {type(e).__name__}: {e}") from e

    def _answer_server_request(self, msg: dict) -> None:
        """A request the server sent us mid-stream. `ping` is the only one this
        client supports; the rest are refused so the server does not wait."""
        if msg.get("method") == "ping":
            reply = {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}
        else:
            reply = {"jsonrpc": "2.0", "id": msg.get("id"),
                     "error": {"code": -32601,
                               "message": f"{CLIENT_NAME} does not support "
                                          f"{msg.get('method')}"}}
        try:
            resp = self._post(reply, timeout=10)
            resp.close()
        except Exception as e:
            print(f"[MCP] {self.name}: could not answer server request: {e}")

    def _rpc_once(self, method: str, params: Optional[dict],
                  timeout: Optional[float]) -> Any:
        self._next_id += 1
        rid = self._next_id
        message = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            message["params"] = params
        limit = timeout or self.timeout
        deadline = time.monotonic() + limit
        resp = self._post(message, timeout=limit)
        try:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" in ctype:
                for payload in _iter_sse(resp, deadline):
                    try:
                        msg = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    for item in (msg if isinstance(msg, list) else [msg]):
                        if not isinstance(item, dict):
                            continue
                        if item.get("id") == rid and ("result" in item or "error" in item):
                            return _unwrap(item, self.name, method)
                        if "method" in item and "id" in item:
                            self._answer_server_request(item)
                raise MCPError(f"{self.name}: the stream ended without an answer "
                               f"to {method}")
            raw = resp.read()
            if not raw.strip():
                raise MCPError(f"{self.name}: empty answer to {method}")
            try:
                msg = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as e:
                raise MCPError(f"{self.name}: answer to {method} is not JSON") from e
            for item in (msg if isinstance(msg, list) else [msg]):
                if isinstance(item, dict) and item.get("id") == rid:
                    return _unwrap(item, self.name, method)
            raise MCPError(f"{self.name}: no answer with id {rid} to {method}")
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        resp = self._post(message, timeout=10)
        resp.close()

    def _rpc(self, method: str, params: Optional[dict] = None,
             timeout: Optional[float] = None) -> Any:
        with self._lock:
            if not self._initialized and method != "initialize":
                self._initialize_locked()
            try:
                return self._rpc_once(method, params, timeout)
            except _SessionExpired:
                print(f"[MCP] {self.name}: session expired — reconnecting")
                self.session_id = None
                self._initialized = False
                self._initialize_locked()
                return self._rpc_once(method, params, timeout)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def _initialize_locked(self) -> dict:
        self._initialized = False
        result = self._rpc_once("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        }, timeout=min(self.timeout, 15.0))
        if not isinstance(result, dict):
            raise MCPError(f"{self.name}: initialize returned no result")
        self.protocol_version = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        self.instructions = str(result.get("instructions") or "")
        self._initialized = True
        try:
            self._notify("notifications/initialized")
        except MCPError as e:
            # Some servers answer the notification with an error status while
            # working perfectly afterwards; it is not worth failing over.
            print(f"[MCP] {self.name}: initialized notification: {e}")
        print(f"[MCP] {self.name}: connected to "
              f"{self.server_info.get('name', '?')} {self.server_info.get('version', '')}"
              f" (protocol {self.protocol_version})")
        return result

    def initialize(self) -> dict:
        with self._lock:
            return self._initialize_locked()

    def close(self) -> None:
        """End the session politely. A server that does not support DELETE
        answers 405, which is fine — the session simply times out there."""
        with self._lock:
            if not self.session_id:
                self._initialized = False
                return
            req = urllib.request.Request(self.url, method="DELETE",
                                         headers=self._headers())
            try:
                self._opener.open(req, timeout=5).close()
            except Exception:
                pass
            self.session_id = None
            self._initialized = False

    # ── the calls an assistant makes ─────────────────────────────────────────
    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        for _ in range(50):          # a hard stop on a server that pages forever
            params = {"cursor": cursor} if cursor else {}
            result = self._rpc("tools/list", params) or {}
            tools.extend(t for t in result.get("tools", []) if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: Optional[dict] = None,
                  timeout: Optional[float] = None) -> ToolResult:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}},
                           timeout=timeout) or {}
        return ToolResult(content=list(result.get("content") or []),
                          is_error=bool(result.get("isError")),
                          structured=result.get("structuredContent"),
                          raw=result)

    def list_resources(self) -> list[dict]:
        out: list[dict] = []
        cursor = None
        for _ in range(50):
            params = {"cursor": cursor} if cursor else {}
            result = self._rpc("resources/list", params) or {}
            out.extend(r for r in result.get("resources", []) if isinstance(r, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return out

    def read_resource(self, uri: str) -> str:
        result = self._rpc("resources/read", {"uri": uri}) or {}
        parts = []
        for c in result.get("contents") or []:
            if "text" in c:
                parts.append(str(c["text"]))
            elif "blob" in c:
                parts.append(f"[binary {c.get('mimeType', '')} {len(c['blob'])} chars base64]")
        return "\n".join(parts)


class _SessionExpired(Exception):
    pass


def _unwrap(msg: dict, server: str, method: str) -> Any:
    if "error" in msg and msg["error"]:
        err = msg["error"]
        code = err.get("code") if isinstance(err, dict) else None
        text = err.get("message") if isinstance(err, dict) else str(err)
        raise MCPError(f"{server}: {method} failed ({code}): {text}")
    return msg.get("result")


# ─────────────────────────────────────────────────────────────────────────────
#  Configured servers
# ─────────────────────────────────────────────────────────────────────────────
_clients: dict[str, MCPClient] = {}
_clients_lock = threading.Lock()


def load_servers() -> dict[str, dict]:
    """Shipped defaults overlaid with `config/mcp_servers.json`.

    The file is `{"servers": {"name": {"url": ..., "token": ..., "headers":
    {...}, "enabled": true, "description": ...}}}`. A malformed file is
    reported and ignored rather than allowed to stop every MCP tool loading.
    """
    servers = {k: dict(v) for k, v in DEFAULT_SERVERS.items()}
    try:
        if SERVERS_PATH.exists():
            data = json.loads(SERVERS_PATH.read_text(encoding="utf-8"))
            for name, cfg in (data.get("servers") or {}).items():
                if isinstance(cfg, dict):
                    merged = dict(servers.get(name, {}))
                    merged.update(cfg)
                    servers[str(name)] = merged
    except Exception as e:
        print(f"[MCP] {SERVERS_PATH.name} could not be read: {e}")
    return servers


def save_server(name: str, url: str, token: str = "", description: str = "",
                headers: Optional[dict] = None, enabled: bool = True) -> None:
    """Add or replace one server in the user's file, keeping the others."""
    data: dict = {"servers": {}}
    try:
        if SERVERS_PATH.exists():
            data = json.loads(SERVERS_PATH.read_text(encoding="utf-8")) or {"servers": {}}
    except Exception:
        data = {"servers": {}}
    data.setdefault("servers", {})
    data["servers"][name] = {"url": url, "token": token, "headers": headers or {},
                             "enabled": bool(enabled), "description": description}
    SERVERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SERVERS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SERVERS_PATH)
    drop_client(name)


def remove_server(name: str) -> bool:
    """Remove a server from the user's file. A shipped default is disabled
    instead, because deleting it from the file would only bring it back."""
    try:
        data = json.loads(SERVERS_PATH.read_text(encoding="utf-8")) if SERVERS_PATH.exists() else {}
    except Exception:
        data = {}
    servers = data.setdefault("servers", {})
    existed = name in servers or name in DEFAULT_SERVERS
    if name in DEFAULT_SERVERS:
        entry = dict(DEFAULT_SERVERS[name])
        entry.update(servers.get(name, {}))
        entry["enabled"] = False
        servers[name] = entry
    else:
        servers.pop(name, None)
    SERVERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SERVERS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    drop_client(name)
    return existed


def get_client(name: str) -> MCPClient:
    """A connected client for a configured server, reused across calls."""
    servers = load_servers()
    cfg = servers.get(name)
    if not cfg:
        raise MCPError(f"no MCP server named '{name}' is configured "
                       f"(known: {', '.join(sorted(servers)) or 'none'})")
    if not cfg.get("enabled", True):
        raise MCPError(f"the MCP server '{name}' is disabled")
    with _clients_lock:
        client = _clients.get(name)
        if (client is None or client.url != _normalise_url(cfg.get("url", ""))
                or client.token != (cfg.get("token") or "").strip()):
            client = MCPClient(cfg.get("url", ""), token=cfg.get("token", ""),
                               headers=cfg.get("headers") or {},
                               timeout=float(cfg.get("timeout", 30)), name=name)
            _clients[name] = client
    return client


def drop_client(name: str) -> None:
    with _clients_lock:
        client = _clients.pop(name, None)
    if client:
        try:
            client.close()
        except Exception:
            pass


def probe(name: str) -> tuple[bool, str]:
    """(reachable, one-line description) — never raises."""
    try:
        client = get_client(name)
        with client._lock:
            if not client._initialized:
                client._initialize_locked()
        info = client.server_info
        return True, f"{info.get('name', name)} {info.get('version', '')}".strip()
    except MCPError as e:
        return False, str(e)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
