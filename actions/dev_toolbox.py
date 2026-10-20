"""
actions/dev_toolbox.py — the small conversions a developer reaches for daily.

UUIDs, passwords, hashes, Base64, URL encoding, JSON formatting, timestamps,
JWT inspection, regex tests and colour conversion — each one exact, local and
instant, with the result put on the clipboard where it is meant to be pasted.

Passwords and other secrets are NEVER returned to the model: they go straight
to the clipboard and the tool answers only that it did. A secret that passes
through the conversation is spoken aloud, written to the log and kept in the
session history — three places a password must not be.
"""
from __future__ import annotations

import base64
import colorsys
import hashlib
import json
import re
import secrets
import string
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

from core import outcome

MAX_INPUT = 200_000
_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _clip(text: str) -> bool:
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception as e:
        print(f"[DevToolbox] clipboard unavailable: {e}")
        return False


def _show(player, title: str, text: str) -> None:
    if player and hasattr(player, "show_content"):
        try:
            player.show_content(title, text)
        except Exception:
            pass


def _input(params: dict) -> str:
    text = params.get("text")
    if text is None and params.get("file_path"):
        path = Path(str(params["file_path"])).expanduser()
        if not path.is_file():
            raise ValueError(f"{path} does not exist")
        return path.read_text(encoding="utf-8", errors="replace")[:MAX_INPUT]
    return str(text or "")[:MAX_INPUT]


def _password(params: dict) -> str:
    length = max(8, min(128, int(params.get("length") or 20)))
    use_symbols = str(params.get("symbols", "true")).lower() not in ("false", "0", "no")
    alphabet = string.ascii_letters + string.digits + ("!@#$%^&*()-_=+[]{}:,.?" if use_symbols else "")
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)
                and (not use_symbols or any(not c.isalnum() for c in pw))):
            break
    copied = _clip(pw)
    if not copied:
        return outcome.failed("dev_toolbox", "the clipboard is unavailable, and the password "
                              "is not read out for safety")
    return outcome.done("dev_toolbox", password="copied to clipboard (not shown)",
                        length=length, symbols=use_symbols)


def _hash(params: dict) -> str:
    algo = str(params.get("algorithm") or "sha256").lower().replace("-", "")
    if algo not in ("md5", "sha1", "sha256", "sha512", "sha3_256", "blake2b"):
        return outcome.failed("dev_toolbox", f"unsupported hash '{algo}'",
                              valid="md5,sha1,sha256,sha512,sha3_256,blake2b")
    h = hashlib.new(algo)
    if params.get("file_path") and params.get("text") is None:
        path = Path(str(params["file_path"])).expanduser()
        if not path.is_file():
            return outcome.failed("dev_toolbox", f"{path} does not exist")
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        source = path.name
    else:
        h.update(_input(params).encode("utf-8"))
        source = "text"
    digest = h.hexdigest()
    return outcome.done("dev_toolbox", algorithm=algo, source=source, hash=digest,
                        copied=_clip(digest))


def _base64(params: dict, decode: bool) -> str:
    text = _input(params)
    if decode:
        try:
            padded = text.strip() + "=" * (-len(text.strip()) % 4)
            out = base64.b64decode(padded, altchars=b"-_" if "-" in text or "_" in text else None)
            result = out.decode("utf-8")
        except UnicodeDecodeError:
            return outcome.failed("dev_toolbox", "the decoded data is binary, not text")
        except Exception as e:
            return outcome.failed("dev_toolbox", f"not valid Base64: {e}")
    else:
        result = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return outcome.done("dev_toolbox", mode="decode" if decode else "encode",
                        chars=len(result), copied=_clip(result)) + "\n" + \
        outcome.untrusted("tool", result[:4000])


def _json(params: dict, player) -> str:
    text = _input(params)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return outcome.failed("dev_toolbox", f"invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}")
    minify = str(params.get("mode") or "").lower() == "minify"
    out = json.dumps(data, separators=(",", ":"), ensure_ascii=False) if minify else \
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=bool(params.get("sort")))
    _show(player, "JSON", out[:4000])
    return outcome.done("dev_toolbox", valid=True, type=type(data).__name__,
                        chars=len(out), copied=_clip(out))


