"""RoofScout pipeline: zip code -> buildings -> aerial roof views -> vision grades.

Sources (all free, no keys):
- Overpass API (OpenStreetMap) for real building footprints with address tags.
- Nominatim (OpenStreetMap) for zip centroid + reverse-geocoded addresses.
- Esri World Imagery tiles for high-res aerial views (primary).
- USGS NAIP aerial photos via the National Map (free fallback where Esri has no coverage).
- Gemini (gemini-3.1-flash-lite) vision with a strict JSON response schema
  for the 0-5 roof Tru-scale grade.
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
    """Return (lat, lng, display_name, city) for a US zip, or None."""
    d = _nominatim("/search", {"postalcode": zipcode, "country": "us",
                               "format": "json", "limit": 1,
                               "addressdetails": 1})
    if not d:
        return None
    a = d[0].get("address", {})
    city = a.get("city") or a.get("town") or a.get("village") or ""
    return (float(d[0]["lat"]), float(d[0]["lon"]),
            d[0].get("display_name", ""), city)


_OVERPASS = ("https://overpass-api.de/api/interpreter",
             "https://overpass.kumi.systems/api/interpreter")


def _overpass_buildings(zipcode: str, lat0: float, lng0: float, limit: int = 120):
    """All building footprints in the area (roof-first, not address-first).

    Tries the postal_code area first, then a tight bbox around the zip center
    (Nominatim's zip bounding box is far too coarse for dense zips — a raw
    bbox query can return tens of megabytes). Buildings already carrying
    addr:housenumber + addr:street keep their address so no reverse-geocoding
    is needed later; the rest get matched to addresses after grading, only
    for roofs that actually need repair."""
    r = 1.6 / 111.0  # ~1.6km half-box around the zip center
    cosla = max(0.2, math.cos(math.radians(lat0)))
    s, n = lat0 - r, lat0 + r
    w, e = lng0 - r / cosla, lng0 + r / cosla
    queries = [
        # postal_code areas are NOT country-scoped ("21502" is also a German
        # PLZ), so intersect with the US boundary area.
        (f'[out:json][timeout:25];area["ISO3166-1"="US"][admin_level=2]->.us;'
         f'area["postal_code"="{zipcode}"]->.a;'
         f'(way["building"](area.a)(area.us););'
         f'out center tags {limit};'),
        (f'[out:json][timeout:25];'
         f'(way["building"]'
         f'({s},{w},{n},{e}););out center tags {limit};'),
    ]
    for q in queries:
        for ep in _OVERPASS:
            try:
                req = urllib.request.Request(
                    ep, data=q.encode(),
                    headers={**_UA, "Content-Type": "text/plain"})
                with urllib.request.urlopen(req, timeout=45) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                out = []
                for el in data.get("elements", []):
                    c, t = el.get("center"), el.get("tags", {})
                    if not c:
                        continue
                    num, street = t.get("addr:housenumber"), t.get("addr:street")
                    out.append({"address": f"{num} {street}" if num and street else "",
                                "lat": float(c["lat"]), "lng": float(c["lon"]),
                                "building": t.get("building", "")})
                if out:
                    return out
            except Exception:
                continue
    return []


def _area_context(lat: float, lng: float) -> dict:
    """One reverse-geocode for city/state/county/postcode context."""
    d = _nominatim("/reverse", {"lat": lat, "lon": lng, "format": "json",
                                "addressdetails": 1, "zoom": 10})
    a = (d or {}).get("address", {}) if d else {}
    return {
        "city": a.get("city") or a.get("town") or a.get("village") or "",
        "state": a.get("state", ""),
        "county": (a.get("county") or "").replace(" County", ""),
    }


def geocode_address(query: str):
    """Geocode a typed street address -> single address dict, or None.

    Requires a house number + street so the aerial view centers on a real
    rooftop, not a city centroid."""
    d = _nominatim("/search", {"q": query, "countrycodes": "us",
                               "format": "json", "addressdetails": 1,
                               "limit": 1})
    if not d:
        return None
    r = d[0]
    a = r.get("address", {})
    house, road = a.get("house_number"), a.get("road")
    if not house or not road:
        return None
    city = (a.get("city") or a.get("town") or a.get("village")
            or a.get("hamlet") or "")
    return {
        "address": f"{house} {road}",
        "city": city,
        "state": a.get("state", ""),
        "postcode": (a.get("postcode") or "")[:5],
        "county": (a.get("county") or "").replace(" County", ""),
        "area": a.get("suburb") or a.get("neighbourhood") or a.get("quarter") or "",
        "lat": float(r["lat"]),
        "lng": float(r["lon"]),
    }


def sample_roofs(zipcode: str, center, count: int, progress=None):
    """Roof-first sampling: every building footprint is a candidate roof.

    Overpass footprints first (real rooftops — addresses attached only where
    the tags already carry them), topped up by a jittered image-sweep grid
    when Overpass is thin. Addresses get matched to damaged roofs AFTER
    grading, via reverse-geocoding. Returns roof-point dicts."""

    lat0, lng0, _display, city = center
    found: list[dict] = []

    def note():
        if progress:
            progress("roofs", len(found), count,
                     f"Finding rooftops… {len(found)}/{count}")

    note()
    ctx: dict = {}
    try:
        blds = _overpass_buildings(zipcode, lat0, lng0,
                                   limit=max(count * 6, 120))
    except Exception:
        blds = []
    if blds:
        ctx = _area_context(lat0, lng0)
        # dedup: one roof per ~35m cell (split building parts collapse)
        seen_cells: set[tuple[int, int]] = set()
        uniq = []
        for b in blds:
            cell = (round(b["lat"] * 3000), round(b["lng"] * 3000))
            if cell in seen_cells:
                continue
            seen_cells.add(cell)
            uniq.append(b)
        # spread across the area: stride through lat-sorted footprints
        uniq.sort(key=lambda b: (b["lat"], b["lng"]))
        stride = max(1, len(uniq) // max(count, 1))
        for b in uniq[::stride]:
            if len(found) >= count:
                break
            found.append({
                "address": b["address"],
                "city": city or ctx.get("city", ""),
                "state": ctx.get("state", ""),
                "postcode": zipcode,
                "county": ctx.get("county", ""),
                "area": "",
                "lat": b["lat"],
                "lng": b["lng"],
                "building": b.get("building", ""),
            })
            note()
    if len(found) < count:
        found.extend(_grid_points(zipcode, center, count - len(found),
                                 progress=progress,
                                 base=len(found), total=count))
    for h in found:
        h.setdefault("city", city or "")
        h.setdefault("state", ctx.get("state", ""))
        h.setdefault("postcode", zipcode)
        h.setdefault("county", ctx.get("county", ""))
        h.setdefault("area", "")
    return found


# Backwards-compatible alias (server used to call this).
def sample_addresses(zipcode: str, center, count: int, progress=None):
    return sample_roofs(zipcode, center, count, progress)


def _grid_points(zipcode: str, center, count: int, progress=None,
                 base: int = 0, total: int | None = None):
    """Jittered-grid sweep points (fallback). Pure image search: no addresses
    are resolved here — damaged roofs found on these cells get matched to
    addresses after grading."""
    lat0, lng0 = center[0], center[1]
    total = total or count
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
    # inside-out order: best cells first
    pts.sort(key=lambda p: (p[0] - lat0) ** 2 + (p[1] - lng0) ** 2)

    found = []
    for la, ln in pts:
        if len(found) >= count:
            break
        if progress:
            progress("roofs", base + len(found), total,
                     f"Sweeping imagery grid… {base + len(found)}/{total}")
        found.append({"address": "", "lat": la, "lng": ln})
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


def _tile_frac(lat: float, lng: float, z: int):
    """Containing tile plus the target's fractional position inside it."""
    n = 2 ** z
    x = int((lng + 180.0) / 360.0 * n)
    lr = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lr) + 1.0 / math.cos(lr)) / math.pi)
            / 2.0 * n)
    fx = (lng + 180.0) / 360.0 * n - x
    fy = ((1.0 - math.log(math.tan(lr) + 1.0 / math.cos(lr)) / math.pi)
          / 2.0 * n - y)
    return x, y, fx, fy


