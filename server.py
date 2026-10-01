"""RoofScout server: zip in, ranked roof leads out."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid

from flask import Flask, jsonify, request, send_from_directory

import pipeline

app = Flask(__name__, static_folder="static", static_url_path="/static")

def _default_db_path():
    env = os.environ.get("DB_PATH", "")
    if env:
        return env
    if os.path.isdir("/data"):
        return "/data/roofscout.db"
    return os.path.join(os.path.dirname(__file__), "roofscout.db")


DB_PATH = _default_db_path()
CACHE_TTL = 7 * 24 * 3600
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
OWNER_EMAIL = "mtthew.westfall@gmail.com"
ALLOWED_TEST_EMAILS = {"mtthew.westfall@gmail.com", "mtthew.westfall@gmaiI.com", "mtthew.westfall@gmaii.com"}
SESSION_DAYS = 30

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS scans (
        zip TEXT NOT NULL, count INTEGER NOT NULL, payload TEXT NOT NULL,
        created_at REAL NOT NULL, PRIMARY KEY (zip, count))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,
        pw_hash TEXT NOT NULL, salt TEXT NOT NULL,
        account_type TEXT NOT NULL DEFAULT 'individual',
        company_name TEXT NOT NULL DEFAULT '',
        is_admin INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY, user_id TEXT NOT NULL,
        created_at REAL NOT NULL, expires_at REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS admin_sessions (
        token TEXT PRIMARY KEY, created_at REAL NOT NULL,
        expires_at REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS unlocks (
        user_id TEXT NOT NULL, lead_key TEXT NOT NULL,
        unlocked_at REAL NOT NULL,
        PRIMARY KEY (user_id, lead_key))""")
    # Trial/plan columns (added after launch; migrate old DBs in place).
    for col in (
            "plan TEXT NOT NULL DEFAULT 'trial'",
            "trial_scans_used INTEGER NOT NULL DEFAULT 0",
            "trial_unlocks_used INTEGER NOT NULL DEFAULT 0",
            "cycle_scans_used INTEGER NOT NULL DEFAULT 0",
            "cycle_unlocks_used INTEGER NOT NULL DEFAULT 0",
            "period_start REAL NOT NULL DEFAULT 0"):
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
    conn.commit()
    return conn


# ---------------- plans & trial quotas ----------------

PLANS = {
    "trial":   {"name": "Trial",   "scans": 1, "unlocks": 5,   "cycle_days": 0},
    "starter": {"name": "Starter", "scans": 2, "unlocks": 25,  "cycle_days": 30},
    "pro":     {"name": "Pro",     "scans": 8, "unlocks": 100, "cycle_days": 30},
}


def _quota(conn, user_id: str, is_admin: bool = False) -> dict:
    """Current plan usage. Admins are unlimited."""
    row = conn.execute(
        "SELECT plan, trial_scans_used, trial_unlocks_used,"
        " cycle_scans_used, period_start, cycle_unlocks_used"
        " FROM users WHERE id=?",
        (user_id,)).fetchone()
    plan = (row[0] if row else "trial") or "trial"
    if plan not in PLANS:
        plan = "trial"
    spec = PLANS[plan]
    now = time.time()
    if is_admin:
        return {"plan": plan, "plan_name": "Admin",
                "scans_left": -1, "unlocks_left": -1,
                "scans_used": 0, "unlocks_used": 0,
                "scans_cap": -1, "unlocks_cap": -1, "cycle_ends": 0}
    if spec["cycle_days"]:
        period_start = row[4] or 0
        if now - period_start > spec["cycle_days"] * 86400:
            conn.execute("UPDATE users SET cycle_scans_used=0,"
                         " cycle_unlocks_used=0, period_start=?"
                         " WHERE id=?", (now, user_id))
            conn.commit()
            scans_used, unlocks_used, period_start = 0, 0, now
        else:
            scans_used = row[3] or 0
            unlocks_used = row[5] or 0
        return {"plan": plan, "plan_name": spec["name"],
                "scans_left": max(0, spec["scans"] - scans_used),
                "unlocks_left": max(0, spec["unlocks"] - unlocks_used),
                "scans_used": scans_used, "unlocks_used": unlocks_used,
                "scans_cap": spec["scans"], "unlocks_cap": spec["unlocks"],
                "cycle_ends": period_start + spec["cycle_days"] * 86400}
    return {"plan": plan, "plan_name": spec["name"],
            "scans_left": max(0, spec["scans"] - (row[1] or 0)),
            "unlocks_left": max(0, spec["unlocks"] - (row[2] or 0)),
            "scans_used": row[1] or 0, "unlocks_used": row[2] or 0,
            "scans_cap": spec["scans"], "unlocks_cap": spec["unlocks"],
            "cycle_ends": 0}


