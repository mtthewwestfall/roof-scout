"""Roof Scout test suite: caps, admin, password gate, isolation, dedup.

Run: cd /tmp/rsfix && python -m pytest tests/ -q
"""
import json
import sys
import time

import pytest

sys.path.insert(0, "/tmp/rsfix")
import pipeline
import server


# ---------------- fixtures & helpers ----------------

@pytest.fixture
def db(monkeypatch, tmp_path):
    """Fresh temp DB per test; jobs cleared."""
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "t.db"))
    server._jobs.clear()
    conn = server._db()
    conn.close()
    yield
    server._jobs.clear()


@pytest.fixture
def app_client(db):
    server.app.config["TESTING"] = True
    return server.app.test_client()


def signup(client, email, password="password123", account_type="individual",
           company_name=""):
    r = client.post("/api/auth/signup", json={
        "email": email, "password": password,
        "account_type": account_type, "company_name": company_name})
    return r


def mklead(address="123 Main St", postcode="26554",
           lat=39.47810, lng=-80.19354, grade=2):
    return {"address": address, "postcode": postcode, "lat": lat, "lng": lng,
            "grade": grade,
            "maps_url": "https://www.google.com/maps/search/?api=1&query=1,2",
            "streetview_url": "https://www.google.com/maps/@?api=1&x=1"}


def seed_cache(zipcode="12345", count=20, leads=None):
    payload = {"zip": zipcode, "area": "Testville, WV",
               "leads": leads or [mklead()], "scanned_at": time.time()}
    conn = server._db()
    try:
        conn.execute("REPLACE INTO scans (zip, count, payload, created_at)"
                     " VALUES (?,?,?,?)",
                     (zipcode, count, json.dumps(payload), time.time()))
        conn.commit()
    finally:
        conn.close()
    return payload


def set_plan(email, plan, scans_used=0, unlocks_used=0, period_start=None):
    conn = server._db()
    try:
        conn.execute(
            "UPDATE users SET plan=?, cycle_scans_used=?, cycle_unlocks_used=?,"
            " period_start=? WHERE email=?",
            (plan, scans_used, unlocks_used, period_start or time.time(), email))
        conn.commit()
    finally:
        conn.close()


def quota_of(client):
    return client.get("/api/auth/me").get_json()["user"]["quota"]


# ---------------- company-name normalization ----------------

class TestNormalizeCompany:
    def test_suffixes_stripped(self):
        assert server._normalize_company("Acme Roofing LLC") == "acme roofing"
        assert server._normalize_company("Acme Roofing, Inc.") == "acme roofing"
        assert server._normalize_company("ACME ROOFING Co.") == "acme roofing"
        assert server._normalize_company("Acme Roofing Corp") == "acme roofing"
        assert server._normalize_company("Acme Roofing Ltd") == "acme roofing"

    def test_case_and_punctuation(self):
        assert server._normalize_company("  Acme-Roofing!! ") == "acme roofing"

    def test_bare_suffix_is_empty(self):
        assert server._normalize_company("LLC") == ""
        assert server._normalize_company("") == ""

    def test_interior_words_kept(self):
        # "inc" inside the name is not a suffix -> kept
        assert server._normalize_company("Incredible Roofs") == "incredible roofs"