def _centered_esri(lat: float, lng: float, z: int):
    """512x512 Esri crop mathematically centered on (lat, lng).

    Fetches the 3x3 tile neighborhood around the containing tile and crops
    a 512x512 window centered on the target pixel, so the roof being graded
    sits in the middle of the frame. Returns JPEG bytes or None."""
    x, y, fx, fy = _tile_frac(lat, lng, z)
    xs = [xx for yy in (y - 1, y, y + 1) for xx in (x - 1, x, x + 1)]
    ys = [yy for yy in (y - 1, y, y + 1) for xx in (x - 1, x, x + 1)]
    with ThreadPoolExecutor(max_workers=9) as ex:
        tiles = list(ex.map(_fetch_tile, [z] * 9, xs, ys))
    if any(t is None for t in tiles):
        return None
    if _is_placeholder(tiles[4]):  # center tile = no coverage at this zoom
        return None
    canvas = Image.new("RGB", (768, 768))
    for i, t in enumerate(tiles):
        canvas.paste(t, ((i % 3) * 256, (i // 3) * 256))
    cx, cy = int((1 + fx) * 256), int((1 + fy) * 256)
    crop = canvas.crop((cx - 256, cy - 256, cx + 256, cy + 256))
    buf = io.BytesIO()
    crop.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def roof_image(lat: float, lng: float, z: int = 20) -> tuple[bytes | None, int, str]:
    """512x512 aerial view centered on the target rooftop.

    Where Esri has no coverage at the requested zoom it serves placeholder
    tiles; step down to z-1 then z-2, then fall back to free USGS NAIP aerial
    photos before giving up. Never grade a placeholder as if it were a roof.
    Returns (JPEG bytes or None, zoom used, imagery source).
    """
    for zz in (z, z - 1, z - 2):
        img = _centered_esri(lat, lng, zz)
        if img:
            return img, zz, "esri"
    usgs = _usgs_image(lat, lng)
    if usgs:
        return usgs, 18, "usgs"
    return None, z, "none"


# ---------------- vision grading ----------------

GRADE_PROMPT = """You are a senior forensic roof inspector grading residential roofs from aerial imagery, using the Roof Tru Scale (0-5). Grade ONLY the target property near the center of each image — never guess about age or materials beyond visible evidence.

5 SOLID — looks new or like-new: uniform color, crisp shingle lines, clean ridges, no visible wear.
4 HEALTHY — minor cosmetic aging only; clearly years of life left.
3 AGING — visible wear: patchy granule loss (shiny or mottled sheen), slight curling or lifting at edges, light moss or algae staining. Worth watching.
2 WORN — needs repair soon: missing, cracked, or lifted shingles; heavy moss or algae coverage; rusted or lifting flashing; debris buildup or ponding.
1 FAILING — needs replacement now: sagging or uneven roof deck, blue tarps, large bare or patched areas, collapsed sections, structural deformation.
0 UNVERIFIABLE — the target roof cannot be assessed (heavy tree cover, deep shadow, mostly out of frame, too coarse). Never guess; use 0.

CRITICAL DISCRIMINATION RULES:
1. Solar panels: do NOT count solar arrays or mounting brackets as damage or discoloration. Grade only the exposed roof surface.
2. Shadows: differentiate sharp tree-limb shadows from sagging or missing shingles. Check whether the dark shape matches a tree next to the house.
3. Glare: high sun angles cause white reflective glare on metal or asphalt. Do not confuse glare with missing material.

For each image also report: primary_material (asphalt shingle, metal, clay/concrete tile, slate, or membrane/flat), pitch_estimate (Flat, Low-slope, Medium, or Steep), obstruction_notes (tree cover, solar panels, shadows, glare — or empty if the view is clear), and damage_boxes — bounding boxes as [ymin, xmin, ymax, xmax] in 0-1000 normalized coordinates around each visible damage area (missing shingles, tarps, ponding, etc.), with a short label per box. For grades 1-2 include at least one damage_box around the worst-affected area. Omit damage_boxes for healthy roofs.

The images are in order. Return a JSON array with exactly one object per image, in order: {"grade": 0-5, "confidence": "low|medium|high", "evidence": ["up to 3 short visual observations"], "primary_material": "...", "pitch_estimate": "...", "obstruction_notes": "...", "damage_boxes": [{"box_2d": [ymin,xmin,ymax,xmax], "label": "..."}]}. Return ONLY the JSON array."""

_GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
               "gemini-3.1-flash-lite:generateContent")


def _gemini_call(api_key: str, image_b64_list: list[str]) -> str | None:
    parts: list[dict] = [{"text": GRADE_PROMPT}]
    for b64 in image_b64_list:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.1, "maxOutputTokens": 4000,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "grade": {"type": "INTEGER",
                                  "description": "Roof Tru Scale: 0 Unclear, 1 Failing, 2 Worn, 3 Aging, 4 Healthy, 5 Solid"},
                        "confidence": {"type": "STRING",
                                       "enum": ["low", "medium", "high"]},
                        "evidence": {"type": "ARRAY",
                                     "items": {"type": "STRING"}, "maxItems": 3,
                                     "description": "Specific visual indicators, e.g. missing tabs, edge curling, algae streaks, tarping"},
                        "primary_material": {"type": "STRING",
                                             "description": "Asphalt shingle, metal, clay/concrete tile, slate, or membrane"},
                        "pitch_estimate": {"type": "STRING",
                                           "description": "Flat, Low-slope, Medium, or Steep"},
                        "obstruction_notes": {"type": "STRING",
                                              "description": "Tree shadows, solar panels, canopy coverage, glare; empty string if clear"},
                        "damage_boxes": {
                            "type": "ARRAY",
                            "description": "Bounding boxes outlining visible damage areas",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "box_2d": {
                                        "type": "ARRAY",
                                        "items": {"type": "INTEGER"},
                                        "minItems": 4, "maxItems": 4,
                                        "description": "[ymin, xmin, ymax, xmax] normalized 0-1000"},
                                    "label": {"type": "STRING",
                                              "description": "E.g. missing_shingles, curled_edges, ponding, tarp"},
                                },
                                "required": ["box_2d", "label"],
                            }},
                    },
                    "required": ["grade", "confidence", "evidence"],
                },
            },
        },
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
            boxes = []
            for b in (g.get("damage_boxes") or [])[:6]:
                bb = b.get("box_2d") if isinstance(b, dict) else None
                if isinstance(bb, list) and len(bb) == 4:
                    try:
                        y0, x0, y1, x1 = (max(0, min(1000, int(v))) for v in bb)
                        if x1 > x0 and y1 > y0:
                            boxes.append({
                                "box": [y0, x0, y1, x1],
                                "label": str(b.get("label", "damage"))[:40],
                            })
                    except (ValueError, TypeError):
                        pass
            out.append({"grade": grade,
                        "confidence": str(g.get("confidence", "low"))[:10],
                        "evidence": [str(e)[:160] for e in ev[:3]],
                        "material": str(g.get("primary_material", ""))[:80],
                        "pitch": str(g.get("pitch_estimate", ""))[:20],
                        "obstruction": str(g.get("obstruction_notes", ""))[:160],
                        "damage_boxes": boxes})
        return out
    except Exception:
        return None


