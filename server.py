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
import urllib.parse
import urllib.request
import uuid

from flask import Flask, jsonify, redirect, request, send_from_directory

import pipeline

app = Flask(__name__, static_folder="static", static_url_path="/static")


@app.before_request
def _force_https():
    # Railway terminates TLS and sets X-Forwarded-Proto. Redirect any
    # plain-HTTP hit to HTTPS (except local dev) so credentials and
    # session cookies never travel in the clear.
    host = (request.host or "").split(":")[0]
    if host in ("localhost", "127.0.0.1"):
        return None
    if request.headers.get("X-Forwarded-Proto", "http") != "https" \
            and not request.is_secure:
        return redirect(request.url.replace("http://", "https://", 1),
                        code=301)
    return None


@app.after_request
def _security_headers(resp):
    resp.headers["Strict-Transport-Security"] = \
        "max-age=31536000; includeSubDomains"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # App uses inline scripts/styles plus Leaflet from unpkg and map
    # tiles / data-URI imagery, so script/style stay 'unsafe-inline'
    # while framing, plugins and base-uri stay locked down.
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "frame-ancestors 'self'; "
        "object-src 'none'; "
        "base-uri 'self'")
    return resp


# Common passwords rejected at signup (case-insensitive). Short curated
# list covering the worst offenders; length minimum still applies.
_COMMON_PASSWORDS = frozenset({
    "password", "password1", "password123", "passw0rd", "qwerty",
    "qwerty123", "12345678", "123456789", "1234567890", "abc123",
    "letmein", "welcome", "welcome1", "monkey", "dragon", "football",
    "iloveyou", "trustno1", "sunshine", "master", "shadow", "superman",
    "roofing", "roofing1", "roofer", "roofer123", "changeme",
})

# Lightweight in-memory login throttle: (ip, email) -> [timestamps].
# 8 failed attempts in 10 minutes -> 60s cooldown. Survives nothing;
# it only needs to slow automated guessing, not be a perfect record.
_login_attempts: dict[tuple[str, str], list[float]] = {}
_login_attempts_lock = threading.Lock()
_LOGIN_MAX_FAILS = 8
_LOGIN_WINDOW = 600.0
_LOGIN_COOLDOWN = 60.0


def _login_throttled(ip: str, email: str) -> bool:
    now = time.time()
    key = (ip, email)
    with _login_attempts_lock:
        hits = [t for t in _login_attempts.get(key, [])
                if now - t < _LOGIN_WINDOW]
        _login_attempts[key] = hits
        return len(hits) >= _LOGIN_MAX_FAILS and \
            now - hits[-1] < _LOGIN_COOLDOWN


def _login_failed(ip: str, email: str):
    key = (ip, email)
    with _login_attempts_lock:
        _login_attempts.setdefault(key, []).append(time.time())


def _login_ok(ip: str, email: str):
    with _login_attempts_lock:
        _login_attempts.pop((ip, email), None)

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
ALLOWED_TEST_EMAILS = {OWNER_EMAIL}
SESSION_DAYS = 30

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS scans (
        zip TEXT NOT NULL, count INTEGER NOT NULL, payload TEXT NOT NULL,
        created_at REAL NOT NULL, PRIMARY KEY (zip, count))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS scan_jobs (
        job_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
        status TEXT NOT NULL, leads TEXT NOT NULL DEFAULT '[]',
        area TEXT NOT NULL DEFAULT '', msg TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL)""")
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
    # Roofs already shown to a customer per ZIP (~35m cells), so repeat
    # scans rotate to new rooftops instead of re-showing the same ones.
    conn.execute("""CREATE TABLE IF NOT EXISTS scan_seen (
        user_id TEXT NOT NULL, zip TEXT NOT NULL,
        lat_r INTEGER NOT NULL, lng_r INTEGER NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (user_id, zip, lat_r, lng_r))""")
    # Trial/plan columns (added after launch; migrate old DBs in place).
    for col in (
            "plan TEXT NOT NULL DEFAULT 'trial'",
            "trial_scans_used INTEGER NOT NULL DEFAULT 0",
            "trial_unlocks_used INTEGER NOT NULL DEFAULT 0",
            "cycle_scans_used INTEGER NOT NULL DEFAULT 0",
            "cycle_unlocks_used INTEGER NOT NULL DEFAULT 0",
            "period_start REAL NOT NULL DEFAULT 0",
            "bonus_unlocks INTEGER NOT NULL DEFAULT 0"):
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
    # Abandoned-lead review decisions: one row per customer per lead, so a
    # rejection can grant its replacement unlock exactly once.
    conn.execute("""CREATE TABLE IF NOT EXISTS lead_reviews (
        user_id TEXT NOT NULL, lead_key TEXT NOT NULL,
        decision TEXT NOT NULL, decided_at REAL NOT NULL,
        PRIMARY KEY (user_id, lead_key))""")
    # Email verification (added 2026-10-01): signup no longer grants a
    # session until the address is confirmed, closing the throwaway-email
    # free-trial loop. Accounts created before this column existed are
    # grandfathered as verified; the UPDATE only runs on the fresh ALTER.
    _migration_now = time.time()
    try:
        conn.execute("ALTER TABLE users ADD COLUMN"
                     " email_verified INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE users SET email_verified=1 WHERE created_at < ?",
                     (_migration_now,))
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            raise
    conn.execute("""CREATE TABLE IF NOT EXISTS verification_tokens (
        token TEXT PRIMARY KEY, user_id TEXT NOT NULL, email TEXT NOT NULL,
        created_at REAL NOT NULL, expires_at REAL NOT NULL)""")
    # For-sale lookup cache: one row per address, refreshed after 7 days, so
    # unlock-time checks stay cheap.
    conn.execute("""CREATE TABLE IF NOT EXISTS listing_cache (
        addr_key TEXT PRIMARY KEY, for_sale INTEGER NOT NULL,
        checked_at REAL NOT NULL)""")
    conn.commit()
    return conn


# ---------------- plans & trial quotas ----------------

PLANS = {
    "trial":   {"name": "Trial",   "scans": 1, "unlocks": 5,   "cycle_days": 0},
    "starter": {"name": "Starter", "scans": 5, "unlocks": 25,  "cycle_days": 30},
    "pro":     {"name": "Pro",     "scans": 20, "unlocks": 100, "cycle_days": 30},
}


def _quota(conn, user_id: str, is_admin: bool = False) -> dict:
    """Current plan usage. Admins are unlimited."""
    row = conn.execute(
        "SELECT plan, trial_scans_used, trial_unlocks_used,"
        " cycle_scans_used, period_start, cycle_unlocks_used,"
        " bonus_unlocks"
        " FROM users WHERE id=?",
        (user_id,)).fetchone()
    plan = (row[0] if row else "trial") or "trial"
    if plan not in PLANS:
        plan = "trial"
    spec = PLANS[plan]
    bonus = row[6] or 0
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
                "unlocks_left": max(0, spec["unlocks"] - unlocks_used + bonus),
                "scans_used": scans_used, "unlocks_used": unlocks_used,
                "scans_cap": spec["scans"], "unlocks_cap": spec["unlocks"],
                "cycle_ends": period_start + spec["cycle_days"] * 86400}
    return {"plan": plan, "plan_name": spec["name"],
            "scans_left": max(0, spec["scans"] - (row[1] or 0)),
            "unlocks_left": max(0, spec["unlocks"] - (row[2] or 0) + bonus),
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


def _refund_scan(conn, user_id: str):
    """Give a scan back when the job failed (crashed, no imagery, bad zip).

    Mirrors _consume_scan: paid plans draw from the cycle counters, trial
    from the trial counters. Never drops below zero.
    """
    row = conn.execute("SELECT plan FROM users WHERE id=?",
                       (user_id,)).fetchone()
    plan = (row[0] if row else "trial") or "trial"
    if plan in PLANS and PLANS[plan]["cycle_days"]:
        conn.execute("UPDATE users SET cycle_scans_used="
                     "max(0, cycle_scans_used-1) WHERE id=?", (user_id,))
    else:
        conn.execute("UPDATE users SET trial_scans_used="
                     "max(0, trial_scans_used-1) WHERE id=?", (user_id,))
    conn.commit()


def _fail_job(job_id: str, error: str):
    """Mark a scan job failed and refund the owner's consumed scan."""
    owner = None
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            owner = job.get("owner")
    _set_job(job_id, status="error", error=error)
    if owner:
        try:
            conn = _db()
            try:
                _refund_scan(conn, owner)
            finally:
                conn.close()
        except Exception:
            pass