def _consume_scan(conn, user_id: str, is_admin: bool):
    row = conn.execute("SELECT plan FROM users WHERE id=?",
                       (user_id,)).fetchone()
    plan = (row[0] if row else "trial") or "trial"
    if is_admin:
        return
    if plan in PLANS and PLANS[plan]["cycle_days"]:
        if not conn.execute("SELECT period_start FROM users WHERE id=?",
                            (user_id,)).fetchone()[0]:
            conn.execute("UPDATE users SET period_start=? WHERE id=?",
                         (time.time(), user_id))
        conn.execute("UPDATE users SET cycle_scans_used=cycle_scans_used+1"
                     " WHERE id=?", (user_id,))
    else:
        conn.execute("UPDATE users SET trial_scans_used=trial_scans_used+1"
                     " WHERE id=?", (user_id,))
    conn.commit()


def _consume_unlock(conn, user_id: str, is_admin: bool):
    row = conn.execute("SELECT plan FROM users WHERE id=?",
                       (user_id,)).fetchone()
    plan = (row[0] if row else "trial") or "trial"
    if is_admin:
        return
    if plan in PLANS and PLANS[plan]["cycle_days"]:
        if not conn.execute("SELECT period_start FROM users WHERE id=?",
                            (user_id,)).fetchone()[0]:
            conn.execute("UPDATE users SET period_start=? WHERE id=?",
                         (time.time(), user_id))
        conn.execute("UPDATE users SET cycle_unlocks_used=cycle_unlocks_used+1"
                     " WHERE id=?", (user_id,))
    else:
        conn.execute("UPDATE users SET trial_unlocks_used=trial_unlocks_used+1"
                     " WHERE id=?", (user_id,))
    conn.commit()


def _lead_key(lead: dict) -> str:
    base = "|".join([str(lead.get("address", "")),
                     str(lead.get("postcode", "")),
                     f"{float(lead.get('lat') or 0):.5f}",
                     f"{float(lead.get('lng') or 0):.5f}"])
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def _mask_address(addr: str) -> str:
    words = []
    for p in (addr or "").split():
        words.append(p[0] + chr(8226) * max(len(p) - 1, 0) if p else p)
    return " ".join(words)


def _jitter(lat: float, lng: float, key: str) -> tuple[float, float]:
    h = hashlib.sha256(key.encode()).digest()
    dlat = (int.from_bytes(h[:4], "big") / 2**32 - 0.5) * 0.006
    dlng = (int.from_bytes(h[4:8], "big") / 2**32 - 0.5) * 0.006
    return round(lat + dlat, 5), round(lng + dlng, 5)


def _unlocked_keys(conn, user_id: str) -> set:
    return {r[0] for r in
            conn.execute("SELECT lead_key FROM unlocks WHERE user_id=?",
                         (user_id,))}