def _gemini_json(api_key: str, parts: list[dict], schema: dict,
                 max_tokens: int = 4000):
    """Single Gemini call returning parsed JSON (or None)."""
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.1, "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
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
        return json.loads(txt) if txt else None
    except Exception:
        return None


PINPOINT_PROMPT = """You are a senior forensic roof inspector. This close-up aerial image shows ONE roof, centered in the frame, graded {grade}/5 on the Roof Tru Scale ({verdict}). Wide-view evidence: {evidence}

Do two things and return ONLY JSON:

1. "boxes": tight bounding boxes [ymin, xmin, ymax, xmax] in 0-1000 normalized coordinates around EACH distinct visible damage area on THIS roof (missing/cracked/lifted shingles, tarps, ponding, worst wear patches, rusted flashing). Boxes must be TIGHT — hug the damage, never the whole roof. If wear is diffuse, box the 1-3 worst patches. If you cannot localize any damage, return [].
2. "repairs": for each problem, one line naming the issue and one line saying what a roofer would do to fix it. Plain language a homeowner understands. Max 6 items.

Discrimination rules: solar panels are NOT damage. Tree shadows are NOT sagging. Sun glare is NOT missing material. Only mark what you can actually see."""

_PINPOINT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "boxes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "box_2d": {"type": "ARRAY", "items": {"type": "INTEGER"},
                               "minItems": 4, "maxItems": 4},
                    "label": {"type": "STRING"},
                },
                "required": ["box_2d", "label"],
            },
        },
        "repairs": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "issue": {"type": "STRING"},
                    "fix": {"type": "STRING"},
                },
                "required": ["issue", "fix"],
            },
        },
    },
}


