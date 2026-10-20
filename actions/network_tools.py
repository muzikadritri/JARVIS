"""
actions/network_tools.py — "is it me, or is it the internet?"

The questions people actually ask an assistant about their network, each
answered by measuring rather than guessing:

    ping       reachability and round-trip time (system ping; TCP fallback
               where ICMP is not allowed)
    dns        what a name resolves to, IPv4 and IPv6, and how long it took
    port       whether one service answers — a TCP connect with a deadline
    http       status code, timing, redirects, server and TLS expiry
    public_ip  the address the internet sees
    local      this machine's interfaces and addresses
    diagnose   gateway → DNS → internet → HTTPS in order, naming the first
               link that breaks

Port checks are limited to an explicit, short list on one host: this answers
"is my server up", it is not a scanner.
"""
from __future__ import annotations

import ipaddress
import platform
import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

from core import outcome

_SYSTEM = platform.system()
MAX_PORTS = 12
DEFAULT_TIMEOUT = 5.0
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]_]{1,253}$")


def _clean_host(value: str) -> str:
    text = str(value or "").strip()
    if "://" in text:
        text = urlsplit(text).hostname or ""
    text = text.strip("/").strip()
    if not text or not _HOST_RE.match(text) or text.startswith("-"):
        return ""
    return text


def _tcp_connect(host: str, port: int, timeout: float) -> tuple[bool, float, str]:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, (time.perf_counter() - started) * 1000, ""
    except socket.timeout:
        return False, timeout * 1000, "timed out"
    except OSError as e:
        return False, (time.perf_counter() - started) * 1000, e.strerror or str(e)


def _ping(params: dict) -> str:
    host = _clean_host(params.get("host") or "1.1.1.1")
    if not host:
        return outcome.failed("network_tools", "that is not a valid host name or address")
    count = max(1, min(10, int(params.get("count") or 4)))
    flag = "-n" if _SYSTEM == "Windows" else "-c"
    args = ["ping", flag, str(count), host]
    if _SYSTEM != "Windows":
        args[1:1] = ["-W", "3"] if _SYSTEM == "Linux" else []
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=count * 4 + 6,
                              errors="replace")
        out = proc.stdout + proc.stderr
        times = [float(t.replace(",", ".")) for t in re.findall(r"(?:time|Zeit)[=<]\s*([\d.,]+)\s*ms", out, re.I)]
        if times:
            loss = round(100 * (1 - len(times) / count))
            return outcome.done("network_tools", host=host, replies=len(times), sent=count,
                                loss_pct=loss, avg_ms=round(sum(times) / len(times), 1),
                                min_ms=round(min(times), 1), max_ms=round(max(times), 1))
        if proc.returncode != 0 and "permission" not in out.lower():
            return outcome.failed("network_tools", f"{host} did not answer ping", host=host,
                                  sent=count, replies=0)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    # ICMP unavailable (no ping binary, no permission): TCP 443 then 80.
    for port in (443, 80):
        ok, ms, err = _tcp_connect(host, port, DEFAULT_TIMEOUT)
        if ok:
            return outcome.done("network_tools", host=host, method=f"tcp:{port}",
                                avg_ms=round(ms, 1), note="ICMP ping unavailable, used TCP")
    return outcome.failed("network_tools", f"{host} is not reachable", host=host)


def _dns(params: dict) -> str:
    host = _clean_host(params.get("host") or "")
    if not host:
        return outcome.ask("network_tools", "Which domain should be looked up?")
    started = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return outcome.failed("network_tools", f"{host} does not resolve: {e.strerror or e}",
                              host=host)
    ms = (time.perf_counter() - started) * 1000
    v4 = sorted({i[4][0] for i in infos if i[0] == socket.AF_INET})
    v6 = sorted({i[4][0] for i in infos if i[0] == socket.AF_INET6})
    reverse = None
    if v4:
        try:
            reverse = socket.gethostbyaddr(v4[0])[0]
        except OSError:
            reverse = None
    return outcome.done("network_tools", host=host, ipv4=",".join(v4[:6]) or None,
                        ipv6=",".join(v6[:4]) or None, resolve_ms=round(ms, 1),
                        reverse=reverse)


def _ports(params: dict) -> list[int]:
    raw = str(params.get("port") or params.get("ports") or "").replace(";", ",")
    ports = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and 0 < int(part) < 65536:
            ports.append(int(part))
    return list(dict.fromkeys(ports))[:MAX_PORTS]


