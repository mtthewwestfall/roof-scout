"""RoofScout pipeline: zip code -> buildings -> aerial roof views -> vision grades.

Sources (all free, no keys):
- Overpass API (OpenStreetMap) for real building footprints with address tags.
- Nominatim (OpenStreetMap) for zip centroid + reverse-geocoded addresses.
- Esri World Imagery tiles for high-res aerial views (primary).
- USGS NAIP aerial photos via the National Map (free fallback where Esri has no coverage).
- Mapillary street-level photos (MAPILLARY_ACCESS_TOKEN), then Google Street
  View Static API (GOOGLE_MAPS_API_KEY) as last-resort fallbacks when no aerial
  view exists. Each is skipped when its key is unset.
- Gemini (gemini-3.1-flash-lite) vision with a strict JSON response schema
  for the 0-5 roof Tru-scale grade.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import zlib
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
             "https://overpass.kumi.systems/api/interpreter",
             "https://overpass.private.coffee/api/interpreter")


_VACANT_TAGS = ("abandoned", "vacant", "ruins", "disused", "demolished")
_VACANT_BUILDING_VALUES = ("vacant", "abandoned", "ruins", "demolished",
                            "disused")


def _vacant_flag(tags: dict) -> bool:
    """True when OSM tags mark the building vacant/abandoned/condemned.

    Public map records — spotty coverage, but a free first-pass filter so a
    condemned building never wastes a deep scan or a lead slot.
    """
    for k, v in tags.items():
        kl, vl = str(k).lower(), str(v).lower()
        if kl in _VACANT_TAGS and vl not in ("no", "false", "0"):
            return True
        if kl == "building" and vl in _VACANT_BUILDING_VALUES:
            return True
    return False


class _OverpassDown(Exception):
    """The Overpass footprint lookup itself failed (every endpoint errored
    or timed out) — as opposed to succeeding with zero buildings found."""


def _overpass_buildings(zipcode: str, lat0: float, lng0: float, limit: int = 120):
    """All building footprints in the area (roof-first, not address-first).

    Queries a small bbox around the zip center first — fast when the
    mirror is healthy and it covers the town center where most roofs are —
    then widens to 1.6km only if the small box came back thin (rural zips).
    The old postal_code area query is gone: US ZIP areas don't resolve in
    OSM and the lookup just burned a minute before timing out.

    Buildings already carrying addr:housenumber + addr:street keep their
    address so no reverse-geocoding is needed later; the rest get matched
    to addresses after grading, only for roofs that actually need repair.

    Raises _OverpassDown when every endpoint failed, so callers can tell
    "map data unavailable" apart from "map data has no buildings here"."""
    def box_query(half_km: float) -> str:
        r = half_km / 111.0
        cosla = max(0.2, math.cos(math.radians(lat0)))
        s, n = lat0 - r, lat0 + r
        w, e = lng0 - r / cosla, lng0 + r / cosla
        return (f'[out:json][timeout:45];'
                f'(way["building"]'
                f'({s},{w},{n},{e}););out center tags {limit};')

    def parse(data) -> list[dict]:
        out = []
        for el in data.get("elements", []):
            c, t = el.get("center"), el.get("tags", {})
            if not c:
                continue
            num, street = t.get("addr:housenumber"), t.get("addr:street")
            out.append({"address": f"{num} {street}" if num and street else "",
                        "lat": float(c["lat"]), "lng": float(c["lon"]),
                        "building": t.get("building", ""),
                        "vacant": _vacant_flag(t)})
        return out

    def attempt(q: str) -> tuple[list[dict], bool]:
        """Try each mirror in turn. Returns (buildings, any_answered)."""
        any_ok = False
        for ep in _OVERPASS:
            try:
                req = urllib.request.Request(
                    ep, data=q.encode(),
                    headers={**_UA, "Content-Type": "text/plain"})
                with urllib.request.urlopen(req, timeout=50) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                any_ok = True
                blds = parse(data)
                if blds:
                    return blds, True
                break  # endpoint answered (empty); don't hammer the mirrors
            except Exception:
                continue
        return [], any_ok

    blds, any_ok = attempt(box_query(0.8))
    if len(blds) < 40:
        # Thin (or failed) small box: widen for rural zips. A failed small
        # box still counts as "tried" — any_ok tracks whether any mirror
        # actually answered.
        wide, wide_ok = attempt(box_query(1.6))
        any_ok = any_ok or wide_ok
        if len(wide) > len(blds):
            blds = wide
    if not any_ok:
        raise _OverpassDown(f"Overpass lookup failed for {zipcode}")
    return blds


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


_CENSUS = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


def _census_geocode(query: str):
    """US Census Geocoder fallback for addresses Nominatim doesn't know.

    Free, no key, authoritative for US addresses (OSM misses real streets,
    e.g. new subdivisions). Returns the same dict shape as geocode_address,
    or None."""
    params = {"address": query, "benchmark": "Public_AR_Current",
              "format": "json"}
    url = _CENSUS + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    matches = (data.get("result") or {}).get("addressMatches") or []
    if not matches:
        return None
    m = matches[0]
    comp = m.get("addressComponents") or {}
    coords = m.get("coordinates") or {}
    try:
        lat, lng = float(coords["y"]), float(coords["x"])
    except (KeyError, TypeError, ValueError):
        return None
    parts = [comp.get("fromAddress") or comp.get("toAddress"),
             comp.get("preDirection"), comp.get("streetName"),
             comp.get("suffixType"), comp.get("postDirection")]
    street = " ".join(p.strip().title() for p in parts if p and p.strip())
    if not street:
        street = (m.get("matchedAddress") or "").split(",")[0].strip().title()
    if not street:
        return None
    ctx = _area_context(lat, lng)
    return {
        "address": street,
        "city": (comp.get("city") or "").title() or ctx["city"],
        "state": comp.get("state") or ctx["state"],
        "postcode": (comp.get("zip") or "")[:5],
        "county": ctx["county"],
        "area": "",
        "lat": lat,
        "lng": lng,
    }


_CENSUS_GEO = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
_PHOTON_REV = "https://photon.komoot.io/reverse"


def _census_locality(lat: float, lng: float):
    """Authoritative city/county/state from Census TIGER geographies.

    Free, no key. Note: the Census coordinates endpoint only returns
    geoLookup data (no street address), so this enriches the locality
    fields — the street address itself comes from Nominatim/Photon.
    Returns a dict with city/state/county, or None.
    """
    params = {"x": lng, "y": lat, "benchmark": "Public_AR_Current",
              "vintage": "Current_Current", "format": "json"}
    url = _CENSUS_GEO + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    geogs = (data.get("result") or {}).get("geographies") or {}

    def _first(layer):
        items = geogs.get(layer) or []
        return items[0] if items else {}

    states = _first("States")
    counties = _first("Counties")
    places = _first("Incorporated Places")
    if not (states or counties or places):
        return None
    return {
        "city": places.get("BASENAME", ""),
        "state": states.get("STUSAB", "") or states.get("BASENAME", ""),
        "county": counties.get("BASENAME", ""),
    }


def _photon_reverse(lat: float, lng: float):
    """Photon (Komoot) reverse geocoder. OSM-based but a separate index
    from Nominatim, so it sometimes knows addresses Nominatim misses.
    Free, no key. Returns an address dict or None."""
    params = {"lat": lat, "lon": lng}
    url = _PHOTON_REV + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    feats = data.get("features") or []
    if not feats:
        return None
    p = feats[0].get("properties") or {}
    num, street = p.get("housenumber"), p.get("street")
    if num and street:
        addr = f"{num} {street}"
    else:
        addr = (street or "").strip()
    if not addr:
        addr = (p.get("name") or "").strip()
    if not addr:
        return None
    return {
        "address": addr,
        "city": (p.get("city") or p.get("town") or p.get("village") or ""),
        "state": p.get("state") or "",
        "postcode": (p.get("postcode") or "")[:5],
        "county": (p.get("county") or "").replace(" County", ""),
    }


def _apply_address(h: dict, r: dict, confidence: str, source: str):
    """Fill a house's address fields from a resolver result, keeping the
    existing city/state/etc. when the resolver has nothing better."""
    h["address"] = r["address"]
    h["address_confidence"] = confidence
    h["address_source"] = source
    for k in ("city", "state", "postcode", "county"):
        h[k] = r.get(k) or h.get(k, "")


def _resolve_address(h: dict) -> bool:
    """Try the free reverse-geocoders in order; attach the first usable
    address. Returns True if any address (verified or estimated) was set.
    Nothing is invented: every guess comes from a real geocoder response
    for these coordinates. Census TIGER data fills the city/county/state
    authoritatively."""
    lat, lng = h["lat"], h["lng"]

    # 0. Census locality first (cheap, authoritative) — enriches whatever
    #    the street-level resolvers find below.
    try:
        loc = _census_locality(lat, lng) or {}
    except Exception:
        loc = {}

    # 1. Nominatim — verified when it returns house_number + road.
    try:
        d = _nominatim("/reverse",
                       {"lat": lat, "lon": lng, "format": "json",
                        "addressdetails": 1, "zoom": 18})
        a = (d or {}).get("address", {}) if d else {}
        house, road = a.get("house_number"), a.get("road")
        city = (a.get("city") or a.get("town") or a.get("village") or
                a.get("hamlet") or loc.get("city") or "")
        base = {
            "city": city,
            "state": a.get("state") or loc.get("state") or "",
            "postcode": (a.get("postcode") or "")[:5],
            "county": ((a.get("county") or "").replace(" County", "") or
                       loc.get("county") or ""),
        }
        if house and road:
            _apply_address(h, {"address": f"{house} {road}", **base},
                           "verified", "nominatim")
            h["area"] = (a.get("suburb") or a.get("neighbourhood") or
                         a.get("quarter") or h.get("area", ""))
            return True
        if road:
            # Street-level educated guess — better than a blank.
            _apply_address(h, {"address": road, **base},
                           "estimated", "nominatim")
            return True
    except Exception:
        pass

    # 2. Photon fallback (separate OSM index, sometimes knows more).
    try:
        r = _photon_reverse(lat, lng)
        if r and r.get("address"):
            for k in ("city", "state", "county"):
                r[k] = r.get(k) or loc.get(k) or ""
            _apply_address(h, r, "estimated", "photon")
            return True
    except Exception:
        pass

    # 3. No street found anywhere — still stamp the authoritative
    #    locality so the lead isn't a bare pin on a map.
    for k in ("city", "state", "county"):
        if loc.get(k) and not h.get(k):
            h[k] = loc[k]
    return False


def geocode_latlng(lat: float, lng: float):
    """Build a house dict from pasted coordinates.

    Reverse-geocodes for a display address + city/state/county context;
    falls back to the raw coordinates as the label when OSM knows nothing
    nearby."""
    d = _nominatim("/reverse", {"lat": lat, "lon": lng, "format": "json",
                                "addressdetails": 1, "zoom": 18})
    a = (d or {}).get("address", {}) if d else {}
    house, road = a.get("house_number"), a.get("road")
    address = (f"{house} {road}" if house and road
               else f"{lat:.5f},{lng:.5f}")
    ctx = _area_context(lat, lng)
    city = (a.get("city") or a.get("town") or a.get("village")
            or a.get("hamlet") or ctx["city"])
    return {
        "address": address,
        "city": city,
        "state": a.get("state", "") or ctx["state"],
        "postcode": (a.get("postcode") or "")[:5],
        "county": ctx["county"],
        "area": a.get("suburb") or a.get("neighbourhood") or "",
        "lat": lat,
        "lng": lng,
    }


def geocode_address(query: str):
    """Geocode a typed street address -> single address dict, or None.

    Requires a house number + street so the aerial view centers on a real
    rooftop, not a city centroid. Nominatim first, US Census Geocoder as
    fallback (OSM misses real streets)."""
    d = _nominatim("/search", {"q": query, "countrycodes": "us",
                               "format": "json", "addressdetails": 1,
                               "limit": 1})
    if d:
        r = d[0]
        a = r.get("address", {})
        house, road = a.get("house_number"), a.get("road")
        if house and road:
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
    # Nominatim missed it — try the Census geocoder before giving up.
    return _census_geocode(query)


def geocode_place(query: str):
    """Best-effort map center for a query that won't rooftop-geocode.

    Returns (lat, lng, label) for the closest Nominatim match of any kind
    (street, neighbourhood, city…), or None. Used to center the manual
    pin-drop picker so the user can point at the right roof themselves.
    """
    d = _nominatim("/search", {"q": query, "countrycodes": "us",
                               "format": "json", "addressdetails": 1,
                               "limit": 1})
    if not d:
        return None
    r = d[0]
    try:
        return (float(r["lat"]), float(r["lon"]),
                str(r.get("display_name", ""))[:90])
    except (TypeError, ValueError, KeyError):
        return None


def _cell_of(lat: float, lng: float) -> tuple[int, int]:
    # ~35m roof cell; rotation + dedup granularity.
    return (round(lat * 3000), round(lng * 3000))


def _footprint_pool(zipcode: str, center, limit: int,
                   progress=None) -> tuple[list[dict], bool]:
    """Deduplicated Overpass building footprints (one per ~35m cell).

    Returns (buildings, footprints_ok). footprints_ok=False means the
    Overpass lookup failed outright — the caller is working without map
    building data (blind grid fallback), NOT that the area has no buildings.
    """
    lat0, lng0 = center[0], center[1]
    if progress:
        progress("footprints", 0, 1, "Finding rooftops on the map…")
    try:
        blds = _overpass_buildings(zipcode, lat0, lng0, limit=limit)
        ok = True
    except Exception:
        blds, ok = [], False
    seen: set[tuple[int, int]] = set()
    uniq = []
    for b in blds:
        cell = _cell_of(b["lat"], b["lng"])
        if cell in seen:
            continue
        seen.add(cell)
        uniq.append(b)
    uniq.sort(key=lambda b: (b["lat"], b["lng"]))
    return uniq, ok


def _shape_houses(buildings: list[dict], ctx: dict, city: str,
                  zipcode: str) -> list[dict]:
    houses = []
    for b in buildings:
        houses.append({
            "address": b.get("address") or "",
            "city": city or ctx.get("city", ""),
            "state": ctx.get("state", ""),
            "postcode": zipcode,
            "county": ctx.get("county", ""),
            "area": "",
            "lat": b["lat"],
            "lng": b["lng"],
            "building": b.get("building", ""),
            "vacant": b.get("vacant", False),
        })
    return houses


MICRO_SCAN_POOL = 200


def candidate_roofs(zipcode: str, center, count: int, progress=None,
                    exclude_cells=None) -> tuple[list[dict], bool]:
    """Full deduplicated candidate pool for rotation + damage triage.

    Every footprint cell minus `exclude_cells` (roofs already shown to this
    customer), topped up with grid points when Overpass is thin. House
    dicts are shaped like sample_roofs() output.

    Returns (houses, footprints_ok); footprints_ok=False means the building
    lookup failed and the pool is blind grid points, not real rooftops.
    """
    exclude_cells = exclude_cells or set()
    lat0, lng0, _display, city = center
    ctx: dict = {}
    pool, footprints_ok = _footprint_pool(zipcode, center, MICRO_SCAN_POOL * 2,
                                          progress=progress)
    if pool:
        ctx = _area_context(lat0, lng0)
    pool = [b for b in pool
            if _cell_of(b["lat"], b["lng"]) not in exclude_cells]
    pool.sort(key=lambda b: (b["lat"] - lat0) ** 2 + (b["lng"] - lng0) ** 2)
    houses = _shape_houses(pool[:MICRO_SCAN_POOL], ctx, city, zipcode)
    want = max(count, MICRO_SCAN_POOL)
    if len(houses) < want:
        have = ({_cell_of(h["lat"], h["lng"]) for h in houses}
                | exclude_cells)
        grid = _grid_points(zipcode, center, want - len(houses),
                            progress=progress,
                            base=len(houses), total=want, exclude=have)
        for g in grid:
            if len(houses) >= want:
                break
            cell = _cell_of(g["lat"], g["lng"])
            if cell in have:
                continue
            have.add(cell)
            houses.append(g)
    for h in houses:
        h.setdefault("city", city or "")
        h.setdefault("state", ctx.get("state", ""))
        h.setdefault("postcode", zipcode)
        h.setdefault("county", ctx.get("county", ""))
        h.setdefault("area", "")
    if progress:
        progress("candidates", 1, 1, f"{len(houses)} candidate roofs")
    return houses, footprints_ok


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
    uniq, _footprints_ok = _footprint_pool(zipcode, center, max(count * 6, 120),
                                            progress=progress)
    if uniq:
        ctx = _area_context(lat0, lng0)
        # spread across the area: stride through lat-sorted footprints
        stride = max(1, len(uniq) // max(count, 1))
        for b in uniq[::stride]:
            if len(found) >= count:
                break
            found.extend(_shape_houses([b], ctx, city, zipcode))
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


_GRID_SPACING_KM = 0.275  # ~200 points inside the first 2.2km
_GRID_MAX_KM = 8.8


def _jitter(key: str) -> float:
    return (zlib.crc32(key.encode()) % 1000) / 1000 - 0.5


def _grid_points(zipcode: str, center, count: int, progress=None,
                 base: int = 0, total: int | None = None, exclude=None):
    """Jittered-grid sweep points (fallback). Pure image search: no addresses
    are resolved here — damaged roofs found on these cells get matched to
    addresses after grading.

    Fixed ~275m spacing, nearest first, out to _GRID_MAX_KM. Cells in
    `exclude` (already checked) are skipped, so each repeat scan widens the
    sweep past the roofs it already covered. Jitter is deterministic so the
    same ZIP yields the same points across restarts."""
    exclude = exclude or set()
    lat0, lng0 = center[0], center[1]
    total = total or count
    cosla = math.cos(math.radians(lat0))
    n = int(_GRID_MAX_KM / _GRID_SPACING_KM)
    pts = []
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            dx = i * _GRID_SPACING_KM + _jitter(f"{zipcode}{i}{j}a") * 0.12
            dy = j * _GRID_SPACING_KM + _jitter(f"{zipcode}{i}{j}b") * 0.12
            d = math.hypot(dx, dy)
            if d > _GRID_MAX_KM:
                continue
            pts.append((d, lat0 + dy / 111.0, lng0 + dx / (111.0 * cosla)))
    pts.sort()

    found = []
    for _d, la, ln in pts:
        if len(found) >= count:
            break
        if _cell_of(la, ln) in exclude:
            continue
        if progress:
            progress("roofs", base + len(found), total,
                     f"Sweeping imagery grid… {base + len(found)}/{total}")
        found.append({"address": "", "lat": la, "lng": ln})
    return found


# ---------------- aerial tiles ----------------

_ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
_USGS_EXPORT = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/export"
_MAPILLARY_IMAGES = "https://graph.mapillary.com/images"
_STREETVIEW = "https://maps.googleapis.com/maps/api/streetview"
STREET_SOURCES = ("mapillary", "streetview")


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


def _http_get(url: str, timeout: int = 20) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def _square_jpeg(data: bytes | None) -> bytes | None:
    """Center-crop a photo to a 512x512 JPEG; None for non-images/blank frames."""
    if not data or not data.startswith(b"\xff\xd8\xff"):
        return None
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None
    if _is_placeholder(img):
        return None
    w, h = img.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((512, 512))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def _bearing(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Compass bearing (degrees) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _dist_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    dy = (lat2 - lat1) * 111000.0
    dx = (lng2 - lng1) * 111000.0 * math.cos(math.radians(lat1))
    return math.hypot(dx, dy)


def _best_mapillary(lat: float, lng: float, images: list[dict],
                    max_m: float = 50.0, max_off: float = 45.0) -> dict | None:
    """Closest non-panoramic photo whose camera points at the house."""
    best, best_score = None, None
    for im in images:
        if im.get("is_pano") or not im.get("thumb_1024_url"):
            continue
        geom = im.get("computed_geometry") or im.get("geometry") or {}
        coords = geom.get("coordinates") or []
        heading = im.get("computed_compass_angle", im.get("compass_angle"))
        if len(coords) != 2 or heading is None:
            continue
        ilng, ilat = coords
        d = _dist_m(ilat, ilng, lat, lng)
        if d > max_m:
            continue
        off = abs((_bearing(ilat, ilng, lat, lng) - heading + 180.0) % 360.0
                  - 180.0)
        if off > max_off:
            continue
        score = d + off / 2.0
        if best_score is None or score < best_score:
            best, best_score = im, score
    return best


def _mapillary_image(lat: float, lng: float, radius_m: float = 50.0) -> bytes | None:
    """Street-level Mapillary photo facing the house (free; needs a token)."""
    token = (os.environ.get("MAPILLARY_ACCESS_TOKEN", "")
             or os.environ.get("MAPILLARY_TOKEN", ""))
    if not token:
        return None
    dlat = radius_m / 111000.0
    dlng = radius_m / (111000.0 * max(0.2, math.cos(math.radians(lat))))
    params = {
        "access_token": token,
        "fields": "id,thumb_1024_url,computed_geometry,geometry,"
                  "computed_compass_angle,compass_angle,is_pano",
        "bbox": f"{lng - dlng},{lat - dlat},{lng + dlng},{lat + dlat}",
        "limit": "50",
    }
    raw = _http_get(_MAPILLARY_IMAGES + "?" + urllib.parse.urlencode(params))
    if not raw:
        return None
    try:
        images = json.loads(raw.decode()).get("data") or []
    except Exception:
        return None
    pick = _best_mapillary(lat, lng, images, max_m=radius_m)
    if not pick:
        return None
    return _square_jpeg(_http_get(pick["thumb_1024_url"], timeout=30))


def _streetview_image(lat: float, lng: float) -> bytes | None:
    """Google Street View photo aimed at the house (paid; needs a key).

    The metadata lookup is free, so the billed image request only happens
    when a panorama actually exists near the point."""
    key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not key:
        return None
    loc = f"{lat},{lng}"
    meta = _http_get(_STREETVIEW + "/metadata?" + urllib.parse.urlencode(
        {"location": loc, "source": "outdoor", "key": key}))
    try:
        if not meta or json.loads(meta.decode()).get("status") != "OK":
            return None
    except Exception:
        return None
    url = _STREETVIEW + "?" + urllib.parse.urlencode({
        "location": loc, "size": "640x640", "fov": "80", "pitch": "20",
        "source": "outdoor", "return_error_code": "true", "key": key})
    return _square_jpeg(_http_get(url, timeout=30))


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
    photos. With no aerial view at all, try a street-level photo (Mapillary,
    then Google Street View) before giving up. Never grade a placeholder as
    if it were a roof. Street-level views report zoom 0.
    Returns (JPEG bytes or None, zoom used, imagery source).
    """
    for zz in (z, z - 1, z - 2):
        img = _centered_esri(lat, lng, zz)
        if img:
            return img, zz, "esri"
    usgs = _usgs_image(lat, lng)
    if usgs:
        return usgs, 18, "usgs"
    mly = _mapillary_image(lat, lng)
    if mly:
        return mly, 0, "mapillary"
    sv = _streetview_image(lat, lng)
    if sv:
        return sv, 0, "streetview"
    return None, z, "none"


