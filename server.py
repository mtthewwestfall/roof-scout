"""RoofScout server: zip in, ranked roof leads out."""
from __future__ import annotations

import hashlib
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

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "roofscout.db"))
CACHE_TTL = 7 * 24 * 3600
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
OWNER_EMAIL = "mtthew.westfall@gmail.com"
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
    return conn


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
    return {"id": row[0], "email": row[1], "account_type": row[2],
            "company_name": row[3], "is_admin": bool(row[4])}


def _require_user():
    user = _current_user()
    if not user:
        return None, (jsonify({"ok": False, "error": "login_required"}), 401)
    return user, None


def _valid_email(email: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email or ""))


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
        is_admin = 1 if email == OWNER_EMAIL else 0
        conn.execute(
            "INSERT INTO users (id, email, pw_hash, salt, account_type,"
            " company_name, is_admin, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, email, _hash_pw(password, salt), salt, account_type,
             company_name, is_admin, time.time()))
        token = _new_session(conn, user_id)
    finally:
        conn.close()
    resp = jsonify({"ok": True, "user": {"email": email,
                                        "account_type": account_type,
                                        "company_name": company_name,
                                        "is_admin": bool(is_admin)}})
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
    _, err = _require_user()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    raw = str(body.get("q", body.get("zip", ""))).strip()
    try:
        count = max(5, min(30, int(body.get("count", 20))))
    except Exception:
        count = 20

    if re.fullmatch(r"\d{5}", raw):
        zipcode = raw
        cached = _cache_get(zipcode, count)
        if cached:
            return jsonify({"ok": True, "cached": True, "payload": cached})
        job_id = uuid.uuid4().hex[:12]
        with _jobs_lock:
            _jobs[job_id] = {"status": "running", "phase": "start", "done": 0,
                             "total": count, "msg": "Starting…", "zip": zipcode}
        t = threading.Thread(target=_run_scan, args=(job_id, zipcode, count),
                             daemon=True)
        t.start()
        return jsonify({"ok": True, "job_id": job_id})

    # ...otherwise treat it as a typed street address (interchangeable input)
    if len(raw) < 5:
        return jsonify({"ok": False,
                        "error": "Enter a 5-digit zip or a street address."}), 400
    house = pipeline.geocode_address(raw)
    if not house:
        return jsonify({"ok": False, "error":
                        f"Couldn't locate “{raw}”. Try a full street address "
                        "with city and state."}), 400
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "phase": "start", "done": 0,
                         "total": 1, "msg": "Starting…",
                         "zip": house.get("postcode", "")}
    t = threading.Thread(target=_run_address_scan, args=(job_id, house),
                         daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/api/scan/<job_id>")
def scan_status(job_id: str):
    _, err = _require_user()
    if err:
        return err
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job"}), 404
    out = {"ok": True, "status": job["status"],
           "phase": job.get("phase"), "done": job.get("done", 0),
           "total": job.get("total", 0), "msg": job.get("msg", "")}
    if job["status"] == "done":
        out["leads"] = job["leads"]
        out["area"] = job.get("area")
    if job["status"] == "error":
        out["error"] = job.get("error")
    return jsonify(out)


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