def _shape_leads(conn, leads: list[dict], user: dict,
                 single: bool = False) -> list[dict]:
    """Mask addresses + jitter pins until a lead is unlocked (all plans)."""
    if user.get("is_admin") or single:
        return [{**l, "lead_key": _lead_key(l), "locked": False}
                for l in leads]
    unlocked = _unlocked_keys(conn, user["id"])
    out = []
    for l in leads:
        key = _lead_key(l)
        if key in unlocked:
            out.append({**l, "lead_key": key, "locked": False})
            continue
        c = dict(l)
        c["lead_key"] = key
        c["locked"] = True
        c["address"] = _mask_address(l.get("address", ""))
        c.pop("maps_url", None)
        c.pop("streetview_url", None)
        try:
            c["lat"], c["lng"] = _jitter(float(l.get("lat") or 0),
                                        float(l.get("lng") or 0), key)
        except Exception:
            pass
        out.append(c)
    return out


def _find_lead(lead_key: str) -> dict | None:
    """Locate a lead's full record in memory or the scan cache."""
    with _jobs_lock:
        jobs = list(_jobs.values())
    for job in jobs:
        for l in job.get("leads") or []:
            if _lead_key(l) == lead_key:
                return l
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT payload FROM scans ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    finally:
        conn.close()
    for (payload,) in rows:
        try:
            for l in json.loads(payload).get("leads") or []:
                if _lead_key(l) == lead_key:
                    return l
        except Exception:
            continue
    return None


# ---------------- auth ----------------

def _hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(),
                               salt.encode(), 210_000).hex()


def _set_session_cookie(resp, token: str):
    secure = request.headers.get("X-Forwarded-Proto", "") == "https" \
        or request.is_secure
    resp.set_cookie("rs_session", token, max_age=SESSION_DAYS * 86400,
                    httponly=True, samesite="Lax", secure=secure, path="/")


def _new_session(conn, user_id: str) -> str:
    token = secrets.token_hex(32)
    now = time.time()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    conn.execute("INSERT INTO sessions (token, user_id, created_at, expires_at)"
                 " VALUES (?,?,?,?)",
                 (token, user_id, now, now + SESSION_DAYS * 86400))
    conn.commit()
    return token


def _current_user():
    token = request.cookies.get("rs_session", "")
    if not token:
        return None
    conn = _db()
    try:
        row = conn.execute(
            "SELECT u.id, u.email, u.account_type, u.company_name, u.is_admin"
            " FROM sessions s JOIN users u ON s.user_id = u.id"
            " WHERE s.token = ? AND s.expires_at > ?",
            (token, time.time())).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    user = {"id": row[0], "email": row[1], "account_type": row[2],
            "company_name": row[3], "is_admin": bool(row[4])}
    conn2 = _db()
    try:
        user["quota"] = _quota(conn2, user["id"], user["is_admin"])
    finally:
        conn2.close()
    return user


def _require_user():
    user = _current_user()
    if not user:
        return None, (jsonify({"ok": False, "error": "login_required"}), 401)
    return user, None


def _valid_email(email: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email or ""))


_COMPANY_SUFFIXES = {
    "llc", "inc", "incorporated", "corp", "corporation", "co", "company",
    "ltd", "limited", "llp", "pllc", "pa", "lp", "pllp",
}


def _normalize_company(name: str) -> str:
    """Lowercase, strip punctuation/extra spaces, drop trailing legal
    suffixes (LLC, Inc, Co, ...). Used to catch the same company signing
    up for a second trial under a different email. Conservative: only
    exact normalized matches are treated as the same company."""
    words = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split()
    while words and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