# ---------------- vision grading ----------------

GRADE_PROMPT = """You are a senior forensic roof inspector grading residential roofs from aerial imagery, using the Roof Tru Scale (0-5). Grade ONLY the target property near the center of each image — never guess about age or materials beyond visible evidence.

5 SOLID — looks new or like-new: uniform color, crisp shingle lines, clean ridges, no visible wear.
4 HEALTHY — minor cosmetic aging only; clearly years of life left.
3 AGING — visible wear: patchy granule loss (shiny or mottled sheen), slight curling or lifting at edges, light moss or algae staining. Worth watching.
2 WORN — needs repair soon: missing, cracked, or lifted shingles; heavy moss or algae coverage; rusted or lifting flashing; debris buildup or ponding.
1 FAILING — needs replacement now: sagging or uneven roof deck, blue tarps, large bare or patched areas, collapsed sections, structural deformation.
0 UNVERIFIABLE — the target roof cannot be assessed (image too coarse to make out shingles, roof mostly out of frame, deep shadow, or tree canopy visibly covering the roof). Never guess; use 0. State the ACTUAL reason you cannot assess it — never default to tree cover.

ABANDONED / DERELICT RULE: if the target property is clearly abandoned or derelict — collapsed or fire-gutted structure, boarded-up and decaying, or a vacant lot with no building at the target point — set "abandoned": true and grade 0. Do not mark a merely old or worn-but-occupied home as abandoned.

STREET-LEVEL RULE: an image may be labeled as a street-level photo taken from the road instead of an aerial view. Grade only the roof slopes visible in it, never guess about hidden slopes, and set confidence no higher than "medium".

CRITICAL DISCRIMINATION RULES:
1. Solar panels: do NOT count solar arrays or mounting brackets as damage or discoloration. Grade only the exposed roof surface.
2. Shadows: differentiate sharp tree-limb shadows from sagging or missing shingles. Check whether the dark shape matches a tree next to the house.
3. Glare: high sun angles cause white reflective glare on metal or asphalt. Do not confuse glare with missing material.

For each image also report: primary_material \u2014 report ONLY the material you actually see (standing-seam metal, clay/concrete tile, slate, membrane/flat, or asphalt shingle). Do NOT default to asphalt shingle \u2014 only report it when you see shingle texture., pitch_estimate (Flat, Low-slope, Medium, or Steep), obstruction_notes — ONLY what is visibly blocking the target roof in THIS image (e.g. tree canopy over the roof, shadows, glare). If the roof is fully visible, use an empty string. NEVER write "tree cover" unless tree canopy is visibly covering part of the roof., and damage_boxes — bounding boxes as [ymin, xmin, ymax, xmax] in 0-1000 normalized coordinates around each visible damage area (missing shingles, tarps, ponding, etc.), with a short label per box. For grades 1-2 include at least one damage_box around the worst-affected area. Omit damage_boxes for healthy roofs. For grades 1-3 also report likely_cause \u2014 what most likely caused this damage, 1-2 sentences in plain homeowner language using visible clues (e.g. overhanging trees dropping water and debris on one slope, poor drainage, aging materials) \u2014 and prevention_tip \u2014 one concrete thing that would make the next roof last longer, 1 sentence in plain homeowner language.

The images are in order. Return a JSON array with exactly one object per image, in order: {"grade": 0-5, "abandoned": true/false, "confidence": "low|medium|high", "evidence": ["exactly 3 raw visual observations. Describe ONLY what your eyes see - colors, shapes, sizes, positions - as if to someone who cannot see the image. NO jargon, NO diagnosis, NO grade-rubric vocabulary. FORBIDDEN words (these are definitions, not observations): mottled, patchy, granule, granules, curling, curled, cupping, cupped, darkening, staining, stained, aging, aged, wear, worn, deterioration, deteriorated, weathering, weathered, blistering, alligatoring. Each bullet MUST include (a) a size or count and (b) an exact position relative to a roof feature (ridge, eave, chimney, valley, vent, dormer). BAD: 'mottled granule loss across the front slope'. GOOD: 'an uneven pale-gray patch roughly the size of a car door on the front slope, about 3 feet below the ridge, slightly left of center'. BAD: 'slight curling visible at the lower eave edge'. GOOD: 'six shingle tabs lifting at their bottom corners along a 10-foot stretch of the north eave'. Two different roofs must never receive identical evidence."], "primary_material": "...", "pitch_estimate": "...", "obstruction_notes": "...", "damage_boxes": [{"box_2d": [ymin,xmin,ymax,xmax], "label": "..."}], "likely_cause": "...", "prevention_tip": "..."}. Return ONLY the JSON array."""

_GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
               "gemini-3.1-flash-lite:generateContent")


def _gemini_call(api_key: str, image_b64_list: list[str],
                 sources: list[str] | None = None) -> str | None:
    parts: list[dict] = [{"text": GRADE_PROMPT}]
    for i, b64 in enumerate(image_b64_list):
        if sources and i < len(sources) and sources[i] in STREET_SOURCES:
            parts.append({"text": f"Image {i + 1}: street-level photo "
                                  "taken from the road."})
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
                                             "description": "Visible roofing material: metal, clay/concrete tile, slate, membrane, or asphalt shingle. Do not default to asphalt shingle; report what is visible."},
                        "pitch_estimate": {"type": "STRING",
                                           "description": "Flat, Low-slope, Medium, or Steep"},
                        "obstruction_notes": {"type": "STRING",
                                              "description": "Tree shadows, solar panels, canopy coverage, glare; empty string if clear"},
                        "likely_cause": {"type": "STRING",
                                         "description": "What most likely caused this damage, 1-2 sentences in plain homeowner language. Use visible clues: overhanging trees dropping water and debris on one slope, poor drainage or ponding, aging materials, storm damage, etc."},
                        "prevention_tip": {"type": "STRING",
                                           "description": "One concrete thing that would make the next roof last longer, 1 sentence in plain homeowner language. E.g. 'Trim branches back 6+ feet from the roofline so leaves and water don't sit on the shingles.'"},
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
                        "abandoned": bool(g.get("abandoned", False)),
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


