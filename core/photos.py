"""
core/photos.py — real photographs for designs, found by search, no API key.

A design with gradient rectangles where the photos belong reads as a
wireframe, however good the type and spacing are. Real imagery is the single
biggest step from "template" to "website", so the planner names what each
image should show and this module finds it:

    1. Openverse      (api.openverse.org) — openly licensed images from Flickr,
                      Wikimedia and museums, searchable without a key
    2. Wikimedia      (commons.wikimedia.org) — the fallback when Openverse is
       Commons        rate-limited or finds nothing
    3. nothing        — the design keeps its gradient placeholder for that slot.
                      A missing photo is never a reason for a design to fail.

Figma cannot fetch anything itself (the plugin has no network access beyond
JARVIS), so the bytes are downloaded HERE, downscaled to at most 1600 px as a
JPEG, and travel into the job as base64. Every photo is cached under
~/.jarvis/photos with its attribution, so the same query costs nothing twice
and the credits can be shown to the user, who decides how to use the images.
"""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import io
import json
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

CACHE_DIR = Path.home() / ".jarvis" / "photos"
USER_AGENT = "JARVIS-SchleimyEdition/1.0 (+https://github.com/muzikadritri/JARVIS)"
OPENVERSE = "https://api.openverse.org/v1/images/"
COMMONS = "https://commons.wikimedia.org/w/api.php"
MAX_DOWNLOAD = 12 * 1024 * 1024
MAX_PX = 1600
TIMEOUT = 8.0
_ASPECT = {"landscape": "wide", "square": "square", "portrait": "tall"}


@dataclass
class Photo:
    query: str
    width: int
    height: int
    title: str
    creator: str
    license: str
    source: str
    attribution: str
    path: str = ""

    def b64(self) -> str:
        return base64.b64encode(Path(self.path).read_bytes()).decode("ascii")


def _get(url: str, timeout: float = TIMEOUT, limit: int = MAX_DOWNLOAD) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json, image/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file too large")
    return data


def _fits(width: int, height: int, orientation: str) -> bool:
    if not width or not height:
        return True
    ratio = width / height
    return {"landscape": ratio >= 1.2, "square": 0.8 <= ratio <= 1.25,
            "portrait": ratio <= 0.85}.get(orientation, True)


def _shrink(data: bytes) -> tuple[bytes, int, int]:
    """JPEG, longest side at most MAX_PX. Without Pillow the original is kept
    when it is small enough to send, and refused when it is not."""
    try:
        from PIL import Image
    except ImportError:
        if len(data) > 1_500_000:
            raise ValueError("image too large to send without Pillow")
        return data, 0, 0
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail((MAX_PX, MAX_PX))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=82, optimize=True, progressive=True)
        return out.getvalue(), img.width, img.height


def _cache_key(query: str, orientation: str) -> str:
    return hashlib.sha1(f"{query.lower().strip()}|{orientation}".encode("utf-8")).hexdigest()[:20]


def _from_cache(key: str) -> Optional[Photo]:
    meta = CACHE_DIR / f"{key}.json"
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
        photo = Photo(**data)
        if Path(photo.path).is_file():
            return photo
    except Exception:
        pass
    return None


def _store(key: str, image: bytes, photo: Photo) -> Photo:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{key}.jpg"
    path.write_bytes(image)
    photo.path = str(path)
    (CACHE_DIR / f"{key}.json").write_text(json.dumps(asdict(photo), ensure_ascii=False),
                                           encoding="utf-8")
    return photo


def _openverse(query: str, orientation: str) -> list[dict]:
    params = {"q": query, "page_size": "12", "mature": "false",
              "license_type": "commercial", "aspect_ratio": _ASPECT.get(orientation, "wide")}
    data = json.loads(_get(f"{OPENVERSE}?{urllib.parse.urlencode(params)}"))
    out = []
    for r in data.get("results") or []:
        urls = [u for u in (r.get("url"), r.get("thumbnail")) if u]
        if not urls:
            continue
        lic = f"CC {str(r.get('license') or '').upper()} {r.get('license_version') or ''}".strip()
        if str(r.get("license") or "").lower() in ("cc0", "pdm"):
            lic = str(r.get("license")).upper()
        out.append({"urls": urls, "width": int(r.get("width") or 0), "height": int(r.get("height") or 0),
                    "title": str(r.get("title") or query)[:120], "creator": str(r.get("creator") or "")[:80],
                    "license": lic, "source": str(r.get("foreign_landing_url") or r.get("url") or ""),
                    "attribution": str(r.get("attribution") or "")[:300]})
    return out