@app.post("/api/auth/signup")
def signup():
    body = request.get_json(force=True, silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    account_type = str(body.get("account_type", "individual")).strip().lower()
    company_name = str(body.get("company_name", "")).strip()
    if not _valid_email(email):
        return jsonify({"ok": False, "error": "Enter a valid email address."}), 400
    if len(password) < 8:
        return jsonify({"ok": False,
                        "error": "Password must be at least 8 characters."}), 400
    if account_type not in ("individual", "company"):
        return jsonify({"ok": False, "error": "Pick individual or company."}), 400
    if account_type == "company" and not company_name:
        return jsonify({"ok": False,
                        "error": "Enter your company name."}), 400
    user_id = uuid.uuid4().hex
    salt = secrets.token_hex(16)
    conn = _db()
    try:
        if conn.execute("SELECT 1 FROM users WHERE email=?",
                        (email,)).fetchone():
            return jsonify({"ok": False,
                            "error": "That email already has an account. Try logging in."}), 400
        if account_type == "company":
            # One trial per company: block a second signup whose company
            # name normalizes to one already on file, even with a new email.
            norm = _normalize_company(company_name)
            if norm:
                existing = conn.execute(
                    "SELECT company_name FROM users"
                    " WHERE account_type='company'").fetchall()
                if any(_normalize_company(e[0]) == norm for e in existing):
                    return jsonify({"ok": False, "error":
                        "This company already has an account. Try logging in,"
                        " or contact support to add another seat."}), 400
        is_admin = 1 if email in ALLOWED_TEST_EMAILS else 0
        conn.execute(
            "INSERT INTO users (id, email, pw_hash, salt, account_type,"
            " company_name, is_admin, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, email, _hash_pw(password, salt), salt, account_type,
             company_name, is_admin, time.time()))
        token = _new_session(conn, user_id)
        quota = _quota(conn, user_id, bool(is_admin))
    finally:
        conn.close()
    resp = jsonify({"ok": True, "user": {"email": email,
                                        "account_type": account_type,
                                        "company_name": company_name,
                                        "is_admin": bool(is_admin),
                                        "quota": quota}})
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/auth/login")
def login():
    body = request.get_json(force=True, silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    conn = _db()
    try:
        row = conn.execute(
            "SELECT id, pw_hash, salt, email, account_type, company_name,"
            " is_admin FROM users WHERE email=?", (email,)).fetchone()
        if not row or _hash_pw(password, row[2]) != row[1]:
            return jsonify({"ok": False,
                            "error": "Invalid email or password."}), 401
        token = _new_session(conn, row[0])
        user = {"email": row[3], "account_type": row[4],
                "company_name": row[5], "is_admin": bool(row[6])}
        user["quota"] = _quota(conn, row[0], user["is_admin"])
    finally:
        conn.close()
    resp = jsonify({"ok": True, "user": user})
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/auth/logout")
def logout():
    token = request.cookies.get("rs_session", "")
    conn = _db()
    try:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
    finally:
        conn.close()
    resp = jsonify({"ok": True})
    resp.delete_cookie("rs_session", path="/")
    return resp


@app.get("/api/auth/me")
def me():
    user = _current_user()
    if not user:
        return jsonify({"ok": False, "error": "login_required"}), 401
    return jsonify({"ok": True, "user": user})


def _cache_get(zipcode: str, count: int):
    conn = _db()
    try:
        row = conn.execute(
            "SELECT payload, created_at FROM scans WHERE zip=? AND count=?",
            (zipcode, count)).fetchone()
    finally:
        conn.close()
    if row and time.time() - row[1] < CACHE_TTL:
        return json.loads(row[0])
    return None


def _cache_put(zipcode: str, count: int, payload: dict):
    conn = _db()
    try:
        conn.execute("REPLACE INTO scans (zip, count, payload, created_at)"
                     " VALUES (?,?,?,?)",
                     (zipcode, count, json.dumps(payload), time.time()))
        conn.commit()
    finally:
        conn.close()


def _set_job(job_id: str, **kw):
    with _jobs_lock:
        _jobs[job_id].update(kw)


def _run_houses(job_id: str, houses: list[dict], area: str,
               cache: tuple | None = None, grader=None):
    """Imagery + grading + sort tail, shared by zip and single-address scans."""
    try:
        def progress(phase, done, total, msg):
            _set_job(job_id, phase=phase, done=done, total=total, msg=msg)

        _set_job(job_id, phase="imagery", done=0, total=len(houses),
                 msg="Pulling aerial views…")

        def grab(h):
            img, zoom, src = pipeline.roof_image(h["lat"], h["lng"])
            if img:
                import base64
                h["image_b64"] = base64.b64encode(img).decode()
                h["zoom"] = zoom
                h["imagery"] = src
            return h
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(grab, houses))
        houses = [h for h in houses if h.get("image_b64")]
        if not houses:
            _set_job(job_id, status="error",
                     error="Aerial imagery unavailable right now. Try again in a bit.")
            return

        if not GEMINI_KEY and grader is None:
            _set_job(job_id, status="error",
                     error="Grader not configured (missing API key).")
            return
        pipeline.grade_roofs(GEMINI_KEY, houses, progress, grader=grader)

        # Pinpoint pass: zoomed damage close-up + repair breakdown for
        # damaged roofs (needs the graded image still in memory).
        if GEMINI_KEY and grader is None:
            pipeline.localize_damage(GEMINI_KEY, houses, progress)
        # Roof-first matching: damaged roofs get mailable street addresses.
        pipeline.attach_addresses(houses, progress)

        for h in houses:
            b64 = h.pop("image_b64", None)
            h["img"] = f"data:image/jpeg;base64,{b64}" if b64 else ""
            h["maps_url"] = ("https://www.google.com/maps/search/?api=1&query="
                             f"{h['lat']},{h['lng']}")
            h["streetview_url"] = ("https://www.google.com/maps/@?api=1&map_action=pano"
                                   f"&viewpoint={h['lat']},{h['lng']}")
        leads = pipeline.sort_leads(houses)
        payload = {"zip": cache[0] if cache else "", "area": area,
                   "leads": leads, "scanned_at": time.time()}
        if cache:
            _cache_put(cache[0], cache[1], payload)
        _set_job(job_id, status="done", leads=leads, area=area,
                 msg=f"Done — {len(leads)} roofs graded.")
    except Exception as e:
        _set_job(job_id, status="error", error=f"Scan failed: {e}")