def _gemini_text(api_key: str, system_text: str, user_text: str,
                 max_tokens: int = 800) -> str | None:
    """Single Gemini text chat call with a system instruction (or None)."""
    payload = {
        "system_instruction": {"parts": [{"text": system_text}]},
        "contents": [{"parts": [{"text": user_text}]}],
        "generationConfig": {
            "temperature": 0.4, "maxOutputTokens": max_tokens,
        },
    }
    req = urllib.request.Request(
        _GEMINI_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode())
        cands = data.get("candidates") or []
        txt = "".join(p.get("text", "") for p in
                      cands[0]["content"]["parts"] if p.get("text")).strip()
        return txt or None
    except Exception:
        return None


PINPOINT_PROMPT = """You are a senior forensic roof inspector. This {view} shows ONE roof, centered in the frame, graded {grade}/5 on the Roof Tru Scale ({verdict}). Wide-view evidence: {evidence}

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
            street = h.get("imagery") in STREET_SOURCES
            closeup = None
            if not street:
                z = min((h.get("zoom") or 19) + 1, 20)
                closeup = _centered_esri(h["lat"], h["lng"], z)
            if not closeup:
                closeup = base64.b64decode(h["image_b64"])
            b64 = base64.b64encode(closeup).decode()
            verdict = "FAILING — needs replacement" if h["grade"] == 1 \
                else "WORN — needs repair soon"
            prompt = PINPOINT_PROMPT.format(
                view=("street-level photo taken from the road" if street
                      else "close-up aerial image"),
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

    Only roofs graded 1-3 get reverse-geocoded — healthy roofs don't need
    a letter. Resolution order (all free, no keys):
      1. OSM addr:housenumber/addr:street tags (verified, kept as-is)
      2. Nominatim reverse (verified when it returns house_number + road,
         otherwise a street-level guess marked estimated)
      3. Photon reverse (separate OSM index, fallback guess, estimated)
    US Census TIGER geographies fill city/county/state authoritatively on
    every lead. Owner's rule: an educated guess beats a blank address, but
    every guess is labeled estimated so roofers know to verify before
    mailing. Nothing is invented from thin air — each guess comes from a
    real geocoder response for those coordinates."""
    # OSM-tagged addresses are verified; label them so the UI can show it.
    for h in houses:
        if h.get("address") and not h.get("address_confidence"):
            h["address_confidence"] = "verified"
            h.setdefault("address_source", "map_tags")
    targets = [h for h in houses
               if h.get("grade") in (1, 2, 3) and not h.get("address")]
    total = len(targets)
    for i, h in enumerate(targets):
        if progress:
            progress("addresses", i, total,
                     f"Matching addresses… {i}/{total}")
        try:
            _resolve_address(h)
        except Exception:
            pass
        time.sleep(1.05)  # be polite to the free geocoders
    if progress:
        matched = sum(1 for h in targets if h.get("address"))
        progress("addresses", total, total,
                 f"Matched {matched} address(es).")
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
            imgs = [c["image_b64"] for c in chunk]
            srcs = [c.get("imagery", "") for c in chunk]
            txt = _gemini_call(api_key, imgs, srcs)
            grades = _parse_grades(txt, len(chunk))
            if grades is None:  # one retry
                txt = _gemini_call(api_key, imgs, srcs)
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
            # Normalize raw grader keys so the test hook and the production
            # path behave identically downstream.
            _normalize_grade_keys(g)
            h.update(g)
        else:
            h.update({"grade": 0, "abandoned": False, "confidence": "low",
                      "evidence": ["grading unavailable"],
                      "material": "", "pitch": "", "obstruction": "",
                      "damage_boxes": []})
        v = obscured_verdict(h)
        if v:
            h.update(v)
    return houses