def _valid_pinpoint_boxes(raw) -> list[dict]:
    boxes = []
    for b in (raw or [])[:6]:
        if not isinstance(b, dict):
            continue
        bb = b.get("box_2d")
        if not (isinstance(bb, list) and len(bb) == 4):
            continue
        try:
            y0, x0, y1, x1 = (max(0, min(1000, int(v))) for v in bb)
        except (ValueError, TypeError):
            continue
        if x1 <= x0 or y1 <= y0:
            continue
        area = (x1 - x0) * (y1 - y0) / 1e6  # fraction of the image
        if not 0.002 <= area <= 0.80:  # not a speck, not the whole roof
            continue
        boxes.append({"box": [y0, x0, y1, x1],
                      "label": str(b.get("label", "damage"))[:40]})
    return boxes


def _burn_boxes(img_bytes: bytes, boxes: list[dict]) -> bytes:
    """Draw pinpoint boxes + labels onto the close-up. Returns JPEG bytes."""
    from PIL import ImageDraw
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    d = ImageDraw.Draw(img)
    for b in boxes:
        y0, x0, y1, x1 = b["box"]
        px = [x0 / 1000 * w, y0 / 1000 * h, x1 / 1000 * w, y1 / 1000 * h]
        d.rectangle(px, outline=(255, 59, 48), width=4)
        d.rectangle([px[0], px[1] - 18, px[0] + 8 * len(b["label"]) + 8,
                     px[1]], fill=(255, 59, 48))
        d.text((px[0] + 4, px[1] - 16), b["label"], fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=84)
    return buf.getvalue()


