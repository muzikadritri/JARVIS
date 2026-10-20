"""
actions/mcp_tools.py — any MCP server, as a tool JARVIS can use.

Every vendor that ships a Model Context Protocol server — Figma, GitHub,
Notion, Linear, a home server, a database — becomes reachable with one entry
in config/mcp_servers.json, added by voice with `action=add`. No code change
per service: the server describes its own tools, and this asks it to run one.

Results come back wrapped as untrusted content. An MCP tool returns whatever
the service holds — a document, an issue, a page — and none of that is the
user speaking.
"""
from __future__ import annotations

import json

from core import mcp_client
from core import outcome

MAX_RESULT_CHARS = 12_000


def _show(player, title: str, text: str) -> None:
    if player and hasattr(player, "show_content"):
        try:
            player.show_content(title, text)
        except Exception:
            pass


def _servers(params: dict, player) -> str:
    servers = mcp_client.load_servers()
    lines, facts = [], {}
    for name, cfg in sorted(servers.items()):
        if not cfg.get("enabled", True):
            lines.append(f"• {name}: disabled")
            continue
        ok, info = mcp_client.probe(name)
        lines.append(f"• {name}: {'online — ' + info if ok else 'offline — ' + info}")
        facts[name] = "online" if ok else "offline"
    _show(player, "MCP SERVERS", "\n".join(lines) or "none configured")
    return outcome.done("mcp_tools", servers=len(servers),
                        status=", ".join(f"{k}={v}" for k, v in facts.items()) or None)


def _tools(params: dict, player) -> str:
    server = str(params.get("server") or "").strip()
    if not server:
        return outcome.ask("mcp_tools", "Which MCP server's tools should be listed?")
    try:
        tools = mcp_client.get_client(server).list_tools()
    except mcp_client.MCPError as e:
        return outcome.failed("mcp_tools", str(e), server=server)
    lines = []
    for t in tools:
        desc = " ".join(str(t.get("description") or "").split())[:160]
        props = ", ".join(sorted(((t.get("inputSchema") or {}).get("properties") or {}).keys()))
        lines.append(f"- {t.get('name')}({props}): {desc}")
    text = "\n".join(lines)
    _show(player, f"MCP — {server}", text[:4000])
    return outcome.done("mcp_tools", server=server, tools=len(tools)) + "\n" + \
        outcome.untrusted("mcp", text[:MAX_RESULT_CHARS])


def _call(params: dict, player) -> str:
    server = str(params.get("server") or "").strip()
    tool = str(params.get("tool") or "").strip()
    if not server or not tool:
        return outcome.ask("mcp_tools", "Which server and which tool should be called?")
    raw = params.get("arguments")
    if isinstance(raw, dict):
        arguments = raw
    else:
        text = str(raw or "").strip()
        try:
            arguments = json.loads(text) if text else {}
        except json.JSONDecodeError as e:
            return outcome.failed("mcp_tools", f"arguments are not valid JSON: {e}")
        if not isinstance(arguments, dict):
            return outcome.failed("mcp_tools", "arguments must be a JSON object")
    try:
        res = mcp_client.get_client(server).call_tool(
            tool, arguments, timeout=float(params.get("timeout") or 60))
    except mcp_client.MCPError as e:
        return outcome.failed("mcp_tools", str(e), server=server, tool=tool)
    text = res.text or "(no text content)"
    _show(player, f"MCP — {tool}", text[:4000])
    head = (outcome.failed("mcp_tools", "the tool reported an error", server=server, tool=tool)
            if res.is_error else
            outcome.done("mcp_tools", server=server, tool=tool, images=len(res.images()) or None))
    return head + "\n" + outcome.untrusted("mcp", text[:MAX_RESULT_CHARS])