def _normalize_grade_keys(g: dict) -> None:
    """Map raw grader keys to the canonical mapped names, in place.

    The test grader hook returns raw model keys ("obstruction_notes") while
    the production path maps them ("obstruction"). Normalize so a street
    re-grade fully replaces the aerial values regardless of path."""
    for raw, canon in (("primary_material", "material"),
                       ("pitch_estimate", "pitch"),
                       ("obstruction_notes", "obstruction")):
        if raw in g:
            if canon not in g:
                g[canon] = g.pop(raw)
            else:
                g.pop(raw)


def street_second_opinion(api_key: str, houses: list[dict], progress=None,
                          grader=None) -> None:
    """Second opinion from the road.

    Aerial imagery can't see through tree cover, and a roof that looks fine
    from directly above can show damage on its street-facing slopes. For
    houses the aerial grader scored 0 (unverifiable) or 4-5 (healthy from
    above), fetch a street-level photo (Mapillary is free; Google Street
    View's metadata check is free and the image is ~$0.007) and re-grade it.
    Houses scoring 1-3 from the street are updated in place — grade,
    evidence, imagery source, and image all become the street-level ones —
    so the normal grade filter downstream picks them up as leads.
    """
    cands = [h for h in houses
             if h.get("grade") in (0, 4, 5) and h.get("lat") is not None
             and h.get("lng") is not None]
    if not cands or (not api_key and grader is None):
        return
    if progress:
        progress("streetview", 0, len(cands),
                 f"Checking street-level views… 0/{len(cands)}")

    def grab(h):
        img = _mapillary_image(h["lat"], h["lng"])
        src = "mapillary"
        if not img:
            img = _streetview_image(h["lat"], h["lng"])
            src = "streetview"
        if img:
            h["_street_b64"] = base64.b64encode(img).decode()
            h["_street_src"] = src
        return h

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(grab, cands))
    with_street = [h for h in cands if h.get("_street_b64")]
    if progress:
        progress("streetview", len(cands) - len(with_street), len(cands),
                 f"Grading street-level views…")
    if not with_street:
        return
    tmp = [{"key": h["key"], "image_b64": h.pop("_street_b64"),
            "imagery": h.pop("_street_src")} for h in with_street]
    grade_roofs(api_key, tmp, progress, grader=grader)
    n = 0
    for h, t in zip(with_street, tmp):
        if t.get("grade") in (1, 2, 3):
            _normalize_grade_keys(t)
            for k in ("grade", "confidence", "evidence", "material",
                      "pitch", "obstruction", "damage_boxes", "abandoned"):
                if k in t:
                    h[k] = t[k]
            # The street photo proved the roof assessable — drop any stale
            # "tree-obscured" verdict attached during the aerial pass so the
            # card never claims tree cover on a clearly-visible property.
            # Also drop leftover raw keys so stale aerial values can't linger
            # next to the fresh street data.
            h.pop("verdict", None)
            h.pop("verdict_note", None)
            h.pop("primary_material", None)
            h.pop("pitch_estimate", None)
            h.pop("obstruction_notes", None)
            h["image_b64"] = t["image_b64"]
            h["imagery"] = t["imagery"]
            h["zoom"] = 0
            h["street_second_opinion"] = True
            n += 1
    if progress:
        progress("streetview", len(cands), len(cands),
                 f"Street-level check found {n} more damaged roof(s)")