def _run_scan(job_id: str, zipcode: str, count: int, grader=None):
    try:
        def progress(phase, done, total, msg):
            _set_job(job_id, phase=phase, done=done, total=total, msg=msg)

        _set_job(job_id, phase="ziplookup", msg="Locating zip code…")
        center = pipeline.zip_center(zipcode)
        if not center:
            _set_job(job_id, status="error",
                     error="Couldn't find that zip code. Try a valid 5-digit US zip.")
            return
        _set_job(job_id, area=center[2])

        houses = pipeline.sample_roofs(zipcode, center, count, progress)
        if not houses:
            _set_job(job_id, status="error",
                     error="No addresses found near that zip. Try another.")
            return
        for h in houses:
            h["key"] = h["address"] + "|" + h["postcode"]
        _run_houses(job_id, houses, center[2], cache=(zipcode, count),
                    grader=grader)
    except Exception as e:
        _set_job(job_id, status="error", error=f"Scan failed: {e}")


def _run_address_scan(job_id: str, house: dict, grader=None):
    try:
        house["key"] = house["address"] + "|" + house["postcode"]
        area = ", ".join(x for x in (house.get("city"),
                                     house.get("state")) if x) or house["address"]
        _set_job(job_id, phase="ziplookup",
                 msg=f"Located {house['address']}…", area=area)
        _run_houses(job_id, [house], area, cache=None, grader=grader)
    except Exception as e:
        _set_job(job_id, status="error", error=f"Scan failed: {e}")


