"""RoofScout pipeline: zip code -> addresses -> aerial roof views -> vision grades.

Sources (all free, no keys):
- Nominatim (OpenStreetMap) for zip centroid + reverse-geocoded addresses.
- Esri World Imagery tiles for high-res aerial views (primary).
- USGS NAIP aerial photos via the National Map (free fallback where Esri has no coverage).
- Gemini (gemini-3.1-flash-lite) vision for the 0-5 roof Tru-scale grade.
"""
from __future__ import annotations

import base64
import io
import json
import math
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageStat

NOMINATIM = "https://nominatim.openstreetmap.org"
_UA = {"User-Agent": "RoofScout/1.0 (residential roof condition finder)"}
_nomi_lock = threading.Lock()
_last_nomi = [0.0]


def _nominatim(path: str, params: dict):
    """Nominatim usage policy: max 1 req/s, valid User-Agent."""
    with _nomi_lock:
        wait = 1.15 - (time.time() - _last_nomi[0])
        if wait > 0:
            time.sleep(wait)
        url = NOMINATIM + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=_UA)
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            data = None
        _last_nomi[0] = time.time()
        return data


_ZIP_RE = re.compile(r"^\d{5}$")


def zip_center(zipcode: str):
    """Return (lat, lng, display_name) for a US zip, or None."""
    d = _nominatim("/search", {"postalcode": zipcode, "country": "us",
                               "format": "json", "limit": 1})
    if not d:
        return None
    return float(d[0]["lat"]), float(d[0]["lon"]), d[0].get("display_name", "")


def sample_addresses(zipcode: str, center, count: int, progress=None):
    """Jittered-grid reverse-geocode sampling. Returns list of address dicts."""
    lat0, lng0, _ = center
    # ~2.2km radius grid; denser near the middle
    radius_km = 2.2
    steps = 9
    pts = []
    for i in range(steps):
        for j in range(steps):
            dx = (i - (steps - 1) / 2) / ((steps - 1) / 2)
            dy = (j - (steps - 1) / 2) / ((steps - 1) / 2)
            if dx * dx + dy * dy > 1.0:
                continue
            # jitter so repeat scans vary a little
            jx = (hash(f"{zipcode}{i}{j}a") % 1000) / 1000 - 0.5
            jy = (hash(f"{zipcode}{i}{j}b") % 1000) / 1000 - 0.5
            lat = lat0 + (dy * radius_km + jy * 0.12) / 111.0
            lng = lng0 + (dx * radius_km + jx * 0.12) / (111.0 * math.cos(math.radians(lat0)))
            pts.append((lat, lng))
    # inside-out order: best addresses first
    pts.sort(key=lambda p: (p[0] - lat0) ** 2 + (p[1] - lng0) ** 2)

    found = []
    seen = set()
    for idx, (la, ln) in enumerate(pts):
        if len(found) >= count:
            break
        if progress:
            progress("addresses", len(found), count,
                     f"Finding addresses… {len(found)}/{count}")
        d = _nominatim("/reverse", {"lat": la, "lon": ln, "format": "json",
                                    "addressdetails": 1, "zoom": 18})
        if not d or "address" not in d:
            continue
        a = d["address"]
        house, road = a.get("house_number"), a.get("road")
        pc = (a.get("postcode") or "")[:5]
        if not house or not road or pc != zipcode:
            continue
        key = (house, road)
        if key in seen:
            continue
        seen.add(key)
        city = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or ""
        found.append({
            "address": f"{house} {road}",
            "city": city,
            "state": a.get("state", ""),
            "postcode": pc,
            "county": (a.get("county") or "").replace(" County", ""),
            "area": a.get("suburb") or a.get("neighbourhood") or a.get("quarter") or "",
            "lat": float(d["lat"]),
            "lng": float(d["lon"]),
        })
    return found


# ---------------- aerial tiles ----------------

_ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
_USGS_EXPORT = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/export"


def _tile_xy(lat: float, lng: float, z: int):
    n = 2 ** z
    x = int((lng + 180.0) / 360.0 * n)
    lr = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2.0 * n)
    return x, y


def _fetch_tile(z: int, x: int, y: int) -> Image.Image | None:
    url = _ESRI.format(z=z, y=y, x=x)
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return Image.open(io.BytesIO(resp.read())).convert("RGB")
    except Exception:
        return None


def _is_placeholder(tile: Image.Image) -> bool:
    """Esri answers HTTP 200 with a flat gray 'Map data not yet available'
    tile where it has no imagery. Real aerial tiles have far more variance."""
    return ImageStat.Stat(tile.convert("L")).stddev[0] < 15.0


def _usgs_image(lat: float, lng: float, half_m: float = 60.0) -> bytes | None:
    """USGS NAIP aerial photo via the National Map export endpoint (free, no key).
    Single 512x512 JPEG for a ~120m box around the point."""
    dlat = half_m / 111000.0
    dlng = half_m / (111000.0 * max(0.2, math.cos(math.radians(lat))))
    params = {
        "bbox": f"{lng - dlng},{lat - dlat},{lng + dlng},{lat + dlat}",
        "bboxSR": "4326", "imageSR": "4326",
        "size": "512,512", "format": "jpg", "f": "image",
    }
    url = _USGS_EXPORT + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = resp.read()
        if not data.startswith(b"\xff\xd8\xff"):
            return None  # error JSON, not an image
        img = Image.open(io.BytesIO(data)).convert("RGB")
        if _is_placeholder(img):
            return None
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=82)
        return buf.getvalue()
    except Exception:
        return None