def localize_damage(api_key: str, houses: list[dict], progress=None):
    """Pinpoint pass for damaged roofs: zoomed close-up centered on the roof,
    tight damage boxes burned into the image, plus a plain-language repair
    breakdown. Sets h["damage_img"] (data URI) and h["repair_breakdown"]."""
    targets = [h for h in houses
               if h.get("grade") in (1, 2) and h.get("image_b64")]
    total = len(targets)
    for i, h in enumerate(targets):
        if progress:
            progress("pinpoint", i, total,
                     f"Pinpointing damage… {i}/{total}")
        try:
            z = min((h.get("zoom") or 19) + 1, 20)
            closeup = _centered_esri(h["lat"], h["lng"], z)
            if not closeup:
                closeup = base64.b64decode(h["image_b64"])
            b64 = base64.b64encode(closeup).decode()
            verdict = "FAILING — needs replacement" if h["grade"] == 1 \
                else "WORN — needs repair soon"
            prompt = PINPOINT_PROMPT.format(
                grade=h["grade"], verdict=verdict,
                evidence="; ".join(h.get("evidence") or ["visible wear"]))
            res = _gemini_json(
                api_key,
                [{"text": prompt},
                 {"inline_data": {"mime_type": "image/jpeg", "data": b64}}],
                _PINPOINT_SCHEMA)
            boxes, repairs = [], []
            if isinstance(res, dict):
                boxes = _valid_pinpoint_boxes(res.get("boxes"))
                for r in (res.get("repairs") or [])[:6]:
                    if isinstance(r, dict) and r.get("issue"):
                        repairs.append({
                            "issue": str(r["issue"])[:200],
                            "fix": str(r.get("fix", ""))[:200],
                        })
            if boxes:
                marked = _burn_boxes(closeup, boxes)
                h["damage_img"] = ("data:image/jpeg;base64," +
                                   base64.b64encode(marked).decode())
            if repairs:
                h["repair_breakdown"] = repairs
        except Exception:
            continue
    if progress:
        progress("pinpoint", total, total, f"Pinpointed {total} roof(s).")
    return houses