PRESCREEN_PROMPT = """You are a roof triage assistant. For EACH image in order (most are aerial views; a few may be street-level photos of the house), reply with ONLY a JSON array of integers — one per image — rating visible roof condition: 1 = pristine, 2 = normal aging only, 3 = visible wear and tear (granule loss, curling or faded shingles, moss, patching), 4 = clear damage (missing shingles, exposed underlayment, sagging, tarp covering part of the roof), 5 = severe damage or failure (large tarped areas, collapsed sections). A blue or black tarp on a roof is emergency storm-damage covering — it IS damage, score it 4 or 5, NEVER 0. Use 0 only when no roof is visible at all (heavy tree cover, no building visible). Solar panels are NOT damage. Example: [2,0,4]"""


def _prescreen_call(api_key: str, b64_list: list[str]) -> list[int] | None:
    parts = [{"text": PRESCREEN_PROMPT}] + [
        {"inline_data": {"mime_type": "image/jpeg", "data": b}}
        for b in b64_list]
    schema = {"type": "ARRAY", "items": {"type": "INTEGER"}}
    try:
        out = _gemini_json(api_key, parts, schema, max_tokens=256)
    except Exception:
        return None
    if not isinstance(out, list):
        return None
    vals = []
    for v in out:
        try:
            vals.append(max(0, min(5, int(v))))
        except (TypeError, ValueError):
            vals.append(0)
    return vals