@app.post("/api/scan")
def start_scan():
    user, err = _require_user()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    raw = str(body.get("q", body.get("zip", ""))).strip()
    try:
        count = max(5, min(30, int(body.get("count", 20))))
    except Exception:
        count = 20

    def quota_ok():
        conn = _db()
        try:
            q = _quota(conn, user["id"], user["is_admin"])
        finally:
            conn.close()
        if user["is_admin"]:
            return None
        if q["scans_left"] <= 0:
            return (jsonify({"ok": False, "error": "trial_scans_exhausted",
                             "quota": q}), 402)
        return None

    def use_scan():
        conn = _db()
        try:
            _consume_scan(conn, user["id"], user["is_admin"])
            q = _quota(conn, user["id"], user["is_admin"])
        finally:
            conn.close()
        return q

    if re.fullmatch(r"\d{5}", raw):
        zipcode = raw
        # Quota first: a cached hit still costs one scan. Check allowance
        # and consume BEFORE serving the cached payload.
        blocked = quota_ok()
        if blocked:
            return blocked
        quota = use_scan()
        cached = _cache_get(zipcode, count)
        if cached:
            conn = _db()
            try:
                leads = _shape_leads(conn, cached.get("leads") or [], user)
            finally:
                conn.close()
            return jsonify({"ok": True, "cached": True, "quota": quota,
                            "payload": {"zip": cached.get("zip"),
                                        "area": cached.get("area"),
                                        "leads": leads,
                                        "scanned_at": cached.get("scanned_at")}})
        job_id = uuid.uuid4().hex[:12]
        with _jobs_lock:
            _jobs[job_id] = {"status": "running", "phase": "start", "done": 0,
                             "total": count, "msg": "Starting…", "zip": zipcode,
                             "single": False, "owner": user["id"]}
        t = threading.Thread(target=_run_scan, args=(job_id, zipcode, count),
                             daemon=True)
        t.start()
        return jsonify({"ok": True, "job_id": job_id, "quota": quota})

    # ...otherwise treat it as a typed street address (interchangeable input)
    if len(raw) < 5:
        return jsonify({"ok": False,
                        "error": "Enter a 5-digit zip or a street address."}), 400
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", raw)
    if m:
        # Pasted coordinates, e.g. "39.4781,-80.19354".
        lat, lng = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return jsonify({"ok": False,
                            "error": "Coordinates out of range."}), 400
        house = pipeline.geocode_latlng(lat, lng)
    else:
        house = pipeline.geocode_address(raw)
    if not house:
        return jsonify({"ok": False, "error":
                        f"Couldn't locate “{raw}”. Try a full street address "
                        "with city and state, or paste coordinates like "
                        "39.4781,-80.19354."}), 400
    blocked = quota_ok()
    if blocked:
        return blocked
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "phase": "start", "done": 0,
                         "total": 1, "msg": "Starting…",
                         "zip": house.get("postcode", ""),
                         "single": True, "owner": user["id"]}
    quota = use_scan()
    t = threading.Thread(target=_run_address_scan, args=(job_id, house),
                         daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id, "quota": quota})


@app.get("/api/scan/<job_id>")
def scan_status(job_id: str):
    user, err = _require_user()
    if err:
        return err
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job"}), 404
    # Account-scoped: only the job's owner (or an admin) may read it.
    if not user.get("is_admin") and job.get("owner") != user["id"]:
        return jsonify({"ok": False, "error": "forbidden"}), 403
    out = {"ok": True, "status": job["status"],
           "phase": job.get("phase"), "done": job.get("done", 0),
           "total": job.get("total", 0), "msg": job.get("msg", "")}
    if job["status"] == "done":
        conn = _db()
        try:
            out["leads"] = _shape_leads(conn, job["leads"], user,
                                       single=job.get("single", False))
        finally:
            conn.close()
        out["area"] = job.get("area")
    if job["status"] == "error":
        out["error"] = job.get("error")
    return jsonify(out)