class TestTrialPerCompany:
    def test_duplicate_email_still_blocked(self, app_client):
        assert signup(app_client, "a@x.com").status_code == 200
        r = signup(app_client, "a@x.com")
        assert r.status_code == 400

    def test_same_company_new_email_blocked(self, app_client):
        c = app_client
        assert signup(c, "one@acme.com", account_type="company",
                      company_name="Acme Roofing LLC").status_code == 200
        r = signup(c, "two@acme.com", account_type="company",
                   company_name="acme roofing, inc.")
        assert r.status_code == 400
        assert "already has an account" in r.get_json()["error"]

    def test_suffix_variant_blocked(self, app_client):
        c = app_client
        assert signup(c, "one@beta.com", account_type="company",
                      company_name="Beta Builders Co").status_code == 200
        r = signup(c, "two@beta.com", account_type="company",
                   company_name="BETA BUILDERS")
        assert r.status_code == 400

    def test_different_company_allowed(self, app_client):
        c = app_client
        assert signup(c, "one@acme.com", account_type="company",
                      company_name="Acme Roofing LLC").status_code == 200
        r = signup(c, "two@gamma.com", account_type="company",
                   company_name="Gamma Roofing LLC")
        assert r.status_code == 200

    def test_individuals_unaffected(self, app_client):
        c = app_client
        assert signup(c, "i1@x.com").status_code == 200
        assert signup(c, "i2@x.com").status_code == 200

    def test_similar_but_different_name_allowed(self, app_client):
        c = app_client
        assert signup(c, "one@acme.com", account_type="company",
                      company_name="Acme Roofing LLC").status_code == 200
        # conservative: only exact normalized matches blocked
        r = signup(c, "two@acme.com", account_type="company",
                   company_name="Acme Roofing Plus LLC")
        assert r.status_code == 200


# ---------------- plan caps & quota ----------------