def _stride_fallback(cands: list[dict], count: int) -> list[dict]:
    if len(cands) <= count:
        return list(cands)
    stride = len(cands) / count
    return [cands[int(i * stride)] for i in range(count)]


def prescreen_damage(api_key: str, cands: list[dict], count: int,
                     progress=None, prescreener=None) -> list[dict]:
    """Cheap triage: micro-scan the wide pool, deep-scan only the damaged.

    Fetches a z=19 crop per candidate and scores damage 1 (pristine) to 5
    (severe) with one tiny Gemini call per 8 roofs. Returns at most `count`
    candidates with a damage score >= 3 (wear and tear or worse), worst
    first — pristine/aging (1-2) and unverifiable (0) roofs never earn the expensive
    deep scan. Buildings flagged vacant/abandoned in the public map records
    keep their `vacant` flag through triage so the deep scan can confirm or
    clear them — they are flagged for review, never silently dropped.
    Falls back to geographic stride when triage fails. prescreener() is a
    test hook like grader().
    """
    total = len(cands)
    if total == 0 or count <= 0:
        return []
    # Every candidate carries the vacant flag (grid top-up points never had
    # OSM tags to read), so the review flag survives triage uniformly.
    for c in cands:
        c.setdefault("vacant", False)
    if progress:
        progress("prescreen", 0, total, f"Pre-screening {total} roofs…")

    def grab(c):
        img, _zoom, _src = roof_image(c["lat"], c["lng"], z=19)
        if img:
            c["image_b64"] = base64.b64encode(img).decode()
        return c

    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(grab, cands))
    with_img = [c for c in cands if c.get("image_b64")]
    if not with_img:
        return _stride_fallback(cands, count)

    index_of = {id(c): i for i, c in enumerate(cands)}
    scored: dict[int, int] = {}
    batches = [with_img[i:i + 8] for i in range(0, len(with_img), 8)]
    done = [0]
    lock = threading.Lock()

    def score(b64s):
        vals = prescreener(b64s) if prescreener else _prescreen_call(api_key, b64s)
        return vals if vals and len(vals) == len(b64s) else None

    def triage(batch):
        b64s = [c["image_b64"] for c in batch]
        vals = score(b64s) or score(b64s)  # one retry
        if vals is None and len(b64s) > 1:
            # A miscounted reply can't be aligned to its images; score each
            # image alone rather than shift scores onto the wrong roofs.
            vals = [(score([b]) or [None])[0] for b in b64s]
        with lock:
            for c, v in zip(batch, vals or []):
                if v is not None:
                    scored[index_of[id(c)]] = v
            done[0] += len(batch)
            if progress:
                progress("prescreen", done[0], total,
                         f"Pre-screening roofs… {done[0]}/{total}")

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(triage, batches))
    if not scored:
        return _stride_fallback(cands, count)
    for i, v in scored.items():
        cands[i]["micro_score"] = v
    # Worst first, but only visibly damaged roofs (score >= 3) earn the
    # deep scan. Pristine/minor (1-2) and unverifiable (0) never do.
    ranked = sorted(scored, key=lambda i: -scored[i])
    damaged = [i for i in ranked if scored[i] >= 3]
    picked_idx = damaged[:count]
    # Backfill guarantee: every scan deep-evaluates exactly `count`
    # properties. Damaged roofs go first (worst first); any remaining
    # slots are filled with the next-best triage scores so the customer
    # always gets the full count they asked for. Backfilled healthy roofs
    # grade out in the deep scan -- they never become leads.
    if len(picked_idx) < count:
        picked_set = set(picked_idx)
        for i in ranked:
            if len(picked_idx) >= count:
                break
            if i not in picked_set:
                picked_idx.append(i)
                picked_set.add(i)
    picked = [cands[i] for i in picked_idx]
    for c in picked:
        c.pop("image_b64", None)
    n_damaged = sum(1 for i in picked_idx if scored[i] >= 3)
    if progress:
        progress("prescreen", 1, 1,
                 f"{len(picked)} roofs selected for deep-dive "
                 f"({n_damaged} flagged damaged)")
    return picked