def _port(params: dict) -> str:
    host = _clean_host(params.get("host") or "")
    ports = _ports(params)
    if not host or not ports:
        return outcome.ask("network_tools", "Which host and which port (or a few, comma-separated)?")
    results = []
    for port in ports:
        ok, ms, err = _tcp_connect(host, port, float(params.get("timeout") or DEFAULT_TIMEOUT))
        results.append(f"{port}:{'open' if ok else 'closed'}"
                       + (f"({round(ms)}ms)" if ok else f"({err})"))
    any_open = any(":open" in r for r in results)
    return outcome.done("network_tools", host=host, ports=" ".join(results), any_open=any_open)


def _tls_expiry(host: str, port: int = 443) -> tuple[str, int]:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=DEFAULT_TIMEOUT) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
    expires = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    days = (expires - datetime.now(timezone.utc)).days
    return expires.date().isoformat(), days


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http(params: dict) -> str:
    url = str(params.get("url") or params.get("host") or "").strip()
    if not url:
        return outcome.ask("network_tools", "Which website or URL should be checked?")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return outcome.failed("network_tools", "only http and https addresses can be checked")
    opener = urllib.request.build_opener(_NoRedirect)
    chain, current, status, server, elapsed = [], url, None, None, 0.0
    for _ in range(6):
        req = urllib.request.Request(current, method="GET",
                                     headers={"User-Agent": "JARVIS-network-check/1.0"})
        started = time.perf_counter()
        try:
            resp = opener.open(req, timeout=float(params.get("timeout") or 10))
            status, server = resp.status, resp.headers.get("Server")
            resp.read(2048)
            resp.close()
            elapsed = (time.perf_counter() - started) * 1000
            break
        except urllib.error.HTTPError as e:
            elapsed = (time.perf_counter() - started) * 1000
            status, server = e.code, e.headers.get("Server") if e.headers else None
            location = e.headers.get("Location") if e.headers else None
            if e.code in (301, 302, 303, 307, 308) and location:
                chain.append(f"{e.code}->{location}")
                current = urllib.request.urljoin(current, location)
                continue
            break
        except urllib.error.URLError as e:
            return outcome.failed("network_tools", f"{parts.hostname} did not answer: {e.reason}",
                                  url=url)
        except (socket.timeout, TimeoutError):
            return outcome.failed("network_tools", f"{parts.hostname} timed out", url=url)
    facts = {"url": url, "status": status, "ms": round(elapsed), "server": server,
             "redirects": " | ".join(chain) or None, "final": current if chain else None}
    final = urlsplit(current)
    if final.scheme == "https" and final.hostname:
        try:
            expires, days = _tls_expiry(final.hostname, final.port or 443)
            facts["tls_expires"] = expires
            facts["tls_days_left"] = days
        except Exception as e:
            facts["tls"] = f"could not read certificate: {e}"
    if status and status < 400:
        return outcome.done("network_tools", **facts)
    return outcome.failed("network_tools", f"the site answered with HTTP {status}", **facts)


def _public_ip(params: dict) -> str:
    for endpoint in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            with urllib.request.urlopen(endpoint, timeout=6) as resp:
                text = resp.read(64).decode("ascii", "replace").strip()
            ipaddress.ip_address(text)
            return outcome.done("network_tools", public_ip=text, source=urlsplit(endpoint).hostname)
        except Exception:
            continue
    return outcome.failed("network_tools", "no IP echo service answered — the internet "
                          "connection may be down")


def _local(params: dict) -> str:
    rows = []
    try:
        import psutil
        stats = psutil.net_if_stats()
        for name, addrs in psutil.net_if_addrs().items():
            if name in stats and not stats[name].isup:
                continue
            ips = [a.address for a in addrs if a.family in (socket.AF_INET, socket.AF_INET6)
                   and not a.address.startswith(("127.", "::1", "fe80"))]
            if ips:
                rows.append(f"{name}={'/'.join(ips[:3])}")
    except Exception:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 80))       # TEST-NET: nothing is sent
        primary = probe.getsockname()[0]
        probe.close()
    except OSError:
        primary = None
    return outcome.done("network_tools", hostname=socket.gethostname(), primary_ip=primary,
                        interfaces=" ".join(rows[:8]) or None)