def roof_image(lat: float, lng: float, z: int = 20) -> tuple[bytes | None, int, str]:
    """2x2 tile stitch (~60m across at z20) centered near the point.

    Where Esri has no coverage at the requested zoom it serves placeholder
    tiles; step down to z-1 then z-2, then fall back to free USGS NAIP aerial
    photos before giving up. Never grade a placeholder as if it were a roof.
    Returns (JPEG bytes or None, zoom used, imagery source).
    """
    for zz in (z, z - 1, z - 2):
        x, y = _tile_xy(lat, lng, zz)
        with ThreadPoolExecutor(max_workers=4) as ex:
            tiles = list(ex.map(_fetch_tile, [zz] * 4, [x, x + 1, x, x + 1],
                                [y, y, y + 1, y + 1]))
        if any(t is None for t in tiles):
            continue
        if all(_is_placeholder(t) for t in tiles):
            continue
        canvas = Image.new("RGB", (512, 512))
        canvas.paste(tiles[0], (0, 0))
        canvas.paste(tiles[1], (256, 0))
        canvas.paste(tiles[2], (0, 256))
        canvas.paste(tiles[3], (256, 256))
        buf = io.BytesIO()
        canvas.save(buf, "JPEG", quality=82)
        return buf.getvalue(), zz, "esri"
    usgs = _usgs_image(lat, lng)
    if usgs:
        return usgs, 18, "usgs"
    return None, z, "none"


# ---------------- vision grading ----------------

GRADE_PROMPT = """You are a roof inspector grading residential roofs from aerial imagery, using the Roof Tru Scale (0-5). Grade ONLY from what you can see on the target property near the center of each image — never guess about age or materials beyond visible evidence.

5 SOLID — looks new or like-new: uniform color, crisp shingle lines, clean ridges, no visible wear.
4 HEALTHY — minor cosmetic aging only; clearly years of life left.
3 AGING — visible wear: patchy granule loss (shiny or mottled sheen), slight curling or lifting at edges, light moss or algae staining. Worth watching.
2 WORN — needs repair soon: missing, cracked, or lifted shingles; heavy moss or algae coverage; rusted or lifting flashing; debris buildup or ponding.
1 FAILING — needs replacement now: sagging or uneven roof deck, blue tarps, large bare or patched areas, collapsed sections, structural deformation.
0 UNVERIFIABLE — the target roof cannot be assessed (heavy tree cover, deep shadow, mostly out of frame, too coarse). Never guess; use 0.

The images are in order. Return a JSON array with exactly one object per image, in order: {"grade": 0-5, "confidence": "low|medium|high", "evidence": ["up to 3 short visual observations"]}. Return ONLY the JSON array."""

_GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
               "gemini-3.1-flash-lite:generateContent")


def _gemini_call(api_key: str, image_b64_list: list[str]) -> str | None:
    parts: list[dict] = [{"text": GRADE_PROMPT}]
    for b64 in image_b64_list:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 3000,
                             "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(
        _GEMINI_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        cands = data.get("candidates") or []
        txt = "".join(p.get("text", "") for p in
                      cands[0]["content"]["parts"] if p.get("text")).strip()
        return txt or None
    except Exception:
        return None


def _parse_grades(txt: str | None, n: int):
    if not txt:
        return None
    try:
        arr = json.loads(txt)
        if not isinstance(arr, list) or len(arr) != n:
            return None
        out = []
        for g in arr:
            grade = int(g.get("grade", 0))
            grade = max(0, min(5, grade))
            ev = g.get("evidence") or []
            out.append({"grade": grade,
                        "confidence": str(g.get("confidence", "low"))[:10],
                        "evidence": [str(e)[:160] for e in ev[:3]]})
        return out
    except Exception:
        return None


def grade_roofs(api_key: str, houses: list[dict], progress=None,
                grader=None) -> list[dict]:
    """Attach grade/confidence/evidence to each house. grader() is a test hook."""
    pending = [h for h in houses if h.get("image_b64")]
    batch = 4
    results: dict[str, dict] = {}
    total = len(pending)
    done = 0
    for i in range(0, total, batch):
        chunk = pending[i:i + batch]
        if progress:
            progress("grading", done, total,
                     f"Grading roofs… {done}/{total}")
        if grader:
            grades = grader([c["image_b64"] for c in chunk])
        else:
            txt = _gemini_call(api_key, [c["image_b64"] for c in chunk])
            grades = _parse_grades(txt, len(chunk))
            if grades is None:  # one retry
                txt = _gemini_call(api_key, [c["image_b64"] for c in chunk])
                grades = _parse_grades(txt, len(chunk))
        for h, g in zip(chunk, grades or []):
            results[h["key"]] = g
        done += len(chunk)
    for h in houses:
        g = results.get(h["key"])
        if g:
            h.update(g)
        else:
            h.update({"grade": 0, "confidence": "low",
                      "evidence": ["grading unavailable"]})
    return houses


_GRADE_ORDER = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 0: 5}


def sort_leads(houses: list[dict]) -> list[dict]:
    return sorted(houses, key=lambda h: (_GRADE_ORDER.get(h.get("grade", 0), 5),
                                         h.get("address", "")))
