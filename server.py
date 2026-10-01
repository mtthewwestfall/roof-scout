"""RoofScout server: zip in, ranked roof leads out."""
from __future__ import annotations

import json
import os
import re
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

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS scans (
        zip TEXT NOT NULL, count INTEGER NOT NULL, payload TEXT NOT NULL,
        created_at REAL NOT NULL, PRIMARY KEY (zip, count))""")
    return conn


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

        houses = pipeline.sample_addresses(zipcode, center, count, progress)
        if not houses:
            _set_job(job_id, status="error",
                     error="No addresses found near that zip. Try another.")
            return
        for h in houses:
            h["key"] = h["address"] + "|" + h["postcode"]

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

        for h in houses:
            b64 = h.pop("image_b64", None)
            h["img"] = f"data:image/jpeg;base64,{b64}" if b64 else ""
            h["maps_url"] = ("https://www.google.com/maps/search/?api=1&query="
                             f"{h['lat']},{h['lng']}")
            h["streetview_url"] = ("https://www.google.com/maps/@?api=1&map_action=pano"
                                   f"&viewpoint={h['lat']},{h['lng']}")
        leads = pipeline.sort_leads(houses)
        payload = {"zip": zipcode, "area": center[2], "leads": leads,
                   "scanned_at": time.time()}
        _cache_put(zipcode, count, payload)
        _set_job(job_id, status="done", leads=leads, area=center[2],
                 msg=f"Done — {len(leads)} roofs graded.")
    except Exception as e:
        _set_job(job_id, status="error", error=f"Scan failed: {e}")


@app.post("/api/scan")
def start_scan():
    body = request.get_json(force=True, silent=True) or {}
    zipcode = re.sub(r"\D", "", str(body.get("zip", "")))[:5]
    try:
        count = max(5, min(30, int(body.get("count", 20))))
    except Exception:
        count = 20
    if not re.fullmatch(r"\d{5}", zipcode):
        return jsonify({"ok": False, "error": "Enter a valid 5-digit US zip."}), 400
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


@app.get("/api/scan/<job_id>")
def scan_status(job_id: str):
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