def _commons(query: str, orientation: str) -> list[dict]:
    params = {"action": "query", "format": "json", "generator": "search",
              "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": "6", "gsrlimit": "12",
              "prop": "imageinfo", "iiprop": "url|size|extmetadata|mime", "iiurlwidth": str(MAX_PX)}
    data = json.loads(_get(f"{COMMONS}?{urllib.parse.urlencode(params)}"))
    out = []
    for page in ((data.get("query") or {}).get("pages") or {}).values():
        info = (page.get("imageinfo") or [{}])[0]
        if not str(info.get("mime") or "").startswith("image/"):
            continue
        meta = info.get("extmetadata") or {}
        value = lambda k: str((meta.get(k) or {}).get("value") or "")
        url = info.get("thumburl") or info.get("url")
        if not url:
            continue
        out.append({"urls": [url], "width": int(info.get("width") or 0),
                    "height": int(info.get("height") or 0),
                    "title": str(page.get("title") or query).replace("File:", "")[:120],
                    "creator": value("Artist")[:80], "license": value("LicenseShortName") or "see source",
                    "source": str(info.get("descriptionurl") or url),
                    "attribution": f"{value('Artist')[:80]} — {value('LicenseShortName')}".strip(" —")})
    return out


def find_photo(query: str, orientation: str = "landscape",
               log: Callable[[str], None] = print) -> Optional[Photo]:
    """The best photo for a query, from cache or the web, or None. Never raises."""
    query = " ".join(str(query or "").split())[:120]
    if not query:
        return None
    orientation = orientation if orientation in _ASPECT else "landscape"
    key = _cache_key(query, orientation)
    cached = _from_cache(key)
    if cached:
        return cached
    for name, search in (("Openverse", _openverse), ("Wikimedia Commons", _commons)):
        try:
            candidates = search(query, orientation)
        except Exception as e:
            log(f"{name} search for '{query}' failed: {e}")
            continue
        # Large and the right shape first; the rest only if nothing better.
        candidates.sort(key=lambda c: (not _fits(c["width"], c["height"], orientation),
                                       -(min(c["width"], 2400) or 0)))
        for cand in candidates[:6]:
            for url in cand["urls"]:
                try:
                    image, w, h = _shrink(_get(url))
                except Exception:
                    continue
                photo = Photo(query=query, width=w or cand["width"], height=h or cand["height"],
                              title=cand["title"], creator=cand["creator"], license=cand["license"],
                              source=cand["source"], attribution=cand["attribution"])
                return _store(key, image, photo)
    return None


def fetch_assets(wanted: list[dict], log: Callable[[str], None] = print,
                 budget_seconds: float = 60.0) -> tuple[dict[str, str], list[Photo]]:
    """{key: base64} for the planner's image list, plus the photos for credits.

    `wanted` is [{"key": "hero", "query": "...", "orientation": "landscape"}].
    Bounded in time as a whole: a slow image host costs at most the budget,
    after which the remaining slots keep their placeholders."""
    assets: dict[str, str] = {}
    photos: list[Photo] = []
    items, seen = [], set()
    for item in wanted[:8]:
        key = str(item.get("key") or "").strip()
        if key and key not in seen:
            seen.add(key)
            items.append((key, str(item.get("query") or ""), str(item.get("orientation") or "landscape")))
    if not items:
        return assets, photos
    # All searches at once: one slow host no longer holds up the others, and
    # the whole batch costs at most the budget.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(items)))
    futures = {pool.submit(find_photo, q, o, log): k for k, q, o in items}
    try:
        for future in concurrent.futures.as_completed(futures, timeout=budget_seconds):
            photo = future.result()
            if photo:
                try:
                    assets[futures[future]] = photo.b64()
                    photos.append(photo)
                except OSError as e:
                    log(f"cached photo for '{futures[future]}' unreadable: {e}")
    except concurrent.futures.TimeoutError:
        log("photo search took too long — remaining images stay placeholders")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return assets, photos


def credits(photos: list[Photo]) -> str:
    """One line per photo, for the HUD and the design's notes."""
    lines = []
    for p in photos:
        who = f" by {p.creator}" if p.creator else ""
        lines.append(f"• {p.title}{who} — {p.license} — {p.source}")
    return "\n".join(lines)
