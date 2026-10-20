"""
core/node_runtime.py — finding and driving Node.js for the JavaScript skills.

Two callers need the same three things: is Node installed, does this code
parse, and what happens when it runs. `js_studio` writes and runs JavaScript;
`figma_designer` syntax-checks the Plugin API code a model wrote BEFORE it is
sent into somebody's open Figma file, because a syntax error caught here costs
a millisecond and one caught in Figma costs a round trip and a confusing toast.

Node is optional. Every function answers sensibly without it — `check_syntax`
reports "not checked" rather than pretending the code passed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

_CANDIDATES_WIN = (
    r"C:\Program Files\nodejs\node.exe",
    r"C:\Program Files (x86)\nodejs\node.exe",
)
_CANDIDATES_UNIX = ("/usr/local/bin/node", "/opt/homebrew/bin/node", "/usr/bin/node")

_cached_node: Optional[str] = None
_cached_checked = False


@dataclass
class RunResult:
    ok: bool
    code: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str = ""

    @property
    def output(self) -> str:
        parts = [self.stdout.strip(), self.stderr.strip()]
        return "\n".join(p for p in parts if p)


def find_node() -> Optional[str]:
    """Path to a node executable, or None. Cached after the first look."""
    global _cached_node, _cached_checked
    if _cached_checked:
        return _cached_node
    found = shutil.which("node")
    if not found:
        for cand in (_CANDIDATES_WIN if sys.platform == "win32" else _CANDIDATES_UNIX):
            if Path(cand).exists():
                found = cand
                break
    if not found:
        nvm = Path.home() / ".nvm" / "versions" / "node"
        if nvm.is_dir():
            versions = sorted(nvm.iterdir(), reverse=True)
            for v in versions:
                cand = v / "bin" / "node"
                if cand.exists():
                    found = str(cand)
                    break
    _cached_node, _cached_checked = found, True
    return found


def find_tool(name: str) -> Optional[str]:
    """npm / npx next to the node that was found, else on PATH."""
    node = find_node()
    if node:
        folder = Path(node).parent
        for suffix in ((".cmd", ".exe", "") if sys.platform == "win32" else ("",)):
            cand = folder / f"{name}{suffix}"
            if cand.exists():
                return str(cand)
    return shutil.which(name)


def node_version() -> str:
    node = find_node()
    if not node:
        return ""
    res = run([node, "--version"], timeout=10)
    return res.stdout.strip() if res.ok else ""


def run(args: Sequence[str], cwd: Optional[str] = None, timeout: float = 30.0,
        stdin: Optional[str] = None, env: Optional[dict] = None) -> RunResult:
    """Run a command and capture everything. Never raises."""
    full_env = dict(os.environ)
    full_env.setdefault("NO_COLOR", "1")
    full_env.setdefault("FORCE_COLOR", "0")
    if env:
        full_env.update(env)
    try:
        proc = subprocess.run(list(args), cwd=cwd, input=stdin, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=full_env)
        return RunResult(ok=proc.returncode == 0, code=proc.returncode,
                         stdout=proc.stdout or "", stderr=proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return RunResult(ok=False, code=None, stdout=out, stderr=err, timed_out=True,
                         error=f"timed out after {timeout:g}s")
    except FileNotFoundError as e:
        return RunResult(ok=False, code=None, stdout="", stderr="", error=f"not found: {e}")
    except Exception as e:
        return RunResult(ok=False, code=None, stdout="", stderr="",
                         error=f"{type(e).__name__}: {e}")


def check_syntax(code: str, module: bool = True,
                 wrap_async: bool = False) -> tuple[Optional[bool], str]:
    """(True, "") parses · (False, error) does not · (None, why) not checked.

    `wrap_async` checks code meant to run as the BODY of an async function —
    the shape of a Figma job, where a top-level `return` and `await` are legal.
    `module` picks ESM over CommonJS for whole files.
    """
    node = find_node()
    if not node:
        return None, "Node.js is not installed, so the syntax was not checked"
    source = f"(async function () {{\n{code}\n}});\n" if wrap_async else code
    suffix = ".mjs" if (module and not wrap_async) else ".cjs"
    fd, path = tempfile.mkstemp(prefix="jarvis_check_", suffix=suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(source)
        res = run([node, "--check", path], timeout=15)
        if res.ok:
            return True, ""
        if res.error:
            return None, res.error
        msg = res.stderr.strip().replace(path, "<code>")
        if wrap_async:
            msg = _shift_lines(msg, -1)
        return False, msg[-1500:]
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _shift_lines(message: str, delta: int) -> str:
    """Line numbers in a wrapped check refer to the wrapper; shift them back."""
    import re

    def repl(m: "re.Match") -> str:
        return f"<code>:{max(1, int(m.group(1)) + delta)}"

    return re.sub(r"<code>:(\d+)", repl, message)