_GRADE_ORDER = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 0: 5}


_TREE_WORDS = ("tree", "canopy", "foliage", "woods", "vegetation")
# The notes must say the trees HIDE the roof — a nearby tree that isn't
# blocking anything must not trigger the verdict.
_HIDE_WORDS = ("hid", "cover", "obscur", "block", "beneath", "under",
               "behind", "over")


def obscured_verdict(house: dict) -> dict | None:
    """Plain-language verdict when a roof can't be assessed at all.

    Grade 0 + low confidence with tree/canopy actively hiding the roof
    means the imagery is useless — say so plainly instead of handing back
    a bare 0. Requires the notes to say the trees hide/cover the roof, not
    merely mention one nearby, so a clearly-visible roof is never
    mislabeled as tree-obscured.
    """
    if house.get("grade") != 0 or house.get("confidence") != "low":
        return None
    text = " ".join(
        [str(house.get("obstruction") or "")] +
        [str(e) for e in (house.get("evidence") or [])]
    ).lower()
    if (any(w in text for w in _TREE_WORDS)
            and any(w in text for w in _HIDE_WORDS)):
        return {"verdict": "tree-obscured",
                "verdict_note": ("Tree cover hides this roof — it can't be graded "
                                 "from the current aerial imagery. Try leaf-off "
                                 "(winter) imagery or an on-site look.")}
    return None


def sort_leads(houses: list[dict]) -> list[dict]:
    # Worst first; properties flagged possibly-abandoned sort after the
    # clean leads (worst-first within each group) so a fake lead is never
    # presented as a good one.
    return sorted(houses, key=lambda h: (bool(h.get("needs_review")),
                                         _GRADE_ORDER.get(h.get("grade", 0), 5),
                                         h.get("address", "")))