def attach_addresses(houses: list[dict], progress=None):
    """Match damaged roofs to mailable street addresses.

    Only roofs graded 1-3 get reverse-geocoded (Nominatim, ~1 req/sec) —
    healthy roofs don't need a letter. Roofs that already carry an address
    (OSM tags, typed search) are left alone. Sets h["address"] etc. where a
    house number + street is found; otherwise the card shows a map link so
    the address can be verified by hand."""
    targets = [h for h in houses
               if h.get("grade") in (1, 2, 3) and not h.get("address")]
    total = len(targets)
    for i, h in enumerate(targets):
        if progress:
            progress("addresses", i, total,
                     f"Matching addresses… {i}/{total}")
        try:
            d = _nominatim("/reverse",
                           {"lat": h["lat"], "lon": h["lng"], "format": "json",
                            "addressdetails": 1, "zoom": 18})
            a = (d or {}).get("address", {}) if d else {}
            house, road = a.get("house_number"), a.get("road")
            if house and road:
                h["address"] = f"{house} {road}"
                h["city"] = (a.get("city") or a.get("town") or
                             a.get("village") or a.get("hamlet") or
                             h.get("city", ""))
                h["state"] = a.get("state", "") or h.get("state", "")
                h["postcode"] = ((a.get("postcode") or "")[:5] or
                                 h.get("postcode", ""))
                h["county"] = ((a.get("county") or "").replace(" County", "") or
                               h.get("county", ""))
                h["area"] = (a.get("suburb") or a.get("neighbourhood") or
                             a.get("quarter") or h.get("area", ""))
        except Exception:
            pass
        time.sleep(1.05)  # Nominatim usage policy
    if progress:
        progress("addresses", total, total,
                 f"Matched {sum(1 for h in targets if h.get('address'))} address(es).")
    return houses


def grade_roofs(api_key: str, houses: list[dict], progress=None,
                grader=None) -> list[dict]:
    """Attach grade/confidence/evidence to each house. grader() is a test hook."""
    pending = [h for h in houses if h.get("image_b64")]
    batch = 4
    results: dict[str, dict] = {}
    total = len(pending)
    done = [0]
    lock = threading.Lock()

    def grade_chunk(chunk):
        if progress:
            with lock:
                progress("grading", done[0], total,
                         f"Grading roofs… {done[0]}/{total}")
        if grader:
            grades = grader([c["image_b64"] for c in chunk])
        else:
            txt = _gemini_call(api_key, [c["image_b64"] for c in chunk])
            grades = _parse_grades(txt, len(chunk))
            if grades is None:  # one retry
                txt = _gemini_call(api_key, [c["image_b64"] for c in chunk])
                grades = _parse_grades(txt, len(chunk))
        out = {}
        for h, g in zip(chunk, grades or []):
            out[h["key"]] = g
        with lock:
            done[0] += len(chunk)
        return out

    chunks = [pending[i:i + batch] for i in range(0, total, batch)]
    # concurrent grading batches — Gemini 3-class models handle the parallelism
    with ThreadPoolExecutor(max_workers=3) as ex:
        for partial in ex.map(grade_chunk, chunks):
            results.update(partial)
    for h in houses:
        g = results.get(h["key"])
        if g:
            h.update(g)
        else:
            h.update({"grade": 0, "confidence": "low",
                      "evidence": ["grading unavailable"],
                      "material": "", "pitch": "", "obstruction": "",
                      "damage_boxes": []})
    return houses


_GRADE_ORDER = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 0: 5}


def sort_leads(houses: list[dict]) -> list[dict]:
    return sorted(houses, key=lambda h: (_GRADE_ORDER.get(h.get("grade", 0), 5),
                                         h.get("address", "")))