def _timestamp(params: dict) -> str:
    raw = str(params.get("text") or "").strip()
    if not raw or raw.lower() == "now":
        now = datetime.now(timezone.utc)
        return outcome.done("dev_toolbox", unix=int(now.timestamp()), iso_utc=now.isoformat(timespec="seconds"),
                            local=datetime.now().isoformat(timespec="seconds"))
    if re.fullmatch(r"-?\d{9,13}(\.\d+)?", raw):
        value = float(raw)
        if value > 1e11:
            value /= 1000
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
        return outcome.done("dev_toolbox", unix=raw, iso_utc=dt.isoformat(timespec="seconds"),
                            local=dt.astimezone().isoformat(timespec="seconds"))
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return outcome.failed("dev_toolbox", "give a Unix timestamp or an ISO date")
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return outcome.done("dev_toolbox", iso=raw, unix=int(dt.timestamp()),
                        copied=_clip(str(int(dt.timestamp()))))


def _jwt(params: dict) -> str:
    token = _input(params).strip()
    parts = token.split(".")
    if len(parts) != 3:
        return outcome.failed("dev_toolbox", "a JWT has three dot-separated parts")
    try:
        def part(p: str) -> dict:
            return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)).decode("utf-8"))
        header, payload = part(parts[0]), part(parts[1])
    except Exception as e:
        return outcome.failed("dev_toolbox", f"not a decodable JWT: {e}")
    facts = {"alg": header.get("alg"), "signature": "NOT verified"}
    for key in ("iss", "sub", "aud"):
        if key in payload:
            facts[key] = str(payload[key])[:80]
    if isinstance(payload.get("exp"), (int, float)):
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        facts["expires"] = exp.isoformat(timespec="seconds")
        facts["expired"] = exp < datetime.now(timezone.utc)
    return outcome.done("dev_toolbox", **facts) + "\n" + outcome.untrusted(
        "jwt", json.dumps({"header": header, "payload": payload}, indent=1)[:4000])


def _regex(params: dict) -> str:
    pattern = str(params.get("pattern") or "")
    text = _input(params)
    if not pattern:
        return outcome.ask("dev_toolbox", "Which regular expression should be tested?")
    flags = 0
    for ch in str(params.get("flags") or "").lower():
        flags |= {"i": re.I, "m": re.M, "s": re.S, "x": re.X}.get(ch, 0)
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return outcome.failed("dev_toolbox", f"invalid regex: {e}")
    matches = [m.group(0) for _, m in zip(range(50), rx.finditer(text))]
    return outcome.done("dev_toolbox", matches=len(matches), groups=rx.groups) + "\n" + \
        outcome.untrusted("tool", "\n".join(matches[:50]))