class TestCaps:
    def test_trial_starts_full(self, app_client):
        signup(app_client, "t@x.com")
        q = quota_of(app_client)
        assert q["plan"] == "trial"
        assert q["scans_left"] == 1 and q["scans_cap"] == 1
        assert q["unlocks_left"] == 5 and q["unlocks_cap"] == 5

    def test_cached_zip_scan_consumes_scan(self, app_client):
        # FIX 1: cached hits cost a scan.
        seed_cache()
        signup(app_client, "t@x.com")
        r = app_client.post("/api/scan", json={"q": "12345"})
        body = r.get_json()
        assert r.status_code == 200 and body["cached"] is True
        assert body["quota"]["scans_left"] == 0
        assert len(body["payload"]["leads"]) == 1

    def test_cached_zip_scan_exhausted_402(self, app_client):
        seed_cache()
        signup(app_client, "t@x.com")
        assert app_client.post("/api/scan", json={"q": "12345"}).status_code == 200
        r = app_client.post("/api/scan", json={"q": "12345"})
        assert r.status_code == 402
        assert r.get_json()["error"] == "trial_scans_exhausted"

    def test_uncached_zip_scan_consumes_and_starts_job(self, app_client,
                                                      monkeypatch):
        def fake_run(job_id, zipcode, count, grader=None):
            server._set_job(job_id, status="done", leads=[], area="X")
        monkeypatch.setattr(server, "_run_scan", fake_run)
        signup(app_client, "t@x.com")
        r = app_client.post("/api/scan", json={"q": "99999"})
        body = r.get_json()
        assert r.status_code == 200 and body["job_id"]
        assert body["quota"]["scans_left"] == 0

    def test_single_address_scan_consumes_scan(self, app_client, monkeypatch):
        monkeypatch.setattr(
            pipeline, "geocode_latlng",
            lambda lat, lng: {"address": "6 Meadowlark Ln", "city": "Fairmont",
                              "state": "WV", "postcode": "26554", "county": "",
                              "area": "", "lat": lat, "lng": lng})

        def fake_run(job_id, house, grader=None):
            server._set_job(job_id, status="done", leads=[], area="X")
        monkeypatch.setattr(server, "_run_address_scan", fake_run)
        signup(app_client, "t@x.com")
        r = app_client.post("/api/scan", json={"q": "39.4781,-80.19354"})
        assert r.status_code == 200
        assert r.get_json()["quota"]["scans_left"] == 0

    def test_failed_geocode_consumes_nothing(self, app_client, monkeypatch):
        monkeypatch.setattr(pipeline, "geocode_address", lambda q: None)
        signup(app_client, "t@x.com")
        r = app_client.post("/api/scan", json={"q": "Nope Nowhere XX"})
        assert r.status_code == 400
        assert quota_of(app_client)["scans_left"] == 1

    def test_anon_scan_401(self, app_client):
        seed_cache()
        assert app_client.post("/api/scan", json={"q": "12345"}).status_code == 401

    def test_unlock_consumes_and_duplicate_free(self, app_client):
        signup(app_client, "t@x.com")
        key = server._lead_key(mklead())
        r1 = app_client.post("/api/leads/unlock", json={"lead_key": key})
        assert r1.status_code == 200
        assert r1.get_json()["quota"]["unlocks_left"] == 4
        r2 = app_client.post("/api/leads/unlock", json={"lead_key": key})
        assert r2.status_code == 200
        assert r2.get_json()["quota"]["unlocks_left"] == 4  # no double charge

    def test_trial_unlocks_exhausted_402(self, app_client):
        signup(app_client, "t@x.com")
        for i in range(5):
            r = app_client.post("/api/leads/unlock",
                                json={"lead_key": f"key{i}"})
            assert r.status_code == 200
        r = app_client.post("/api/leads/unlock", json={"lead_key": "key5"})
        assert r.status_code == 402
        assert r.get_json()["error"] == "trial_unlocks_exhausted"

    def test_starter_26th_unlock_402(self, app_client):
        signup(app_client, "s@x.com")
        set_plan("s@x.com", "starter", unlocks_used=25)
        r = app_client.post("/api/leads/unlock", json={"lead_key": "k"})
        assert r.status_code == 402
        assert r.get_json()["error"] == "unlocks_exhausted"

    def test_starter_25th_unlock_ok(self, app_client):
        signup(app_client, "s@x.com")
        set_plan("s@x.com", "starter", unlocks_used=24)
        r = app_client.post("/api/leads/unlock", json={"lead_key": "k"})
        assert r.status_code == 200
        assert r.get_json()["quota"]["unlocks_left"] == 0

    def test_pro_caps(self, app_client):
        signup(app_client, "p@x.com")
        set_plan("p@x.com", "pro")
        q = quota_of(app_client)
        assert q["scans_cap"] == 8 and q["scans_left"] == 8
        assert q["unlocks_cap"] == 100 and q["unlocks_left"] == 100

    def test_masked_until_unlocked(self, app_client):
        signup(app_client, "t@x.com")
        conn = server._db()
        try:
            shaped = server._shape_leads(conn, [mklead()], {"id": "x",
                                                           "is_admin": False})
        finally:
            conn.close()
        c = shaped[0]
        assert c["locked"] is True
        assert c["address"] != "123 Main St"  # masked
        assert "maps_url" not in c and "streetview_url" not in c
        assert (c["lat"], c["lng"]) != (39.47810, -80.19354)  # jittered

    def test_unlocked_reveals_full(self, app_client):
        signup(app_client, "t@x.com")
        lead = mklead()
        key = server._lead_key(lead)
        app_client.post("/api/leads/unlock", json={"lead_key": key})
        conn = server._db()
        try:
            uid = conn.execute("SELECT id FROM users WHERE email=?",
                               ("t@x.com",)).fetchone()[0]
            shaped = server._shape_leads(conn, [lead],
                                         {"id": uid, "is_admin": False})
        finally:
            conn.close()
        c = shaped[0]
        assert c["locked"] is False
        assert c["address"] == "123 Main St"
        assert "maps_url" in c

    def test_admin_unlimited(self, app_client):
        seed_cache()
        signup(app_client, server.OWNER_EMAIL)
        q = quota_of(app_client)
        assert q["scans_left"] == -1 and q["unlocks_left"] == -1
        r = app_client.post("/api/scan", json={"q": "12345"})
        assert r.status_code == 200  # no consumption, still fine
        r = app_client.post("/api/leads/unlock", json={"lead_key": "k"})
        assert r.status_code == 200

    def test_cycle_reset_after_30_days(self, app_client):
        signup(app_client, "s@x.com")
        set_plan("s@x.com", "starter", scans_used=2, unlocks_used=25,
                 period_start=time.time() - 31 * 86400)
        q = quota_of(app_client)
        assert q["scans_left"] == 2 and q["unlocks_left"] == 25

    def test_set_plan_gives_fresh_month(self, app_client, monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", "pw")
        signup(app_client, "s@x.com")
        set_plan("s@x.com", "starter", scans_used=2, unlocks_used=25)
        c2 = server.app.test_client()
        assert c2.post("/api/admin/login",
                       json={"password": "pw"}).status_code == 200
        r = c2.post("/api/admin/set-plan",
                    json={"email": "s@x.com", "plan": "starter"})
        assert r.status_code == 200
        q = quota_of(app_client)
        assert q["scans_left"] == 2 and q["unlocks_left"] == 25


# ---------------- admin API ----------------

class TestAdmin:
    def test_users_anon_403(self, app_client):
        assert app_client.get("/api/admin/users").status_code == 403

    def test_users_nonadmin_403(self, db):
        a = server.app.test_client()
        owner = server.app.test_client()
        signup(a, "a@x.com")
        signup(owner, server.OWNER_EMAIL)
        # a@x.com (non-admin) is denied...
        assert a.get("/api/admin/users").status_code == 403
        # ...while the owner account is allowed
        assert owner.get("/api/admin/users").status_code == 200

    def test_users_owner_account_lists(self, db):
        owner = server.app.test_client()
        u = server.app.test_client()
        signup(owner, server.OWNER_EMAIL)
        signup(u, "u@x.com")
        r = owner.get("/api/admin/users")
        assert r.status_code == 200
        users = r.get_json()["users"]
        emails = [u["email"] for u in users]
        assert server.OWNER_EMAIL in emails and "u@x.com" in emails
        u = [x for x in users if x["email"] == "u@x.com"][0]
        assert u["quota"]["scans_cap"] == 1  # quota included per row

    def test_set_plan_anon_403(self, app_client):
        r = app_client.post("/api/admin/set-plan",
                            json={"email": "x@x.com", "plan": "pro"})
        assert r.status_code == 403

    def test_set_plan_ok(self, app_client, monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", "pw")
        signup(app_client, "u@x.com")
        c2 = server.app.test_client()
        c2.post("/api/admin/login", json={"password": "pw"})
        r = c2.post("/api/admin/set-plan",
                    json={"email": "u@x.com", "plan": "pro"})
        assert r.status_code == 200
        assert r.get_json()["plan"] == "pro"

    def test_set_plan_bad_plan_400(self, app_client, monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", "pw")
        c2 = server.app.test_client()
        c2.post("/api/admin/login", json={"password": "pw"})
        r = c2.post("/api/admin/set-plan",
                    json={"email": "u@x.com", "plan": "mega"})
        assert r.status_code == 400

    def test_set_plan_unknown_email_404(self, app_client, monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", "pw")
        c2 = server.app.test_client()
        c2.post("/api/admin/login", json={"password": "pw"})
        r = c2.post("/api/admin/set-plan",
                    json={"email": "nobody@x.com", "plan": "pro"})
        assert r.status_code == 404

    def test_admin_page_served_anon(self, app_client):
        r = app_client.get("/admin")
        assert r.status_code == 200
        assert b"Roof Scout" in r.data

    def test_admin_page_served_for_admin(self, db):
        # regression: used to 500 for admins (view returned None)
        c = server.app.test_client()
        signup(c, server.OWNER_EMAIL)
        r = c.get("/admin")
        assert r.status_code == 200

    def test_admin_page_served_with_password_session(self, app_client,
                                                     monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", "pw")
        app_client.post("/api/admin/login", json={"password": "pw"})
        assert app_client.get("/admin").status_code == 200


# ---------------- admin password gate ----------------

class TestPasswordGate:
    def test_wrong_password_401(self, app_client, monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", "correct")
        r = app_client.post("/api/admin/login", json={"password": "wrong"})
        assert r.status_code == 401
        assert r.get_json()["error"] == "bad_password"

    def test_no_password_configured_401(self, app_client, monkeypatch):
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        r = app_client.post("/api/admin/login", json={"password": "anything"})
        assert r.status_code == 401

    def test_correct_password_sets_cookie(self, app_client, monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", "s3cret")
        r = app_client.post("/api/admin/login", json={"password": "s3cret"})
        assert r.status_code == 200
        assert "rs_admin=" in r.headers.get("Set-Cookie", "")
        assert "HttpOnly" in r.headers.get("Set-Cookie", "")

    def test_cookie_grants_admin_api(self, app_client, monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", "s3cret")
        app_client.post("/api/admin/login", json={"password": "s3cret"})
        assert app_client.get("/api/admin/users").status_code == 200

    def test_bad_cookie_still_403(self, app_client):
        app_client.set_cookie("rs_admin", "bogus")
        assert app_client.get("/api/admin/users").status_code == 403

    def test_admin_cookie_does_not_log_in_user(self, app_client, monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", "s3cret")
        app_client.post("/api/admin/login", json={"password": "s3cret"})
        # admin-password session must not double as a user session
        assert app_client.get("/api/auth/me").status_code == 401

    def test_logout_clears_admin(self, app_client, monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", "s3cret")
        app_client.post("/api/admin/login", json={"password": "s3cret"})
        assert app_client.get("/api/admin/users").status_code == 200
        app_client.post("/api/admin/logout")
        assert app_client.get("/api/admin/users").status_code == 403

    def test_owner_needs_no_password(self, db):
        c = server.app.test_client()
        signup(c, server.OWNER_EMAIL)
        assert c.get("/api/admin/users").status_code == 200


# ---------------- cross-account isolation (FIX 2 & 4) ----------------

def _user_id(email):
    conn = server._db()
    try:
        return conn.execute("SELECT id FROM users WHERE email=?",
                            (email,)).fetchone()[0]
    finally:
        conn.close()


def _inject_job(job_id, owner, leads=None, single=False, status="done"):
    server._jobs[job_id] = {"status": status, "owner": owner,
                            "leads": leads or [], "single": single,
                            "area": "Testville", "phase": "done",
                            "done": 1, "total": 1, "msg": "Done"}


class TestIsolation:
    def test_other_users_job_403(self, db):
        a = server.app.test_client()
        b = server.app.test_client()
        signup(a, "a@x.com")
        signup(b, "b@x.com")
        _inject_job("jobB", _user_id("b@x.com"), leads=[mklead()])
        r = a.get("/api/scan/jobB")
        assert r.status_code == 403
        assert r.get_json()["error"] == "forbidden"

    def test_own_job_200(self, db):
        a = server.app.test_client()
        signup(a, "a@x.com")
        _inject_job("jobA", _user_id("a@x.com"), leads=[mklead()])
        r = a.get("/api/scan/jobA")
        assert r.status_code == 200
        assert r.get_json()["status"] == "done"

    def test_admin_may_view_any_job(self, db):
        admin = server.app.test_client()
        b = server.app.test_client()
        signup(admin, server.OWNER_EMAIL)
        signup(b, "b@x.com")
        _inject_job("jobB", _user_id("b@x.com"), leads=[mklead()])
        r = admin.get("/api/scan/jobB")
        assert r.status_code == 200

    def test_unknown_job_404(self, db):
        a = server.app.test_client()
        signup(a, "a@x.com")
        assert a.get("/api/scan/nope123").status_code == 404

    def test_anon_job_401(self, db):
        a = server.app.test_client()
        assert a.get("/api/scan/whatever").status_code == 401

    def test_done_job_leads_masked_per_viewer(self, db):
        a = server.app.test_client()
        b = server.app.test_client()
        signup(a, "a@x.com")
        signup(b, "b@x.com")
        lead = mklead()
        _inject_job("jobA", _user_id("a@x.com"), leads=[lead])
        # A sees it masked before unlocking...
        masked = a.get("/api/scan/jobA").get_json()["leads"][0]
        assert masked["locked"] is True
        # ...B cannot see it at all...
        assert b.get("/api/scan/jobA").status_code == 403
        # ...A unlocks -> A sees full address, B's view of the same lead
        # stays masked (unlock state is per-account).
        key = server._lead_key(lead)
        assert a.post("/api/leads/unlock",
                      json={"lead_key": key}).status_code == 200
        full = a.get("/api/scan/jobA").get_json()["leads"][0]
        assert full["locked"] is False
        assert full["address"] == "123 Main St"
        conn = server._db()
        try:
            bshape = server._shape_leads(conn, [lead],
                                         {"id": _user_id("b@x.com"),
                                          "is_admin": False})[0]
        finally:
            conn.close()
        assert bshape["locked"] is True
        assert bshape["address"] != "123 Main St"

    def test_unlock_quotas_are_per_account(self, db):
        a = server.app.test_client()
        b = server.app.test_client()
        signup(a, "a@x.com")
        signup(b, "b@x.com")
        key = server._lead_key(mklead())
        assert a.post("/api/leads/unlock",
                      json={"lead_key": key}).status_code == 200
        assert b.post("/api/leads/unlock",
                      json={"lead_key": key}).status_code == 200
        qa, qb = quota_of(a), quota_of(b)
        assert qa["unlocks_left"] == 4 and qb["unlocks_left"] == 4
        # separate unlock rows: B unlocking did not ride on A's
        conn = server._db()
        try:
            n = conn.execute("SELECT COUNT(*) FROM unlocks WHERE lead_key=?",
                             (key,)).fetchone()[0]
        finally:
            conn.close()
        assert n == 2

    def test_single_address_job_unmasked_for_owner_only(self, db):
        a = server.app.test_client()
        b = server.app.test_client()
        signup(a, "a@x.com")
        signup(b, "b@x.com")
        _inject_job("jobS", _user_id("a@x.com"), leads=[mklead()],
                    single=True)
        own = a.get("/api/scan/jobS").get_json()["leads"][0]
        assert own["locked"] is False  # single-address: full detail, no mask
        assert own["address"] == "123 Main St"
        assert b.get("/api/scan/jobS").status_code == 403

    def test_scan_start_tags_owner(self, db, monkeypatch):
        def fake_run(job_id, zipcode, count, grader=None):
            server._set_job(job_id, status="done", leads=[], area="X")
        monkeypatch.setattr(server, "_run_scan", fake_run)
        a = server.app.test_client()
        signup(a, "a@x.com")
        job_id = a.post("/api/scan",
                        json={"q": "99999"}).get_json()["job_id"]
        assert server._jobs[job_id]["owner"] == _user_id("a@x.com")


class TestObscuredVerdict:
    def test_tree_canopy_gets_plain_verdict(self):
        h = {"grade": 0, "confidence": "low",
             "obstruction": "heavy tree canopy coverage",
             "evidence": ["roof largely obscured by trees"]}
        v = pipeline.obscured_verdict(h)
        assert v and v["verdict"] == "tree-obscured"
        assert "leaf-off" in v["verdict_note"]

    def test_graded_roof_no_verdict(self):
        h = {"grade": 3, "confidence": "medium",
             "obstruction": "some tree shadows", "evidence": []}
        assert pipeline.obscured_verdict(h) is None

    def test_clear_view_no_verdict(self):
        h = {"grade": 0, "confidence": "low",
             "obstruction": "deep shadow, out of frame", "evidence": []}
        assert pipeline.obscured_verdict(h) is None

    def test_grade_roofs_attaches_verdict(self):
        houses = [{"key": "k1", "image_b64": "x"}]
        def grader(imgs):
            return [{"grade": 0, "confidence": "low",
                     "evidence": ["dense canopy over roof"],
                     "primary_material": "asphalt shingle",
                     "pitch_estimate": "", "obstruction_notes": "tree cover",
                     "damage_boxes": []}]
        out = pipeline.grade_roofs("key", houses, grader=grader)
        assert out[0]["verdict"] == "tree-obscured"
        assert out[0]["verdict_note"]