def _add(params: dict, player) -> str:
    name = str(params.get("server") or "").strip().lower().replace(" ", "_")
    url = str(params.get("url") or "").strip()
    if not name or not url:
        return outcome.ask("mcp_tools", "What should the server be called, and what is its URL?")
    if not url.startswith(("http://", "https://")):
        return outcome.failed("mcp_tools", "the URL must start with http:// or https://")
    mcp_client.save_server(name, url, token=str(params.get("token") or ""),
                           description=str(params.get("description") or ""))
    ok, info = mcp_client.probe(name)
    return outcome.done("mcp_tools", added=name, url=url, reachable=ok, server_info=info[:160])


def _remove(params: dict, player) -> str:
    name = str(params.get("server") or "").strip()
    if not name:
        return outcome.ask("mcp_tools", "Which MCP server should be removed?")
    existed = mcp_client.remove_server(name)
    if not existed:
        return outcome.failed("mcp_tools", f"no server named '{name}' is configured")
    return outcome.done("mcp_tools", removed=name)


def _resources(params: dict, player) -> str:
    server = str(params.get("server") or "").strip()
    if not server:
        return outcome.ask("mcp_tools", "Which server's resources should be listed?")
    uri = str(params.get("uri") or "").strip()
    try:
        client = mcp_client.get_client(server)
        if uri:
            text = client.read_resource(uri)
        else:
            text = "\n".join(f"- {r.get('uri')}: {r.get('name', '')} {r.get('description', '')}".strip()
                             for r in client.list_resources())
    except mcp_client.MCPError as e:
        return outcome.failed("mcp_tools", str(e), server=server)
    _show(player, f"MCP — {server}", text[:4000] or "(empty)")
    return outcome.done("mcp_tools", server=server, uri=uri or None) + "\n" + \
        outcome.untrusted("mcp", text[:MAX_RESULT_CHARS])


_ACTIONS = {"servers": _servers, "tools": _tools, "call": _call, "add": _add,
            "remove": _remove, "resources": _resources}


def mcp_tools(parameters: dict, player=None, **_ignored) -> str:
    params = parameters or {}
    action = str(params.get("action") or "servers").strip().lower()
    action = {"list": "servers", "status": "servers", "run": "call", "use": "call",
              "delete": "remove", "read": "resources"}.get(action, action)
    handler = _ACTIONS.get(action)
    if handler is None:
        return outcome.failed("mcp_tools", f"unknown action '{action}'",
                              valid=",".join(sorted(_ACTIONS)))
    try:
        return handler(params, player)
    except Exception as e:
        print(f"[MCPTools] {action} crashed: {type(e).__name__}: {e}")
        return outcome.failed("mcp_tools", f"{action} failed: {e}")


TOOL = {
    "name": "mcp_tools",
    "description": (
        "Uses Model Context Protocol (MCP) servers — external services that "
        "offer their own tools (Figma's desktop MCP, GitHub, Notion, databases, "
        "anything the user connected). Actions: 'servers' (which are configured "
        "and online), 'tools' (what a server offers), 'call' (run one of its "
        "tools with JSON arguments), 'resources' (list or read a resource uri), "
        "'add' (connect a new server by URL), 'remove'. List a server's tools "
        "before calling one you have not seen."
    ),
    "brief": "MCP servers: servers, tools, call, resources, add, remove.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "servers | tools | call | resources | add | remove"},
            "server": {"type": "STRING", "description": "Server name, e.g. 'figma'"},
            "tool": {"type": "STRING", "description": "call: the tool name"},
            "arguments": {"type": "STRING", "description": "call: the tool's arguments as a JSON object string"},
            "uri": {"type": "STRING", "description": "resources: a resource uri to read"},
            "url": {"type": "STRING", "description": "add: the server's Streamable HTTP URL"},
            "token": {"type": "STRING", "description": "add: optional bearer token"},
            "description": {"type": "STRING", "description": "add: what the server is for"},
            "timeout": {"type": "NUMBER", "description": "call: seconds (default 60)"},
        },
        "required": ["action"],
    },
    "handler": mcp_tools,
}