@app.post("/api/leads/unlock")
def unlock_lead():
    """Spend one unlock to reveal a lead's full address (all plans)."""
    user, err = _require_user()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    lead_key = str(body.get("lead_key", "")).strip()
    if not lead_key:
        return jsonify({"ok": False, "error": "Missing lead."}), 400
    conn = _db()
    try:
        quota = _quota(conn, user["id"], user["is_admin"])
        if user["is_admin"]:
            return jsonify({"ok": True, "quota": quota})
        if conn.execute("SELECT 1 FROM unlocks WHERE user_id=? AND lead_key=?",
                        (user["id"], lead_key)).fetchone():
            return jsonify({"ok": True, "quota": quota})
        if quota["unlocks_left"] <= 0:
            if quota["plan"] == "trial":
                return jsonify({"ok": False,
                                "error": "trial_unlocks_exhausted",
                                "quota": quota}), 402
            return jsonify({"ok": False, "error": "unlocks_exhausted",
                            "quota": quota}), 402
        conn.execute("INSERT INTO unlocks (user_id, lead_key, unlocked_at)"
                     " VALUES (?,?,?)", (user["id"], lead_key, time.time()))
        _consume_unlock(conn, user["id"], False)
        quota = _quota(conn, user["id"], False)
    finally:
        conn.close()
    lead = _find_lead(lead_key)
    if lead:
        lead = {**lead, "lead_key": lead_key, "locked": False}
    return jsonify({"ok": True, "quota": quota, "lead": lead})


def _admin_ok() -> bool:
    """True if the requester is an admin: logged-in owner account,
    or holds a valid admin-password session cookie."""
    token = request.cookies.get("rs_admin", "")
    if token:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT 1 FROM admin_sessions WHERE token = ? AND expires_at > ?",
                (token, time.time())).fetchone()
            if row:
                return True
        finally:
            conn.close()
    user = _current_user()
    return bool(user and user.get("is_admin"))


@app.post("/api/admin/login")
def admin_login():
    """Admin password gate. The password is the ADMIN_PASSWORD env var —
    set it to the same admin password used on the other products."""
    body = request.get_json(silent=True) or {}
    pw = body.get("password", "")
    expected = os.environ.get("ADMIN_PASSWORD", "")
    if expected and pw and hmac.compare_digest(pw, expected):
        token = secrets.token_urlsafe(32)
        now = time.time()
        conn = _db()
        try:
            conn.execute(
                "INSERT INTO admin_sessions (token, created_at, expires_at)"
                " VALUES (?, ?, ?)", (token, now, now + SESSION_DAYS * 86400))
            conn.commit()
        finally:
            conn.close()
        resp = jsonify({"ok": True})
        secure = request.headers.get("X-Forwarded-Proto", "") == "https" \
            or request.is_secure
        resp.set_cookie("rs_admin", token, max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="Lax", secure=secure, path="/")
        return resp
    time.sleep(0.5)
    return jsonify({"ok": False, "error": "bad_password"}), 401


@app.post("/api/admin/logout")
def admin_logout():
    token = request.cookies.get("rs_admin", "")
    if token:
        conn = _db()
        try:
            conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
            conn.commit()
        finally:
            conn.close()
    resp = jsonify({"ok": True})
    resp.delete_cookie("rs_admin", path="/")
    return resp


def _can_run_tests() -> bool:
    """Only mtthew.westfall@gmail.com (and typo variant gmaiI.com) can run test suite."""
    user = _current_user()
    if user and user.get("email") in ALLOWED_TEST_EMAILS:
        return True
    return False


_latest_test_results: dict | None = None