def _lead_key(lead: dict) -> str:
    base = "|".join([str(lead.get("address", "")),
                     str(lead.get("postcode", "")),
                     f"{float(lead.get('lat') or 0):.5f}",
                     f"{float(lead.get('lng') or 0):.5f}"])
    return hashlib.sha1(base.encode()).hexdigest()[:16]


_LISTING_TTL = 7 * 86400  # re-check for-sale status weekly


def _addr_key(lead: dict) -> str:
    norm = re.sub(r"\s+", " ",
                  f"{lead.get('address', '')} {lead.get('postcode', '')}"
                  .strip().lower())
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def _listing_vendor_lookup(address: str, city: str, state: str,
                           postcode: str) -> bool | None:
    """Ask the listings vendor whether the address is actively for sale.

    Returns True/False, or None when unknown (no API key configured, no
    address, vendor error). Vendor: RentCast listings API
    (GET /v1/listings/sale, X-Api-Key header); only `status == "Active"`
    counts as for sale — Inactive/delisted never does.
    """
    api_key = os.environ.get("LISTINGS_API_KEY", "").strip()
    if not api_key or not address.strip():
        return None
    q = ", ".join(p for p in (address.strip(), city.strip(),
                              state.strip(), postcode.strip()) if p)
    url = ("https://api.rentcast.io/v1/listings/sale?address="
           + urllib.parse.quote(q))
    req = urllib.request.Request(
        url, headers={"X-Api-Key": api_key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.load(resp)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    for item in data:
        if isinstance(item, dict) \
                and str(item.get("status", "")).lower() == "active":
            return True
    return False


def _for_sale_status(conn, lead: dict, live: bool = True) -> bool | None:
    """For-sale status for an unlocked lead: cached 7 days, else vendor.

    live=False reads the cache only (no vendor call) — for render paths
    like _shape_leads that must never trigger a paid lookup. The live
    lookup runs once at unlock time.
    """
    key = _addr_key(lead)
    row = conn.execute("SELECT for_sale, checked_at FROM listing_cache"
                       " WHERE addr_key=?", (key,)).fetchone()
    now = time.time()
    if row and now - row[1] < _LISTING_TTL:
        return bool(row[0])
    if not live:
        return None
    status = _listing_vendor_lookup(str(lead.get("address", "")),
                                    str(lead.get("city", "")),
                                    str(lead.get("state", "")),
                                    str(lead.get("postcode", "")))
    if status is not None:
        conn.execute("INSERT OR REPLACE INTO listing_cache"
                     " (addr_key, for_sale, checked_at) VALUES (?,?,?)",
                     (key, int(status), now))
        conn.commit()
    return status


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
    rejected = {r[0] for r in
                conn.execute("SELECT lead_key FROM lead_reviews"
                             " WHERE user_id=? AND decision='reject'",
                             (user["id"],))}
    out = []
    for l in leads:
        key = _lead_key(l)
        if key in rejected:
            continue  # customer already passed on this one
        if key in unlocked:
            c = {**l, "lead_key": key, "locked": False}
            fs = _for_sale_status(conn, l, live=False)
            if fs is not None:
                c["for_sale"] = fs
            out.append(c)
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


# ---------------- Stripe webhook (automatic plan fulfillment) ----------------

STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
# Fallback mapping: Payment-Link amounts (cents) -> plan. Prefer setting
# metadata plan=starter|pro on the Payment Link itself; amount is the backup.
STRIPE_AMOUNT_PLANS = {4999: "starter", 9999: "pro"}


def _stripe_sig_ok(payload: bytes, header: str, secret: str) -> bool:
    """Verify a Stripe-Signature header (t=...,v1=...) with timestamp tolerance."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
        ts, sig = parts.get("t", ""), parts.get("v1", "")
        if not ts or not sig:
            return False
        if abs(time.time() - int(ts)) > 300:
            return False
        expect = hmac.new(secret.encode(), f"{ts}.".encode() + payload,
                          hashlib.sha256).hexdigest()
        return hmac.compare_digest(expect, sig)
    except Exception:
        return False


def _apply_plan_email(conn, email: str, plan: str) -> bool:
    """Set a user's plan by email, resetting the billing cycle.
    Shared by /admin and the Stripe webhook. Returns False when the
    account does not exist."""
    row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        return False
    conn.execute("UPDATE users SET plan=?, cycle_scans_used=0,"
                 " cycle_unlocks_used=0, period_start=? WHERE id=?",
                 (plan, time.time(), row[0]))
    conn.commit()
    return True


def _stripe_tables(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS stripe_events ("
                 "event_id TEXT PRIMARY KEY, type TEXT NOT NULL,"
                 " created_at REAL NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS stripe_pending ("
                 "email TEXT NOT NULL, plan TEXT NOT NULL,"
                 " event_id TEXT NOT NULL, created_at REAL NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS stripe_customers ("
                 "customer_id TEXT PRIMARY KEY, email TEXT NOT NULL)")


@app.post("/api/stripe/webhook")
def stripe_webhook():
    """Stripe -> Roof Scout automatic fulfillment.

    Stripe dashboard setup: add endpoint
    https://getveridatenow.com/api/stripe/webhook with events
    checkout.session.completed, invoice.paid, customer.subscription.deleted,
    and put the endpoint's signing secret in STRIPE_WEBHOOK_SECRET.
    The payer's email is the identity key, matched to the Roof Scout account.
    Set metadata plan=starter|pro on each Payment Link for explicit mapping
    (amount_total is the fallback)."""
    payload = request.get_data()
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "webhook_not_configured"}), 503
    if not _stripe_sig_ok(payload, request.headers.get("Stripe-Signature", ""),
                          STRIPE_WEBHOOK_SECRET):
        return jsonify({"ok": False, "error": "bad_signature"}), 400
    try:
        event = json.loads(payload)
    except Exception:
        return jsonify({"ok": False, "error": "bad_json"}), 400
    etype = event.get("type", "")
    eid = event.get("id", "")
    conn = _db()
    try:
        _stripe_tables(conn)
        if eid and conn.execute("SELECT 1 FROM stripe_events WHERE event_id=?",
                                (eid,)).fetchone():
            return jsonify({"ok": True, "duplicate": True})
        obj = (event.get("data") or {}).get("object") or {}
        email, plan = "", ""
        if etype == "checkout.session.completed":
            if obj.get("payment_status") == "paid":
                email = str(((obj.get("customer_details") or {}).get("email")
                             or obj.get("customer_email") or "")).strip().lower()
                plan = str((obj.get("metadata") or {}).get("plan", "")).lower()
                if plan not in PLANS or plan == "trial":
                    plan = STRIPE_AMOUNT_PLANS.get(obj.get("amount_total") or 0, "")
                cust = obj.get("customer")
                if cust and email:
                    conn.execute("INSERT OR REPLACE INTO stripe_customers"
                                 " (customer_id, email) VALUES (?,?)", (cust, email))
        elif etype == "invoice.paid":
            # Renewals only: the initial subscription_create invoice is already
            # covered by checkout.session.completed above.
            if obj.get("billing_reason") == "subscription_cycle":
                email = str(obj.get("customer_email") or "").strip().lower()
                plan = STRIPE_AMOUNT_PLANS.get(obj.get("amount_due") or 0, "")
        elif etype == "customer.subscription.deleted":
            cust = obj.get("customer", "")
            row = conn.execute("SELECT email FROM stripe_customers WHERE"
                               " customer_id=?", (cust,)).fetchone() if cust else None
            if row:
                email, plan = row[0], "trial"
        if email and plan:
            if _apply_plan_email(conn, email, plan):
                print(f"[stripe] fulfilled {email} -> {plan} ({etype})")
            else:
                conn.execute("INSERT INTO stripe_pending"
                             " (email, plan, event_id, created_at)"
                             " VALUES (?,?,?,?)", (email, plan, eid, time.time()))
                print(f"[stripe] payment for unknown account: {email} ->"
                      f" {plan} ({eid})")
        if eid:
            conn.execute("INSERT OR IGNORE INTO stripe_events"
                         " (event_id, type, created_at) VALUES (?,?,?)",
                         (eid, etype, time.time()))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


# ---------------- automatic Street View per listing ----------------

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")


def _sv_cache_dir() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "svcache")
    os.makedirs(d, exist_ok=True)
    return d


def _sv_fetch(lat: float, lng: float) -> bytes | None:
    """Fetch a Street View still from Google. None when unavailable."""
    url = ("https://maps.googleapis.com/maps/api/streetview"
           f"?size=640x480&location={lat},{lng}&key={GOOGLE_MAPS_API_KEY}")
    req = urllib.request.Request(url, headers={"User-Agent": "RoofScout/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
    # Google answers no-imagery with a tiny placeholder image.
    return data if len(data) > 2000 else None


@app.get("/api/leads/<lead_key>/streetview")
def lead_streetview(lead_key):
    """Proxied Google Street View still for an unlocked lead.

    The API key never leaves the server. Locked leads (jittered coords,
    masked addresses) get 403 so the real location can't leak."""
    user, err = _require_verified()
    if err:
        return err
    if not re.fullmatch(r"[0-9a-f]{16}", lead_key or ""):
        return jsonify({"ok": False, "error": "bad_lead"}), 404
    if not GOOGLE_MAPS_API_KEY:
        return jsonify({"ok": False, "error": "streetview_unavailable"}), 404
    conn = _db()
    try:
        ok = user["is_admin"] or conn.execute(
            "SELECT 1 FROM unlocks WHERE user_id=? AND lead_key=?",
            (user["id"], lead_key)).fetchone()
    finally:
        conn.close()
    if not ok:
        return jsonify({"ok": False, "error": "locked"}), 403
    lead = _find_lead(lead_key)
    lat, lng = (lead or {}).get("lat"), (lead or {}).get("lng")
    if not lat or not lng:
        return jsonify({"ok": False, "error": "no_location"}), 404
    path = os.path.join(_sv_cache_dir(), f"{lead_key}.jpg")
    if not os.path.exists(path):
        try:
            data = _sv_fetch(float(lat), float(lng))
        except Exception:
            data = None
        if not data:
            return jsonify({"ok": False, "error": "no_imagery"}), 404
        with open(path, "wb") as f:
            f.write(data)
    return send_from_directory(_sv_cache_dir(), f"{lead_key}.jpg",
                               mimetype="image/jpeg")


# ---------------- USPS address validation ----------------

USPS_CLIENT_ID = os.environ.get("USPS_CLIENT_ID", "")
USPS_CLIENT_SECRET = os.environ.get("USPS_CLIENT_SECRET", "")
USPS_BASE = "https://apis.usps.com"
_usps_token_cache = {"value": "", "expires": 0.0}
_usps_lock = threading.Lock()


def _usps_configured() -> bool:
    return bool(USPS_CLIENT_ID and USPS_CLIENT_SECRET)


def _usps_token() -> str | None:
    """OAuth client-credentials token, cached until near expiry."""
    with _usps_lock:
        if _usps_token_cache["value"] and \
                time.time() < _usps_token_cache["expires"] - 60:
            return _usps_token_cache["value"]
    # USPS expects a JSON body (per their official api-examples).
    body = json.dumps({
        "client_id": USPS_CLIENT_ID,
        "client_secret": USPS_CLIENT_SECRET,
        "grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        f"{USPS_BASE}/oauth2/v3/token", data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "RoofScout/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    token = data.get("access_token", "")
    if not token:
        return None
    try:
        expires_in = int(data.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    with _usps_lock:
        _usps_token_cache["value"] = token
        _usps_token_cache["expires"] = time.time() + expires_in
    return token


def _usps_validate(street: str, city: str, state: str,
                   zipcode: str) -> dict | None:
    """Standardize one address via the USPS Addresses API v3.
    Returns the parsed AddressValidateResponse, or None on transport/
    auth failure (the caller keeps the free-source address then)."""
    token = _usps_token()
    if not token:
        return None
    params = {"streetAddress": street, "city": city, "state": state}
    if zipcode:
        params["ZIPCode"] = zipcode
    url = f"{USPS_BASE}/addresses/v3/address?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}",
                      "User-Agent": "RoofScout/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _usps_cache_key(street: str, city: str, state: str, zipcode: str) -> str:
    base = "|".join(x.strip().lower()
                    for x in (street, city, state, zipcode))
    return hashlib.sha256(base.encode()).hexdigest()[:24]


def _apply_usps_result(h: dict, res: dict):
    """Fold a USPS validation response into a lead.

    DPV-confirmed deliverable -> the address is standardized, ZIP+4 is
    attached, and even an estimated guess is promoted to verified.
    Not deliverable / vacant -> the lead goes to the review flow instead
    of a dead mailbox. Anything inconclusive leaves the free-source
    address untouched."""
    addr = res.get("address") or {}
    info = res.get("additionalInfo") or {}
    street = addr.get("streetAddress", "")
    if not street:
        return
    dpv = str(info.get("DPVConfirmation") or "").upper()
    nostat = str(info.get("DPVNostatReasonCode") or "")
    h["address"] = street
    h["city"] = addr.get("city") or h.get("city", "")
    h["state"] = addr.get("state") or h.get("state", "")
    h["postcode"] = addr.get("ZIPCode") or h.get("postcode", "")
    if addr.get("ZIPPlus4"):
        h["zip4"] = addr["ZIPPlus4"]
    h["usps_dpv"] = dpv
    if dpv == "Y":
        h["address_confidence"] = "verified"
        h["address_source"] = "usps"
    elif dpv == "N" or nostat == "5":
        reason = "USPS: address not confirmed deliverable" + \
            (" (vacant)" if nostat == "5" else "")
        if h.get("needs_review") and h.get("review_reason"):
            h["review_reason"] = h["review_reason"] + "; " + reason
        else:
            h["needs_review"] = True
            h["review_reason"] = reason


def _usps_enhance_leads(houses: list[dict], progress=None):
    """Validate lead addresses against USPS when credentials are set.

    Runs after the free geocoders in the scan pipeline. Results are
    cached per address so repeat scans don't re-bill. Any USPS hiccup
    leaves the free-source address exactly as it was — validation is
    strictly additive, never destructive."""
    if not _usps_configured():
        return
    conn = _db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS usps_validated ("
                     "key TEXT PRIMARY KEY, result TEXT NOT NULL,"
                     " created_at REAL NOT NULL)")
        targets = [h for h in houses
                   if h.get("grade") in (1, 2, 3) and h.get("address")]
        total = len(targets)
        for i, h in enumerate(targets):
            if progress:
                progress("usps", i, total,
                         f"Validating addresses… {i}/{total}")
            key = _usps_cache_key(h.get("address", ""), h.get("city", ""),
                                  h.get("state", ""), h.get("postcode", ""))
            row = conn.execute("SELECT result FROM usps_validated WHERE key=?",
                               (key,)).fetchone()
            if row:
                try:
                    res = json.loads(row[0])
                except Exception:
                    continue
            else:
                try:
                    res = _usps_validate(h["address"], h.get("city", ""),
                                         h.get("state", ""),
                                         h.get("postcode", "")) or {}
                except Exception:
                    continue
                conn.execute("INSERT OR REPLACE INTO usps_validated"
                             " (key, result, created_at) VALUES (?,?,?)",
                             (key, json.dumps(res), time.time()))
                conn.commit()
            _apply_usps_result(h, res)
        if progress:
            progress("usps", total, total, "Address validation done.")
    finally:
        conn.close()


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
            "SELECT u.id, u.email, u.account_type, u.company_name, u.is_admin,"
            " u.email_verified"
            " FROM sessions s JOIN users u ON s.user_id = u.id"
            " WHERE s.token = ? AND s.expires_at > ?",
            (token, time.time())).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    user = {"id": row[0], "email": row[1], "account_type": row[2],
            "company_name": row[3], "is_admin": bool(row[4]),
            "email_verified": bool(row[5])}
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


def _require_verified():
    """Logged-in AND email-confirmed. Gates every quota-consuming action
    (scans, unlocks, review approvals) so unverified throwaway accounts
    can't burn free trials."""
    user, err = _require_user()
    if err:
        return None, err
    if not user.get("email_verified") and not user.get("is_admin"):
        return None, (jsonify({"ok": False, "error": "verify_email"}), 403)
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


VERIFY_TOKEN_TTL = 24 * 3600  # verification links live 24 hours
VERIFY_RESEND_COOLDOWN = 60  # at most one verification email per minute


def _send_email(to_email: str, subject: str, html: str) -> bool:
    """Send one transactional email via Resend. Returns True on success.
    Pure stdlib (urllib) so tests can patch it; never raises."""
    api_key = os.environ.get("RESEND_API_KEY", "")
    mail_from = os.environ.get("MAIL_FROM", "Roof Scout <support@lockeddoor.ai>")
    if not api_key:
        print(f"[email] RESEND_API_KEY not set; would send to {to_email}: {subject}",
              flush=True)
        return False
    try:
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=json.dumps({"from": mail_from, "to": [to_email],
                             "subject": subject, "html": html}).encode(),
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json",
                     "User-Agent": "roof-scout/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"[email] Resend failed for {to_email}: {e}", flush=True)
        return False


def _verification_email_html(verify_url: str) -> str:
    return f"""<div style="font-family:sans-serif;max-width:560px;margin:0 auto;">
<p>Welcome to <b>Roof Scout</b> — one quick step and you're in.</p>
<p><a href="{verify_url}" style="display:inline-block;padding:12px 24px;
background:#2563eb;color:#fff;border-radius:8px;text-decoration:none;">
Confirm my email</a></p>
<p style="color:#666;font-size:13px;">This link expires in 24 hours.
If you didn't sign up for Roof Scout, just ignore this email.</p></div>"""


def _issue_verification_token(conn, user_id: str, email: str) -> str:
    """Create (or reuse a fresh) verification token and email it. Returns
    the token, or '' if the email could not be sent."""
    now = time.time()
    row = conn.execute(
        "SELECT token, created_at FROM verification_tokens"
        " WHERE user_id=? AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
        (user_id, now)).fetchone()
    if row and now - row[1] < VERIFY_RESEND_COOLDOWN:
        return row[0]  # too soon to resend; reuse the live token
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM verification_tokens WHERE user_id=?", (user_id,))
    conn.execute(
        "INSERT INTO verification_tokens (token, user_id, email, created_at,"
        " expires_at) VALUES (?,?,?,?,?)",
        (token, user_id, email, now, now + VERIFY_TOKEN_TTL))
    conn.commit()
    base = os.environ.get("PUBLIC_APP_URL",
                          "https://roof-scout-production.up.railway.app").rstrip("/")
    verify_url = f"{base}/api/auth/verify?token={token}"
    if _send_email(email, "Confirm your Roof Scout email",
                   _verification_email_html(verify_url)):
        return token
    return ""


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
    if password.strip().lower() in _COMMON_PASSWORDS:
        return jsonify({"ok": False,
                        "error": "That password is too common. Pick something harder to guess."}), 400
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
        # No session until the email is confirmed: this is the trial-abuse
        # gate. A throwaway address can't consume scans/unlocks.
        token = _issue_verification_token(conn, user_id, email)
    finally:
        conn.close()
    if not token:
        return jsonify({"ok": False, "error":
            "We couldn't send the confirmation email. Check the address"
            " and try again."}), 502
    return jsonify({"ok": True, "verify_sent": True, "email": email})


@app.get("/api/auth/verify")
def verify_email():
    token = str(request.args.get("token", ""))
    if not token:
        return redirect("/?verify=missing", code=302)
    conn = _db()
    try:
        row = conn.execute(
            "SELECT user_id, email, expires_at FROM verification_tokens"
            " WHERE token=?", (token,)).fetchone()
        if not row:
            return redirect("/?verify=invalid", code=302)
        if row[2] < time.time():
            conn.execute("DELETE FROM verification_tokens WHERE token=?",
                         (token,))
            conn.commit()
            return redirect("/?verify=expired", code=302)
        user_id = row[0]
        conn.execute("UPDATE users SET email_verified=1 WHERE id=?",
                     (user_id,))
        conn.execute("DELETE FROM verification_tokens WHERE user_id=?",
                     (user_id,))
        session_token = _new_session(conn, user_id)
    finally:
        conn.close()
    resp = redirect("/?verified=1", code=302)
    _set_session_cookie(resp, session_token)
    return resp


@app.post("/api/auth/resend-verification")
def resend_verification():
    body = request.get_json(force=True, silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    if not _valid_email(email):
        return jsonify({"ok": False, "error": "Enter a valid email address."}), 400
    conn = _db()
    try:
        row = conn.execute("SELECT id, email_verified FROM users WHERE email=?",
                           (email,)).fetchone()
        if not row:
            # Don't reveal whether the address is registered.
            return jsonify({"ok": True, "verify_sent": True})
        if row[1]:
            return jsonify({"ok": False, "error": "already_verified"}), 400
        token = _issue_verification_token(conn, row[0], email)
    finally:
        conn.close()
    if not token:
        return jsonify({"ok": False, "error":
            "We couldn't send the confirmation email right now."}), 502
    return jsonify({"ok": True, "verify_sent": True})


@app.post("/api/auth/login")
def login():
    body = request.get_json(force=True, silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    ip = request.headers.get("X-Forwarded-For",
                             request.remote_addr or "").split(",")[0].strip()
    if _login_throttled(ip, email):
        return jsonify({"ok": False,
                        "error": "Too many attempts. Wait a minute and try again."}), 429
    conn = _db()
    try:
        row = conn.execute(
            "SELECT id, pw_hash, salt, email, account_type, company_name,"
            " is_admin, email_verified FROM users WHERE email=?", (email,)).fetchone()
        if not row or _hash_pw(password, row[2]) != row[1]:
            _login_failed(ip, email)
            return jsonify({"ok": False,
                            "error": "Invalid email or password."}), 401
        _login_ok(ip, email)
        if not row[7]:
            # Password is right but the address isn't confirmed yet:
            # re-send the link instead of handing out a session.
            token = _issue_verification_token(conn, row[0], email)
            if not token:
                return jsonify({"ok": False, "error":
                    "We couldn't send the confirmation email right now."}), 502
            return jsonify({"ok": False, "error": "verify_email",
                            "email": email}), 403
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


def _cacheable(payload: dict) -> dict:
    """Copy of a scan payload safe to persist: Google Street View photos are
    served live only, never stored, so cached leads keep just the link."""
    leads = []
    for l in payload.get("leads") or []:
        if l.get("imagery") == "streetview":
            l = {**l, "img": "", "damage_img": ""}
        leads.append(l)
    return {**payload, "leads": leads}


def _seen_cells(user_id: str | None, zipcode: str) -> set:
    """~35m roof cells already shown to this customer for this ZIP."""
    if not user_id:
        return set()
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT lat_r, lng_r FROM scan_seen WHERE user_id=? AND zip=?",
            (user_id, zipcode)).fetchall()
    finally:
        conn.close()
    return set(rows)


def _record_seen(user_id: str | None, zipcode: str, houses: list[dict]):
    if not user_id or not houses:
        return
    conn = _db()
    try:
        now = time.time()
        conn.executemany(
            "INSERT OR IGNORE INTO scan_seen"
            " (user_id, zip, lat_r, lng_r, created_at) VALUES (?,?,?,?,?)",
            [(user_id, zipcode,
              round(h["lat"] * 3000), round(h["lng"] * 3000), now)
             for h in houses if h.get("lat") is not None])
        conn.commit()
    finally:
        conn.close()


def _set_job(job_id: str, **kw):
    with _jobs_lock:
        _jobs[job_id].update(kw)
        job = dict(_jobs[job_id])
    # Persist completed scans so leads survive refreshes and restarts.
    if kw.get("status") == "done":
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT OR REPLACE INTO scan_jobs"
                " (job_id, user_id, status, leads, area, msg, created_at)"
                " VALUES (?, ?, 'done', ?, ?, ?, ?)",
                (job_id, job.get("owner", ""), json.dumps(job.get("leads") or []),
                 job.get("area", ""), job.get("msg", ""), time.time()))
            conn.commit()
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _run_houses(job_id: str, houses: list[dict], area: str,
               cache: tuple | None = None, grader=None, user_id=None,
               footprints_ok: bool = True):
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
            _fail_job(job_id,
                      "Aerial imagery unavailable right now. Try again in a bit."
                      " Your scan was refunded.")
            return

        if not GEMINI_KEY and grader is None:
            _fail_job(job_id,
                      "Grader not configured (missing API key)."
                      " Your scan was refunded.")
            return
        pipeline.grade_roofs(GEMINI_KEY, houses, progress, grader=grader)

        # Street-level second opinion: aerial can't see through tree cover,
        # and street-facing slopes can show damage invisible from above.
        # Houses the aerial grader couldn't use (0) or called healthy (4-5)
        # get one street photo + re-grade; new 1-3s become leads below.
        if GEMINI_KEY or grader:
            pipeline.street_second_opinion(GEMINI_KEY, houses, progress,
                                           grader=grader)

        # Damaged only: the deep dive (pinpoint + addresses) runs solely on
        # roofs the grader flagged 1-3. Healthy (4-5) and ungradable (0)
        # roofs are not leads and never reach the customer.
        houses = [h for h in houses if h.get("grade") in (1, 2, 3)]
        # Abandoned / vacant signals become a review flag, not a silent
        # drop: the customer sees the warning and decides whether the
        # property is worth an unlock. Never present a fake lead as clean.
        for h in houses:
            reasons = []
            if h.get("vacant"):
                reasons.append("Map records mark this building "
                               "vacant/abandoned")
            if h.get("abandoned"):
                reasons.append("Aerial imagery suggests a derelict or "
                               "abandoned structure")
            if reasons:
                h["needs_review"] = True
                h["review_reason"] = "; ".join(reasons)
        if not houses:
            _set_job(job_id, status="done", leads=[], area=area,
                     msg="Done — no visibly damaged roofs found in this area. "
                         "Try another ZIP.")
            return

        # Pinpoint pass: zoomed damage close-up + repair breakdown for
        # damaged roofs (needs the graded image still in memory).
        if GEMINI_KEY and grader is None:
            pipeline.localize_damage(GEMINI_KEY, houses, progress)
        # Roof-first matching: damaged roofs get mailable street addresses.
        pipeline.attach_addresses(houses, progress)
        # USPS validation (optional): standardize deliverable addresses,
        # flag dead/vacant ones for review. No-op without credentials.
        _usps_enhance_leads(houses, progress)

        for h in houses:
            b64 = h.pop("image_b64", None)
            h["img"] = f"data:image/jpeg;base64,{b64}" if b64 else ""
            h["maps_url"] = ("https://www.google.com/maps/search/?api=1&query="
                             f"{h['lat']},{h['lng']}")
            h["streetview_url"] = ("https://www.google.com/maps/@?api=1&map_action=pano"
                                   f"&viewpoint={h['lat']},{h['lng']}")
        leads = pipeline.sort_leads(houses)
        payload = {"zip": cache[0] if cache else "", "area": area,
                   "leads": leads, "scanned_at": time.time(),
                   "footprints_ok": footprints_ok}
        if cache:
            _cache_put(cache[0], cache[1], _cacheable(payload))
            _record_seen(user_id, cache[0], houses)
        flagged = sum(1 for h in leads if h.get("needs_review"))
        msg = f"Done — {len(leads)} damaged roofs found."
        if flagged:
            msg += f" {flagged} flagged for review (possibly abandoned)."
        _set_job(job_id, status="done", leads=leads, area=area, msg=msg)
    except Exception as e:
        _fail_job(job_id, f"Scan failed: {e}. Your scan was refunded.")


def _run_scan(job_id: str, zipcode: str, count: int, user_id=None,
              grader=None, prescreener=None):
    try:
        def progress(phase, done, total, msg):
            _set_job(job_id, phase=phase, done=done, total=total, msg=msg)

        _set_job(job_id, phase="ziplookup", msg="Locating zip code…")
        center = pipeline.zip_center(zipcode)
        if not center:
            _fail_job(job_id, "Couldn't find that zip code."
                      " Try a valid 5-digit US zip. Your scan was refunded.")
            return
        _set_job(job_id, area=center[2])

        # Rotation: skip roofs already shown to this customer; when every
        # known candidate has been shown, start a fresh pass.
        exclude = _seen_cells(user_id, zipcode)
        cands, footprints_ok = pipeline.candidate_roofs(
            zipcode, center, count, progress, exclude_cells=exclude)
        if not cands and exclude:
            cands, footprints_ok = pipeline.candidate_roofs(
                zipcode, center, count, progress)
        _set_job(job_id, footprints_ok=footprints_ok)
        if not cands:
            _fail_job(job_id, "No addresses found near that zip. Try another."
                      " Your scan was refunded.")
            return
        # Damage pre-screen: cheap triage over the wide candidate pool,
        # then deep-dive only the worst-looking roofs.
        if GEMINI_KEY or grader or prescreener:
            houses = pipeline.prescreen_damage(GEMINI_KEY, cands, count,
                                               progress,
                                               prescreener=prescreener)
        else:
            # No grader available (local dev without API key): geographic
            # stride through the rotation-aware candidate pool.
            houses = pipeline._stride_fallback(cands, count)
        # Every micro-scanned roof counts as seen, lead or not, so the next
        # scan of this ZIP triages roofs farther out.
        triaged = [c for c in cands if "micro_score" in c]
        if not houses:
            if triaged:
                _record_seen(user_id, zipcode, triaged)
                _fail_job(job_id, f"No damaged roofs among the {len(triaged)}"
                          " roofs checked. Scan again to check roofs farther"
                          " out. Your scan was refunded.")
            else:
                _fail_job(job_id, "No addresses found near that zip."
                          " Try another. Your scan was refunded.")
            return
        for h in houses:
            h["key"] = (h.get("address") or "") + "|" + h.get("postcode", "")
        _run_houses(job_id, houses, center[2], cache=(zipcode, count),
                    grader=grader, user_id=user_id,
                    footprints_ok=footprints_ok)
        with _jobs_lock:
            ok = _jobs[job_id].get("status") == "done"
        if ok:
            _record_seen(user_id, zipcode, triaged)
    except Exception as e:
        _fail_job(job_id, f"Scan failed: {e}. Your scan was refunded.")


def _run_address_scan(job_id: str, house: dict, grader=None):
    try:
        house["key"] = house["address"] + "|" + house["postcode"]
        area = ", ".join(x for x in (house.get("city"),
                                     house.get("state")) if x) or house["address"]
        _set_job(job_id, phase="ziplookup",
                 msg=f"Located {house['address']}…", area=area)
        _run_houses(job_id, [house], area, cache=None, grader=grader)
    except Exception as e:
        _fail_job(job_id, f"Scan failed: {e}. Your scan was refunded.")


@app.post("/api/scan")
def start_scan():
    user, err = _require_verified()
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
        # Rotation: a cached result is only served when this customer has
        # NOT seen those roofs yet; otherwise a fresh scan covers new roofs.
        seen = _seen_cells(user["id"], zipcode)
        cached = None if seen else _cache_get(zipcode, count)
        if cached:
            _record_seen(user["id"], zipcode, cached.get("leads") or [])
            conn = _db()
            try:
                leads = _shape_leads(conn, cached.get("leads") or [], user)
            finally:
                conn.close()
            return jsonify({"ok": True, "cached": True, "quota": quota,
                            "payload": {"zip": cached.get("zip"),
                                        "area": cached.get("area"),
                                        "leads": leads,
                                        "footprints_ok": cached.get("footprints_ok", True),
                                        "scanned_at": cached.get("scanned_at")}})
        job_id = uuid.uuid4().hex[:12]
        with _jobs_lock:
            _jobs[job_id] = {"status": "running", "phase": "start", "done": 0,
                             "total": count, "msg": "Starting…", "zip": zipcode,
                             "single": False, "owner": user["id"]}
        t = threading.Thread(target=_run_scan,
                             args=(job_id, zipcode, count, user["id"]),
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
        # House-number mismatch: the geocoder returned a different number
        # than requested (e.g. asked 2349, got 2111). Don't silently scan
        # the wrong house — send the user to the pin picker instead.
        if house:
            want_num = re.match(r"\s*(\d+)", raw)
            got_num = re.match(r"\s*(\d+)", house.get("address", ""))
            if (want_num and got_num
                    and want_num.group(1) != got_num.group(1)):
                return jsonify({
                    "ok": False,
                    "error":
                        f"We found {house['address']} but couldn't locate "
                        f"{want_num.group(1)} on that street. Drag the pin "
                        f"onto the right rooftop — or check Street View "
                        f"to find it visually.",
                    "place": {"lat": house["lat"], "lng": house["lng"],
                              "label": house["address"]}}), 400
    if not house:
        place = None
        try:
            gp = pipeline.geocode_place(raw)
            if gp:
                place = {"lat": gp[0], "lng": gp[1], "label": gp[2]}
        except Exception:
            place = None
        body = {"ok": False, "error":
                f"Couldn't locate “{raw}”. Try a full street address "
                "with city and state, paste coordinates like "
                "39.4781,-80.19354 — or drop a pin on the aerial map below."}
        if place:
            body["place"] = place
        return jsonify(body), 400
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


@app.get("/api/place")
def place_lookup():
    """Best-effort map center for the pin-drop picker ("center on" box)."""
    user, err = _require_user()
    if err:
        return err
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"ok": False, "error": "Enter a city, ZIP, or street."}), 400
    gp = None
    try:
        gp = pipeline.geocode_place(q)
    except Exception:
        gp = None
    if not gp:
        return jsonify({"ok": False, "error": f"Couldn't locate “{q}”."}), 404
    return jsonify({"ok": True,
                    "place": {"lat": gp[0], "lng": gp[1], "label": gp[2]}})


@app.get("/api/scan/<job_id>")
def scan_status(job_id: str):
    user, err = _require_user()
    if err:
        return err
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        # Fall back to persisted scans (survives refresh + restart).
        conn = _db()
        try:
            row = conn.execute(
                "SELECT user_id, leads, area, msg FROM scan_jobs WHERE job_id=?",
                (job_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return jsonify({"ok": False, "error": "unknown job"}), 404
        if not user.get("is_admin") and row[0] != user["id"]:
            return jsonify({"ok": False, "error": "forbidden"}), 403
        try:
            leads = json.loads(row[1])
        except Exception:
            leads = []
        job = {"status": "done", "leads": leads, "area": row[2],
               "msg": row[3], "owner": row[0]}
    # Account-scoped: only the job's owner (or an admin) may read it.
    if not user.get("is_admin") and job.get("owner") != user["id"]:
        return jsonify({"ok": False, "error": "forbidden"}), 403
    out = {"ok": True, "status": job["status"],
           "phase": job.get("phase"), "done": job.get("done", 0),
           "total": job.get("total", 0), "msg": job.get("msg", ""),
           "footprints_ok": job.get("footprints_ok", True)}
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
    user, err = _require_verified()
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
        try:
            conn.execute("INSERT INTO unlocks (user_id, lead_key, unlocked_at)"
                         " VALUES (?,?,?)",
                         (user["id"], lead_key, time.time()))
        except sqlite3.IntegrityError:
            # Lost a race with another request for the same lead: it is
            # already unlocked, so treat this as the free duplicate case.
            pass
        else:
            _consume_unlock(conn, user["id"], False)
        quota = _quota(conn, user["id"], False)
    finally:
        conn.close()
    lead = _find_lead(lead_key)
    if lead:
        lead = {**lead, "lead_key": lead_key, "locked": False}
        conn2 = _db()
        try:
            fs = _for_sale_status(conn2, lead)
        finally:
            conn2.close()
        if fs is not None:
            lead["for_sale"] = fs
    return jsonify({"ok": True, "quota": quota, "lead": lead})


@app.post("/api/leads/review")
def review_lead():
    """Approve or reject a possibly-abandoned (flagged) lead.

    Approve spends one unlock and reveals the lead, exactly like unlocking.
    Reject dismisses it and grants one replacement unlock — the customer
    never pays an unlock for a dead property they didn't want.
    One decision per customer per lead: repeats change nothing.
    """
    user, err = _require_verified()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    lead_key = str(body.get("lead_key", "")).strip()
    action = str(body.get("action", "")).strip().lower()
    if not lead_key or action not in ("approve", "reject"):
        return jsonify({"ok": False,
                        "error": "Need a lead and action=approve|reject."}), 400
    conn = _db()
    try:
        if conn.execute("SELECT 1 FROM lead_reviews WHERE user_id=? AND lead_key=?",
                        (user["id"], lead_key)).fetchone():
            quota = _quota(conn, user["id"], user["is_admin"])
            return jsonify({"ok": True, "quota": quota,
                            "note": "already decided"})
        if conn.execute("SELECT 1 FROM unlocks WHERE user_id=? AND lead_key=?",
                        (user["id"], lead_key)).fetchone():
            return jsonify({"ok": False,
                            "error": "Lead already unlocked."}), 400
        # Only real, flagged leads can be reviewed. Without this, fabricated
        # keys could farm unlimited replacement unlocks via reject, and
        # approve could spend an unlock on a lead that doesn't exist.
        lead = _find_lead(lead_key)
        if lead is None:
            return jsonify({"ok": False,
                            "error": "Unknown lead."}), 404
        if not lead.get("needs_review"):
            return jsonify({"ok": False,
                            "error": "Lead is not flagged for review."}), 400
        try:
            conn.execute("INSERT INTO lead_reviews (user_id, lead_key, decision,"
                         " decided_at) VALUES (?,?,?,?)",
                         (user["id"], lead_key, action, time.time()))
        except sqlite3.IntegrityError:
            # Lost a race with another decision on the same lead: treat as
            # the already-decided case rather than erroring.
            conn.rollback()
            quota = _quota(conn, user["id"], user["is_admin"])
            return jsonify({"ok": True, "quota": quota,
                            "note": "already decided"})
        if action == "reject":
            if not user["is_admin"]:
                conn.execute("UPDATE users SET bonus_unlocks=bonus_unlocks+1"
                             " WHERE id=?", (user["id"],))
            conn.commit()
            quota = _quota(conn, user["id"], user["is_admin"])
            return jsonify({"ok": True, "quota": quota,
                            "replacement": not user["is_admin"]})
        # approve: same path as a normal unlock (spends one unlock)
        quota = _quota(conn, user["id"], user["is_admin"])
        if user["is_admin"]:
            conn.commit()
        else:
            if quota["unlocks_left"] <= 0:
                conn.rollback()
                err_code = ("trial_unlocks_exhausted"
                            if quota["plan"] == "trial" else "unlocks_exhausted")
                return jsonify({"ok": False, "error": err_code,
                                "quota": quota}), 402
            try:
                conn.execute("INSERT INTO unlocks (user_id, lead_key,"
                             " unlocked_at) VALUES (?,?,?)",
                             (user["id"], lead_key, time.time()))
            except sqlite3.IntegrityError:
                pass
            else:
                _consume_unlock(conn, user["id"], False)
            conn.commit()
            quota = _quota(conn, user["id"], False)
    finally:
        conn.close()
    lead = _find_lead(lead_key)
    if lead:
        lead = {**lead, "lead_key": lead_key, "locked": False}
        conn2 = _db()
        try:
            fs = _for_sale_status(conn2, lead)
        finally:
            conn2.close()
        if fs is not None:
            lead["for_sale"] = fs
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
    """Only mtthew.westfall@gmail.com can run test suite."""
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
        try:
            _stripe_tables(conn)
            pending = [{"email": r[0], "plan": r[1], "event": r[2],
                        "at": r[3]} for r in conn.execute(
                "SELECT email, plan, event_id, created_at FROM stripe_pending"
                " ORDER BY created_at DESC LIMIT 50").fetchall()]
        except Exception:
            pending = []
    finally:
        conn.close()
    return jsonify({"ok": True, "users": users,
                    "pending_payments": pending})


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
        _stripe_tables(conn)
        if not _apply_plan_email(conn, email, plan):
            return jsonify({"ok": False, "error": "No such account."}), 404
    finally:
        conn.close()
    return jsonify({"ok": True, "email": email, "plan": plan})


@app.get("/")
def landing():
    return send_from_directory("static", "landing.html")


@app.get("/app")
def index():
    return send_from_directory("static", "index.html")


@app.get("/health")
def health():
    return jsonify({"ok": True, "grader": bool(GEMINI_KEY)})


if __name__ == "__main__":
    _db().close()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)),
            threaded=True)