def _color(params: dict) -> str:
    raw = str(params.get("text") or "").strip()
    rgb = None
    m = _HEX_RE.match(raw)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    else:
        nums = [float(n) for n in re.findall(r"[\d.]+", raw)]
        if raw.lower().startswith("hsl") and len(nums) >= 3:
            r, g, b = colorsys.hls_to_rgb(nums[0] / 360, nums[2] / 100, nums[1] / 100)
            rgb = tuple(round(v * 255) for v in (r, g, b))
        elif len(nums) >= 3:
            rgb = tuple(max(0, min(255, round(n))) for n in nums[:3])
    if not rgb:
        return outcome.failed("dev_toolbox", "give a colour as #hex, rgb(r, g, b) or hsl(h, s%, l%)")
    hexv = "#{:02X}{:02X}{:02X}".format(*rgb)
    hue, light, sat = colorsys.rgb_to_hls(*(v / 255 for v in rgb))

    def lum(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    L = 0.2126 * lum(rgb[0]) + 0.7152 * lum(rgb[1]) + 0.0722 * lum(rgb[2])
    contrast_white = (1.05) / (L + 0.05)
    contrast_black = (L + 0.05) / 0.05
    return outcome.done("dev_toolbox", hex=hexv, rgb=f"rgb({rgb[0]}, {rgb[1]}, {rgb[2]})",
                        hsl=f"hsl({round(hue * 360)}, {round(sat * 100)}%, {round(light * 100)}%)",
                        contrast_on_white=round(contrast_white, 2),
                        contrast_on_black=round(contrast_black, 2),
                        best_text="black" if contrast_black > contrast_white else "white",
                        copied=_clip(hexv))


def _case(params: dict) -> str:
    text = _input(params)
    words = re.findall(r"[A-Za-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text))
    mode = str(params.get("mode") or "camel").lower()
    if not words:
        return outcome.failed("dev_toolbox", "there is no text to convert")
    lower = [w.lower() for w in words]
    out = {
        "camel": lower[0] + "".join(w.title() for w in lower[1:]),
        "pascal": "".join(w.title() for w in lower),
        "snake": "_".join(lower),
        "kebab": "-".join(lower),
        "constant": "_".join(w.upper() for w in lower),
        "title": " ".join(w.title() for w in lower),
    }.get(mode)
    if out is None:
        return outcome.failed("dev_toolbox", f"unknown case '{mode}'",
                              valid="camel,pascal,snake,kebab,constant,title")
    return outcome.done("dev_toolbox", mode=mode, result=out, copied=_clip(out))


def dev_toolbox(parameters: dict, player=None, **_ignored) -> str:
    params = parameters or {}
    action = str(params.get("action") or "").strip().lower()
    try:
        if action == "uuid":
            count = max(1, min(20, int(params.get("length") or 1)))
            ids = [str(uuid.uuid4()) for _ in range(count)]
            return outcome.done("dev_toolbox", uuids=" ".join(ids), copied=_clip("\n".join(ids)))
        if action == "password":
            return _password(params)
        if action == "hash":
            return _hash(params)
        if action in ("base64_encode", "base64"):
            return _base64(params, decode=False)
        if action == "base64_decode":
            return _base64(params, decode=True)
        if action == "url_encode":
            out = quote(_input(params), safe="")
            return outcome.done("dev_toolbox", result=out[:500], copied=_clip(out))
        if action == "url_decode":
            out = unquote(_input(params))
            return outcome.done("dev_toolbox", copied=_clip(out)) + "\n" + outcome.untrusted("tool", out[:4000])
        if action in ("json", "json_format"):
            return _json(params, player)
        if action == "timestamp":
            return _timestamp(params)
        if action == "jwt":
            return _jwt(params)
        if action == "regex":
            return _regex(params)
        if action == "color":
            return _color(params)
        if action == "case":
            return _case(params)
        return outcome.failed("dev_toolbox", f"unknown action '{action}'",
                              valid="uuid,password,hash,base64_encode,base64_decode,url_encode,"
                                    "url_decode,json,timestamp,jwt,regex,color,case")
    except ValueError as e:
        return outcome.failed("dev_toolbox", str(e))
    except Exception as e:
        print(f"[DevToolbox] {action} crashed: {type(e).__name__}: {e}")
        return outcome.failed("dev_toolbox", f"{action} failed: {e}")


TOOL = {
    "name": "dev_toolbox",
    "description": (
        "Developer utilities, exact and local; results go to the clipboard. "
        "Actions: uuid, password (copied to the clipboard, never shown), hash "
        "(md5/sha1/sha256/sha512 of text or a file), base64_encode/base64_decode, "
        "url_encode/url_decode, json (validate + format or minify), timestamp "
        "(Unix ↔ ISO, or 'now'), jwt (decode without verifying), regex (test a "
        "pattern), color (hex/rgb/hsl + contrast), case (camel/snake/kebab/…)."
    ),
    "brief": "Dev utilities: uuid, password, hash, base64, url, json, timestamp, jwt, regex, color, case.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "uuid | password | hash | base64_encode | base64_decode | "
                                                        "url_encode | url_decode | json | timestamp | jwt | regex | color | case"},
            "text": {"type": "STRING", "description": "The input text"},
            "file_path": {"type": "STRING", "description": "Read the input from this file instead"},
            "length": {"type": "INTEGER", "description": "password: length (default 20) · uuid: how many"},
            "symbols": {"type": "BOOLEAN", "description": "password: include symbols (default true)"},
            "algorithm": {"type": "STRING", "description": "hash: md5 | sha1 | sha256 | sha512"},
            "mode": {"type": "STRING", "description": "json: pretty|minify · case: camel|pascal|snake|kebab|constant|title"},
            "pattern": {"type": "STRING", "description": "regex: the pattern"},
            "flags": {"type": "STRING", "description": "regex: i, m, s, x"},
        },
        "required": ["action"],
    },
    "handler": dev_toolbox,
}