@app.post("/api/admin/run-tests")
def admin_run_tests():
    """Run pytest suite and store/return results.
    Restricted to authorized email (mtthew.westfall@gmail.com)."""
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin_required"}), 403
    user = _current_user()
    if not user or user.get("email") not in ALLOWED_TEST_EMAILS:
        return jsonify({
            "ok": False,
            "error": "unauthorized_email",
            "message": "Only mtthew.westfall@gmail.com is authorized to run tests."
        }), 403

    global _latest_test_results
    import pytest

    class TestCollector:
        def __init__(self):
            self.reports = []
            self.start_time = time.time()
            self.end_time = None

        def pytest_runtest_logreport(self, report):
            if report.when == "call" or (report.when == "setup" and report.failed):
                self.reports.append({
                    "nodeid": report.nodeid,
                    "name": report.location[2],
                    "file": report.location[0],
                    "outcome": report.outcome,
                    "duration": round(report.duration, 4),
                    "longrepr": str(report.longrepr) if report.failed else None
                })

    collector = TestCollector()
    test_dir = os.path.join(os.path.dirname(__file__), "tests")
    res_code = pytest.main([test_dir, "-q"], plugins=[collector])
    collector.end_time = time.time()

    total = len(collector.reports)
    passed = sum(1 for r in collector.reports if r["outcome"] == "passed")
    failed = sum(1 for r in collector.reports if r["outcome"] == "failed")
    skipped = sum(1 for r in collector.reports if r["outcome"] == "skipped")
    total_duration = round(collector.end_time - collector.start_time, 2)
    pass_rate = round((passed / total * 100), 1) if total > 0 else 0.0

    suites: dict[str, list] = {}
    for r in collector.reports:
        # Nodeid format: tests/test_file.py::ClassName::test_name or tests/test_file.py::test_name
        parts = r["nodeid"].split("::")
        suite_name = parts[1] if len(parts) > 2 else "General"
        if suite_name not in suites:
            suites[suite_name] = []
        suites[suite_name].append(r)

    _latest_test_results = {
        "ok": True,
        "run_at": time.time(),
        "exit_code": int(res_code),
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "pass_rate": pass_rate,
        "duration": total_duration,
        "reports": collector.reports,
        "suites": suites
    }

    return jsonify(_latest_test_results)


@app.get("/api/admin/test-results")
def admin_test_results():
    """Retrieve last test run results and authorization status."""
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin_required"}), 403
    return jsonify({
        "ok": True,
        "can_run_tests": _can_run_tests(),
        "latest_results": _latest_test_results
    })


@app.get("/api/admin/users")
def admin_users():
    """Owner-only: list all accounts with plan + quota usage."""
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin_required"}), 403
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, email, account_type, company_name, plan,"
            " is_admin, created_at FROM users ORDER BY created_at DESC").fetchall()
        users = []
        for r in rows:
            q = _quota(conn, r[0], False)
            users.append({
                "email": r[1], "account_type": r[2], "company_name": r[3],
                "plan": r[4], "is_admin": bool(r[5]),
                "created_at": r[6], "quota": q,
            })
    finally:
        conn.close()
    return jsonify({"ok": True, "users": users})


@app.get("/admin")
def admin_page():
    """Admin account-management page. The page itself shows a login form
    and gates its API calls; safe to serve to everyone."""
    return send_from_directory("static", "admin.html")


@app.post("/api/admin/set-plan")
def admin_set_plan():
    """Owner-only: give an account a (free) month of any plan.

    Resets the billing cycle: fresh scan + unlock allowances from now."""
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin_required"}), 403
    body = request.get_json(force=True, silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    plan = str(body.get("plan", "")).strip().lower()
    if plan not in PLANS:
        return jsonify({"ok": False,
                        "error": f"Plan must be one of: {', '.join(PLANS)}"}), 400
    conn = _db()
    try:
        cur = conn.execute("SELECT id FROM users WHERE email=?", (email,))
        row = cur.fetchone()
        if not row:
            return jsonify({"ok": False, "error": "No such account."}), 404
        conn.execute("UPDATE users SET plan=?, cycle_scans_used=0,"
                     " cycle_unlocks_used=0, period_start=? WHERE id=?",
                     (plan, time.time(), row[0]))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "email": email, "plan": plan})


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/health")
def health():
    return jsonify({"ok": True, "grader": bool(GEMINI_KEY)})


if __name__ == "__main__":
    _db().close()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)),
            threaded=True)