def _gateway() -> str:
    try:
        if _SYSTEM == "Windows":
            out = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=8,
                                 errors="replace").stdout
            m = re.findall(r"(?:Default Gateway|Standardgateway)[ .]*:\s*([\d.]+)", out)
            return m[0] if m else ""
        if _SYSTEM == "Darwin":
            out = subprocess.run(["route", "-n", "get", "default"], capture_output=True,
                                 text=True, timeout=8).stdout
            m = re.search(r"gateway:\s*([\d.]+)", out)
            return m.group(1) if m else ""
        out = subprocess.run(["ip", "route", "show", "default"], capture_output=True,
                             text=True, timeout=8).stdout
        m = re.search(r"default via ([\d.]+)", out)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _diagnose(params: dict) -> str:
    steps = []
    gw = _gateway()
    if gw:
        ok = any(_tcp_connect(gw, p, 2.0)[0] for p in (80, 443, 53))
        steps.append(f"gateway {gw}: {'answers' if ok else 'no TCP answer (may still route)'}")
    else:
        steps.append("gateway: not found")
    try:
        socket.getaddrinfo("example.com", 443)
        dns_ok = True
        steps.append("dns: ok")
    except socket.gaierror as e:
        dns_ok = False
        steps.append(f"dns: FAILED ({e.strerror})")
    net_ok, ms, _ = _tcp_connect("1.1.1.1", 443, DEFAULT_TIMEOUT)
    steps.append(f"internet (1.1.1.1:443): {'ok ' + str(round(ms)) + 'ms' if net_ok else 'FAILED'}")
    https_ok = False
    if dns_ok and net_ok:
        try:
            with urllib.request.urlopen("https://www.google.com/generate_204", timeout=8) as r:
                https_ok = r.status in (200, 204)
        except Exception:
            https_ok = False
        steps.append(f"https: {'ok' if https_ok else 'FAILED (proxy, firewall or captive portal?)'}")
    if not net_ok:
        verdict = "no internet connection — the problem is the local network or the provider"
    elif not dns_ok:
        verdict = "the internet is reachable but name resolution fails — a DNS problem"
    elif not https_ok:
        verdict = "connected, but web traffic is blocked or intercepted"
    else:
        verdict = "the connection is healthy"
    return outcome.done("network_tools", verdict=verdict, steps=" | ".join(steps))


_ACTIONS = {"ping": _ping, "dns": _dns, "port": _port, "http": _http,
            "public_ip": _public_ip, "local": _local, "diagnose": _diagnose}


def network_tools(parameters: dict, player=None, **_ignored) -> str:
    params = parameters or {}
    action = str(params.get("action") or "diagnose").strip().lower()
    action = {"ip": "public_ip", "myip": "public_ip", "website": "http", "site": "http",
              "lookup": "dns", "check": "diagnose", "interfaces": "local"}.get(action, action)
    handler = _ACTIONS.get(action)
    if handler is None:
        return outcome.failed("network_tools", f"unknown action '{action}'",
                              valid=",".join(sorted(_ACTIONS)))
    try:
        return handler(params)
    except Exception as e:
        print(f"[Network] {action} crashed: {type(e).__name__}: {e}")
        return outcome.failed("network_tools", f"{action} failed: {e}")


TOOL = {
    "name": "network_tools",
    "description": (
        "Network diagnostics, measured: 'diagnose' (why is the internet not "
        "working — gateway, DNS, connectivity, HTTPS), 'ping' a host, 'dns' "
        "lookup, 'port' (is a service answering on a few ports), 'http' (is a "
        "website up — status, speed, redirects, certificate expiry), 'public_ip', "
        "'local' (this machine's addresses)."
    ),
    "brief": "Network: diagnose, ping, dns, port, http, public_ip, local.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "diagnose | ping | dns | port | http | public_ip | local"},
            "host": {"type": "STRING", "description": "Host name or IP address"},
            "url": {"type": "STRING", "description": "http: the website or URL"},
            "port": {"type": "STRING", "description": "port: one port or a comma-separated few"},
            "count": {"type": "INTEGER", "description": "ping: number of probes (default 4)"},
            "timeout": {"type": "NUMBER", "description": "Seconds per probe"},
        },
        "required": ["action"],
    },
    "handler": network_tools,
}
