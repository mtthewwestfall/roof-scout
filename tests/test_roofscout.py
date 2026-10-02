"""Roof Scout test suite: caps, admin, password gate, isolation, dedup.

Run: cd /tmp/rsfix && python -m pytest tests/ -q
"""
import json
import math
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pipeline
import server


# ---------------- fixtures & helpers ----------------

@pytest.fixture
def db(monkeypatch, tmp_path):
    """Fresh temp DB per test; jobs cleared."""
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "t.db"))
    # Email verification is real code, but tests never hit the network:
    # pretend every confirmation email sends fine.
    monkeypatch.setattr(server, "_send_email", lambda *a: True)
    server._jobs.clear()
    conn = server._db()
    conn.close()
    yield
    server._jobs.clear()


@pytest.fixture
def app_client(db):
    server.app.config["TESTING"] = True
    return server.app.test_client()


def signup(client, email, password="TestPass99!", account_type="individual",
           company_name="", verify=True):
    r = client.post("/api/auth/signup", json={
        "email": email, "password": password,
        "account_type": account_type, "company_name": company_name})
    if verify and r.status_code == 200:
        # Complete the email-confirmation step the way a user would: pull
        # the token the server issued and visit the verify link, which
        # also establishes the session cookie.
        conn = server._db()
        try:
            row = conn.execute(
                "SELECT token FROM verification_tokens WHERE email=?"
                " ORDER BY created_at DESC LIMIT 1", (email,)).fetchone()
        finally:
            conn.close()
        if row:
            client.get(f"/api/auth/verify?token={row[0]}")
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
        def fake_run(job_id, zipcode, count, user_id=None, grader=None):
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


class TestAdminTestRunner:
    def test_test_runner_anon_403(self, app_client):
        assert app_client.get("/api/admin/test-results").status_code == 403
        assert app_client.post("/api/admin/run-tests").status_code == 403

    def test_test_runner_unauthorized_email_403(self, app_client, monkeypatch):
        signup(app_client, "other@example.com")
        r1 = app_client.get("/api/admin/test-results")
        assert r1.status_code == 403
        assert r1.get_json()["error"] == "admin_required"

        monkeypatch.setenv("ADMIN_PASSWORD", "pw")
        app_client.post("/api/admin/login", json={"password": "pw"})
        r2 = app_client.post("/api/admin/run-tests")
        assert r2.status_code == 403
        assert r2.get_json()["error"] == "unauthorized_email"

    def test_test_runner_authorized_email_gmail(self, app_client, monkeypatch):
        signup(app_client, "mtthew.westfall@gmail.com")
        r_get = app_client.get("/api/admin/test-results")
        assert r_get.status_code == 200
        assert r_get.get_json()["can_run_tests"] is True

        import pytest
        def fake_pytest_main(args, plugins=None):
            if plugins and len(plugins) > 0:
                class FakeReport:
                    when = "call"
                    nodeid = "tests/test_roofscout.py::TestSample::test_one"
                    location = ("tests/test_roofscout.py", 10, "test_one")
                    outcome = "passed"
                    duration = 0.05
                    failed = False
                    longrepr = None
                plugins[0].pytest_runtest_logreport(FakeReport())
            return 0

        monkeypatch.setattr(pytest, "main", fake_pytest_main)

        r_post = app_client.post("/api/admin/run-tests")
        assert r_post.status_code == 200
        res = r_post.get_json()
        assert res["ok"] is True
        assert res["total"] == 1

    def test_allowed_test_emails_exact(self):
        assert server.ALLOWED_TEST_EMAILS == {"mtthew.westfall@gmail.com"}


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
        def fake_run(job_id, zipcode, count, user_id=None, grader=None):
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


# ---------------- security hardening (2026-10-01) ----------------

class TestPasswordPolicy:
    def test_common_password_rejected(self, app_client):
        r = signup(app_client, "a@t.com", password="password")
        assert r.status_code == 400
        assert "too common" in r.get_json()["error"]

    def test_common_password_case_insensitive(self, app_client):
        r = signup(app_client, "b@t.com", password="Password123")
        assert r.status_code == 400

    def test_short_password_still_rejected(self, app_client):
        r = signup(app_client, "c@t.com", password="short1")
        assert r.status_code == 400

    def test_strong_password_accepted(self, app_client):
        r = signup(app_client, "d@t.com", password="Truly-Unique-99!")
        assert r.status_code == 200


class TestSecurityHeaders:
    def test_headers_present(self, app_client):
        r = app_client.get("/")
        assert r.headers.get("Strict-Transport-Security", "").startswith(
            "max-age=31536000")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("X-Frame-Options") == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in r.headers.get(
            "Content-Security-Policy", "")

    def test_csp_allows_leaflet_and_tiles(self, app_client):
        csp = app_client.get("/").headers.get("Content-Security-Policy", "")
        assert "https://unpkg.com" in csp
        assert "data:" in csp


class TestFailedScanRefund:
    def _user_with_scan(self, app_client, email="r@t.com"):
        r = signup(app_client, email)
        assert r.status_code == 200
        conn = server._db()
        try:
            row = conn.execute("SELECT id FROM users WHERE email=?",
                               (email,)).fetchone()
            uid = row[0]
            server._consume_scan(conn, uid, False)
            used = conn.execute("SELECT trial_scans_used FROM users"
                                " WHERE id=?", (uid,)).fetchone()[0]
            assert used == 1
        finally:
            conn.close()
        return uid

    def test_fail_job_refunds_trial_scan(self, app_client, db):
        uid = self._user_with_scan(app_client)
        server._jobs["jobxyz"] = {"status": "running", "owner": uid}
        server._fail_job("jobxyz", "boom")
        assert server._jobs["jobxyz"]["status"] == "error"
        conn = server._db()
        try:
            used = conn.execute("SELECT trial_scans_used FROM users"
                                " WHERE id=?", (uid,)).fetchone()[0]
        finally:
            conn.close()
        assert used == 0

    def test_refund_never_goes_negative(self, app_client, db):
        uid = self._user_with_scan(app_client)
        conn = server._db()
        try:
            server._refund_scan(conn, uid)   # 1 -> 0
            server._refund_scan(conn, uid)   # stays 0
            used = conn.execute("SELECT trial_scans_used FROM users"
                                " WHERE id=?", (uid,)).fetchone()[0]
        finally:
            conn.close()
        assert used == 0


class TestLoginThrottle:
    def test_throttle_helpers(self):
        server._login_attempts.clear()
        for _ in range(8):
            assert not server._login_throttled("1.2.3.4", "e@t.com")
            server._login_failed("1.2.3.4", "e@t.com")
        assert server._login_throttled("1.2.3.4", "e@t.com")
        server._login_ok("1.2.3.4", "e@t.com")
        assert not server._login_throttled("1.2.3.4", "e@t.com")

    def test_endpoint_throttles_after_8_bad_logins(self, app_client):
        signup(app_client, "f@t.com")
        for _ in range(8):
            r = app_client.post("/api/auth/login",
                                json={"email": "f@t.com",
                                      "password": "wrong-pw"})
            assert r.status_code == 401
        r = app_client.post("/api/auth/login",
                            json={"email": "f@t.com",
                                  "password": "wrong-pw"})
        assert r.status_code == 429


class TestUnlockRace:
    def test_duplicate_unlock_free_after_race(self, app_client, db):
        r = signup(app_client, "g@t.com")
        assert r.status_code == 200
        conn = server._db()
        try:
            uid = conn.execute("SELECT id FROM users WHERE email=?",
                               ("g@t.com",)).fetchone()[0]
            # Simulate the race winner already inserting the row.
            conn.execute("INSERT INTO unlocks (user_id, lead_key, unlocked_at)"
                         " VALUES (?,?,?)", (uid, "race-key-1", time.time()))
            conn.execute("UPDATE users SET trial_unlocks_used=1 WHERE id=?",
                         (uid,))
            conn.commit()
        finally:
            conn.close()
        # The SELECT-then-INSERT loser path is covered by the early-return;
        # exercise the endpoint for a genuinely new key too.
        r = app_client.post("/api/leads/unlock", json={"lead_key": "k2"})
        assert r.status_code == 200
        q = r.get_json()["quota"]
        assert q["unlocks_used"] == 2
        # Duplicate unlock of k2 stays free.
        r = app_client.post("/api/leads/unlock", json={"lead_key": "k2"})
        assert r.status_code == 200
        assert r.get_json()["quota"]["unlocks_used"] == 2


# ---------------- scan rotation (repeat-ZIP new roofs) ----------------

class TestRotation:
    def test_seen_roundtrip(self, db):
        signup(server.app.test_client(), "rot@x.com")
        uid = _user_id("rot@x.com")
        server._record_seen(uid, "12345",
                            [{"lat": 39.4781, "lng": -80.1935}])
        cells = server._seen_cells(uid, "12345")
        assert cells == {(round(39.4781 * 3000), round(-80.1935 * 3000))}
        assert server._seen_cells(uid, "99999") == set()
        assert server._seen_cells("other-id", "12345") == set()
        assert server._seen_cells(None, "12345") == set()

    def test_candidate_roofs_excludes_seen(self, monkeypatch):
        pool = [{"lat": 39.0 + i * 0.001, "lng": -80.0, "address": "",
                 "building": "house"} for i in range(10)]
        monkeypatch.setattr(pipeline, "_overpass_buildings",
                            lambda *a, **k: pool)
        monkeypatch.setattr(pipeline, "_area_context", lambda *a: {})
        monkeypatch.setattr(pipeline, "_grid_points", lambda *a, **k: [])
        center = (39.0, -80.0, "X", "City")
        allc = pipeline.candidate_roofs("12345", center, 5)
        assert len(allc) == 10  # full pool goes to the pre-screen triage
        excl = {pipeline._cell_of(allc[0]["lat"], allc[0]["lng"])}
        again = pipeline.candidate_roofs("12345", center, 5,
                                         exclude_cells=excl)
        assert len(again) == 9
        assert all(pipeline._cell_of(h["lat"], h["lng"]) not in excl
                   for h in again)

    def test_candidate_pool_is_nearest_200(self, monkeypatch):
        pool = [{"lat": 39.0 + i * 0.001, "lng": -80.0, "address": "",
                 "building": "house"} for i in range(260)]
        asked = {}
        def fake_overpass(z, la, ln, limit=120):
            asked["limit"] = limit
            return pool
        monkeypatch.setattr(pipeline, "_overpass_buildings", fake_overpass)
        monkeypatch.setattr(pipeline, "_area_context", lambda *a: {})
        monkeypatch.setattr(pipeline, "_grid_points",
                            lambda *a, **k: pytest.fail("grid used"))
        center = (39.1, -80.0, "X", "City")
        out = pipeline.candidate_roofs("12345", center, 20)
        assert len(out) == pipeline.MICRO_SCAN_POOL == 200
        assert asked["limit"] >= 200
        far = max(abs(h["lat"] - 39.1) for h in out)
        assert far <= 0.1 + 1e-9

    def test_vacant_buildings_stay_in_pool_flagged(self, monkeypatch):
        houses = [{"lat": 39.0 + i * 0.001, "lng": -80.0, "address": "",
                   "building": "house", "vacant": i % 2 == 0}
                  for i in range(10)]
        monkeypatch.setattr(pipeline, "_overpass_buildings",
                            lambda *a, **k: houses)
        monkeypatch.setattr(pipeline, "_area_context", lambda *a: {})
        monkeypatch.setattr(pipeline, "_grid_points", lambda *a, **k: [])
        out = pipeline.candidate_roofs("12345", (39.0, -80.0, "X", "City"), 20)
        assert len(out) == 10
        assert sum(1 for h in out if h["vacant"]) == 5

    def test_thin_footprints_topped_up_to_200(self, monkeypatch):
        monkeypatch.setattr(pipeline, "_overpass_buildings",
                            lambda *a, **k: [])
        monkeypatch.setattr(pipeline, "_area_context", lambda *a: {})
        asked = {}
        def fake_grid(z, c, n, **k):
            asked["n"] = n
            return []
        monkeypatch.setattr(pipeline, "_grid_points", fake_grid)
        pipeline.candidate_roofs("12345", (39.0, -80.0, "X", "City"), 20)
        assert asked["n"] == 200

    def test_grid_densifies_to_requested_count(self):
        center = (39.0, -80.0, "X", "City")
        pts = pipeline._grid_points("12345", center, 200)
        assert len({pipeline._cell_of(p["lat"], p["lng"]) for p in pts}) == 200
        assert len(pipeline._grid_points("12345", center, 20)) == 20

    def test_grid_widens_past_checked_cells(self):
        center = (39.0, -80.0, "X", "City")

        def dist(p):
            return math.hypot(p["lat"] - 39.0,
                              (p["lng"] + 80.0) * math.cos(math.radians(39.0)))

        first = pipeline._grid_points("12345", center, 200)
        seen = {pipeline._cell_of(p["lat"], p["lng"]) for p in first}
        second = pipeline._grid_points("12345", center, 200, exclude=seen)
        assert len(second) == 200
        assert not seen & {pipeline._cell_of(p["lat"], p["lng"])
                           for p in second}
        assert min(map(dist, second)) >= max(map(dist, first))
        assert pipeline._grid_points("12345", center, 200) == first

    def test_no_footprints_still_micro_scans_200(self, monkeypatch):
        monkeypatch.setattr(pipeline, "_overpass_buildings",
                            lambda *a, **k: [])
        monkeypatch.setattr(pipeline, "_area_context", lambda *a: {})
        out = pipeline.candidate_roofs("12345", (39.0, -80.0, "X", "City"), 20)
        assert len(out) == 200

    def _scan_env(self, monkeypatch, n):
        pool = [{"lat": 39.0 + i * 0.001, "lng": -80.0, "address": "",
                 "building": "house"} for i in range(n)]
        monkeypatch.setattr(pipeline, "zip_center",
                            lambda z: (39.0, -80.0, "Testville", "Testville"))
        monkeypatch.setattr(pipeline, "_overpass_buildings",
                            lambda *a, **k: pool)
        monkeypatch.setattr(pipeline, "_area_context", lambda *a: {})
        monkeypatch.setattr(pipeline, "_grid_points", lambda *a, **k: [])
        monkeypatch.setattr(pipeline, "roof_image",
                            lambda lat, lng, z=20: (b"img", z, "esri"))
        monkeypatch.setattr(pipeline, "attach_addresses",
                            lambda houses, progress=None: houses)

    def test_zero_lead_scan_moves_outward(self, db, monkeypatch):
        self._scan_env(monkeypatch, 300)
        server._jobs["j1"] = {"status": "running"}
        server._run_scan("j1", "12345", 20, user_id="u1",
                         prescreener=lambda b: [1] * len(b))
        assert server._jobs["j1"]["status"] == "error"
        assert "200 roofs checked" in server._jobs["j1"]["error"]
        assert len(server._seen_cells("u1", "12345")) == 200
        server._jobs["j2"] = {"status": "running"}
        server._run_scan("j2", "12345", 20, user_id="u1",
                         prescreener=lambda b: [1] * len(b))
        assert "100 roofs checked" in server._jobs["j2"]["error"]
        assert len(server._seen_cells("u1", "12345")) == 300

    def test_lead_scan_records_every_triaged_roof(self, db, monkeypatch):
        self._scan_env(monkeypatch, 250)

        def grader(imgs):
            return [{"grade": 2, "abandoned": False, "confidence": "high",
                     "evidence": [], "material": "", "pitch": "",
                     "obstruction": "", "damage_boxes": []} for _ in imgs]

        server._jobs["j1"] = {"status": "running"}
        server._run_scan("j1", "12345", 5, user_id="u1", grader=grader,
                         prescreener=lambda b: [3] * len(b))
        assert server._jobs["j1"]["status"] == "done"
        assert len(server._jobs["j1"]["leads"]) == 5
        assert len(server._seen_cells("u1", "12345")) == 200

    def test_cached_scan_not_served_after_seen(self, app_client, monkeypatch):
        # A cached ZIP is NOT served to a customer who already saw those
        # roofs; they get a fresh scan job for new rooftops instead.
        def fake_run(job_id, zipcode, count, user_id=None, grader=None):
            server._set_job(job_id, status="done", leads=[], area="X")
        monkeypatch.setattr(server, "_run_scan", fake_run)
        signup(app_client, "rot@x.com")
        uid = _user_id("rot@x.com")
        server._record_seen(uid, "99999", [{"lat": 39.1, "lng": -80.1}])
        server._cache_put("99999", 20, {"zip": "99999", "area": "X",
                                        "leads": [], "scanned_at": 0.0})
        r = app_client.post("/api/scan", json={"q": "99999", "count": 20})
        body = r.get_json()
        assert r.status_code == 200 and body.get("job_id")
        assert "cached" not in body

    def test_cached_scan_served_when_nothing_seen(self, app_client):
        signup(app_client, "fresh@x.com")
        seed_cache("12345", 20)
        r = app_client.post("/api/scan", json={"q": "12345", "count": 20})
        body = r.get_json()
        assert r.status_code == 200 and body.get("cached") is True
        # Serving the cached roofs records them as seen for this customer.
        uid = _user_id("fresh@x.com")
        assert server._seen_cells(uid, "12345")


# ---------------- damage pre-screen ----------------

class TestPrescreen:
    def _cands(self, n):
        return [{"lat": 39.0 + i * 0.002, "lng": -80.0,
                 "address": f"{i} Main St", "postcode": "12345",
                 "key": f"{i} Main St|12345"} for i in range(n)]

    def test_picks_worst_first_zero_sinks(self, monkeypatch):
        cands = self._cands(10)
        monkeypatch.setattr(pipeline, "roof_image",
                            lambda lat, lng, z=20: (b"img", z, "esri"))
        vals = iter([1, 0, 5, 2, 0, 4, 1, 3, 2, 5])
        out = pipeline.prescreen_damage(
            "k", cands, 3,
            prescreener=lambda b64s: [next(vals) for _ in b64s])
        # scores idx0..9: 1,0,5,2,0,4,1,3,2,5 -> worst first, 0s last
        assert [c["address"] for c in out] == ["2 Main St", "9 Main St",
                                              "5 Main St"]
        assert all("image_b64" not in c for c in out)

    def test_falls_back_on_triage_failure(self, monkeypatch):
        cands = self._cands(10)
        monkeypatch.setattr(pipeline, "roof_image",
                            lambda lat, lng, z=20: (b"img", z, "esri"))
        out = pipeline.prescreen_damage("k", cands, 4,
                                        prescreener=lambda b64s: None)
        assert len(out) == 4  # geographic stride fallback

    def test_falls_back_when_no_images(self, monkeypatch):
        cands = self._cands(10)
        monkeypatch.setattr(pipeline, "roof_image",
                            lambda lat, lng, z=20: (None, z, "esri"))
        out = pipeline.prescreen_damage("k", cands, 4,
                                        prescreener=lambda b64s: [5] * len(b64s))
        assert len(out) == 4

    def test_progress_arity_matches_server(self, monkeypatch):
        # The server's progress callback takes (phase, done, total, msg);
        # every progress call in the new pipeline code must match it.
        calls = []

        def progress(phase, done, total, msg):
            calls.append((phase, done, total, msg))

        pool = [{"lat": 39.0 + i * 0.001, "lng": -80.0, "address": "",
                 "building": "house"} for i in range(10)]
        monkeypatch.setattr(pipeline, "_overpass_buildings",
                            lambda *a, **k: pool)
        monkeypatch.setattr(pipeline, "_area_context", lambda *a: {})
        monkeypatch.setattr(pipeline, "_grid_points", lambda *a, **k: [])
        monkeypatch.setattr(pipeline, "roof_image",
                            lambda lat, lng, z=20: (b"img", z, "esri"))
        center = (39.0, -80.0, "X", "City")
        cands = pipeline.candidate_roofs("12345", center, 5,
                                         progress=progress)
        pipeline.prescreen_damage("k", cands, 3, progress=progress,
                                  prescreener=lambda b: [3] * len(b))
        assert any(p == "candidates" for p, _, _, _ in calls)
        assert any(p == "prescreen" for p, _, _, _ in calls)

    def test_empty_candidates(self):
        assert pipeline.prescreen_damage("k", [], 5) == []

    def test_small_pool_still_triaged(self, monkeypatch):
        # No passthrough: even a small pool gets the micro scan, and only
        # visibly damaged roofs earn the deep scan.
        cands = self._cands(3)
        monkeypatch.setattr(pipeline, "roof_image",
                            lambda lat, lng, z=20: (b"img", z, "esri"))
        out = pipeline.prescreen_damage("k", cands, 5,
                                        prescreener=lambda b: [1] * len(b))
        assert out == []  # all pristine -> nothing deep-scanned
        out = pipeline.prescreen_damage("k", cands, 5,
                                        prescreener=lambda b: [4] * len(b))
        assert len(out) == 3  # all damaged -> all deep-scanned

    def test_miscounted_batch_rescored_one_by_one(self, monkeypatch):
        monkeypatch.setattr(pipeline, "roof_image",
                            lambda *a, **k: (b"img", 19, "esri"))
        cands = [{"address": f"{i} St", "lat": 39.0 + i * 0.001,
                  "lng": -80.0} for i in range(4)]
        truth = {0: 1, 1: 4, 2: 2, 3: 5}
        calls = []

        def pre(b64s):
            calls.append(len(b64s))
            if len(b64s) > 1:
                return [4, 2, 5]  # one score missing
            return [truth[len(calls) - 3]]

        out = pipeline.prescreen_damage("k", cands, 4, prescreener=pre)
        assert calls == [4, 4, 1, 1, 1, 1]
        assert [c["micro_score"] for c in cands] == [1, 4, 2, 5]
        assert [c["address"] for c in out] == ["3 St", "1 St"]

    def test_vacant_buildings_keep_flag_through_triage(self, monkeypatch):
        # Vacant-flagged buildings are NOT dropped: they get triaged like
        # anything else and carry the flag into the deep scan for review.
        cands = self._cands(4)
        cands[1]["vacant"] = True
        cands[3]["vacant"] = True
        monkeypatch.setattr(pipeline, "roof_image",
                            lambda lat, lng, z=20: (b"img", z, "esri"))
        out = pipeline.prescreen_damage(
            "k", cands, 4, prescreener=lambda b64s: [5] * len(b64s))
        assert [c["address"] for c in out] == ["0 Main St", "1 Main St",
                                              "2 Main St", "3 Main St"]
        assert [c["vacant"] for c in out] == [False, True, False, True]

    def test_vacant_flag_from_osm_tags(self):
        assert pipeline._vacant_flag({"abandoned": "yes"})
        assert pipeline._vacant_flag({"building": "vacant"})
        assert pipeline._vacant_flag({"disused": "yes"})
        assert not pipeline._vacant_flag({"building": "house"})
        assert not pipeline._vacant_flag({"abandoned": "no"})
        assert not pipeline._vacant_flag({})


# ---------------- pin-drop picker ----------------

class TestPinPicker:
    def test_geocode_place_parses_nominatim(self, monkeypatch):
        monkeypatch.setattr(
            pipeline, "_nominatim",
            lambda path, params: [{"lat": "39.6295", "lon": "-79.9559",
                                   "display_name": "Morgantown, WV, USA"}])
        out = pipeline.geocode_place("Morgantown WV")
        assert out[0] == pytest.approx(39.6295)
        assert out[1] == pytest.approx(-79.9559)
        assert "Morgantown" in out[2]

    def test_geocode_place_none(self, monkeypatch):
        monkeypatch.setattr(pipeline, "_nominatim",
                            lambda path, params: [])
        assert pipeline.geocode_place("zzzzz") is None

    def test_failed_geocode_includes_place_and_consumes_nothing(
            self, app_client, monkeypatch):
        monkeypatch.setattr(pipeline, "geocode_address", lambda q: None)
        monkeypatch.setattr(pipeline, "geocode_place",
                            lambda q: (39.6295, -79.9559, "Morgantown, WV"))
        signup(app_client, "t@x.com")
        r = app_client.post("/api/scan", json={"q": "Dad's House WV"})
        assert r.status_code == 400
        body = r.get_json()
        assert body["place"]["lat"] == pytest.approx(39.6295)
        assert quota_of(app_client)["scans_left"] == 1  # nothing consumed

    def test_failed_geocode_without_place(self, app_client, monkeypatch):
        monkeypatch.setattr(pipeline, "geocode_address", lambda q: None)
        monkeypatch.setattr(pipeline, "geocode_place", lambda q: None)
        signup(app_client, "t@x.com")
        r = app_client.post("/api/scan", json={"q": "Nope Nowhere XX"})
        assert r.status_code == 400
        assert "place" not in r.get_json()

    def test_place_endpoint(self, app_client, monkeypatch):
        monkeypatch.setattr(pipeline, "geocode_place",
                            lambda q: (38.99, -78.76, "Cumberland, MD"))
        signup(app_client, "t@x.com")
        r = app_client.get("/api/place?q=Cumberland")
        assert r.status_code == 200
        assert r.get_json()["place"]["label"] == "Cumberland, MD"

    def test_place_endpoint_not_found(self, app_client, monkeypatch):
        monkeypatch.setattr(pipeline, "geocode_place", lambda q: None)
        signup(app_client, "t@x.com")
        assert app_client.get("/api/place?q=zzz").status_code == 404

    def test_place_endpoint_anon_401(self, app_client):
        assert app_client.get("/api/place?q=Cumberland").status_code == 401


# ---------------- damaged-only deep scan ----------------

class TestDamagedOnly:
    def _cands(self, n):
        return [{"lat": 39.0 + i * 0.002, "lng": -80.0,
                 "address": f"{i} Main St", "postcode": "12345",
                 "key": f"{i} Main St|12345"} for i in range(n)]

    def test_pristine_and_unverifiable_never_deep_scanned(self, monkeypatch):
        cands = self._cands(8)
        monkeypatch.setattr(pipeline, "roof_image",
                            lambda lat, lng, z=20: (b"img", z, "esri"))
        out = pipeline.prescreen_damage(
            "k", cands, 8,
            prescreener=lambda b64s: [1, 2, 0, 1, 2, 0, 1, 2][:len(b64s)])
        assert out == []  # nothing visibly damaged -> no deep scan

    def test_damaged_capped_worst_first(self, monkeypatch):
        cands = self._cands(8)
        monkeypatch.setattr(pipeline, "roof_image",
                            lambda lat, lng, z=20: (b"img", z, "esri"))
        out = pipeline.prescreen_damage(
            "k", cands, 3,
            prescreener=lambda b64s: [5, 3, 4, 2, 5, 1, 0, 3][:len(b64s)])
        # qualifying: idx0(5), idx4(5), idx2(4), idx1(3), idx7(3) -> top 3
        assert [c["address"] for c in out] == ["0 Main St", "4 Main St",
                                              "2 Main St"]

    def _run(self, monkeypatch, grades, vacant=()):
        houses = self._cands(len(grades))
        for i in vacant:
            houses[i]["vacant"] = True
        monkeypatch.setattr(pipeline, "roof_image",
                            lambda lat, lng, z=20: (b"img", z, "esri"))
        monkeypatch.setattr(pipeline, "attach_addresses",
                            lambda houses, progress=None: houses)
        it = iter(grades)

        def grader(imgs):
            out = []
            for _ in imgs:
                g = next(it)
                grade, abandoned = g if isinstance(g, tuple) else (g, False)
                out.append({"grade": grade, "abandoned": abandoned,
                            "confidence": "high", "evidence": [],
                            "material": "", "pitch": "", "obstruction": "",
                            "damage_boxes": []})
            return out

        server._jobs["j1"] = {"status": "running"}
        server._run_houses("j1", houses, "Testville, WV", grader=grader)
        return server._jobs["j1"]

    def test_leads_are_damaged_only_worst_first(self, monkeypatch):
        job = self._run(monkeypatch, [4, 1, 0, 3, 5, 2])
        assert job["status"] == "done"
        assert [h["grade"] for h in job["leads"]] == [1, 2, 3]
        assert "damaged" in job["msg"]

    def test_no_damaged_roofs_clean_done(self, monkeypatch):
        job = self._run(monkeypatch, [4, 5, 0, 4])
        assert job["status"] == "done"
        assert job["leads"] == []
        assert "no visibly damaged" in job["msg"].lower()

    def test_abandoned_roofs_flagged_not_dropped(self, monkeypatch):
        # Abandoned signals become a review flag, never a silent drop: the
        # customer sees the warning and decides whether to spend an unlock.
        # Either signal flags — OSM vacant tags OR the grader's abandoned
        # verdict — so all three are flagged here, worst first.
        job = self._run(monkeypatch, [(1, True), (2, False), (3, True)],
                        vacant=(1,))
        assert job["status"] == "done"
        assert [h["grade"] for h in job["leads"]] == [1, 2, 3]
        flagged = [h for h in job["leads"] if h.get("needs_review")]
        assert len(flagged) == 3
        assert "derelict" in job["leads"][0]["review_reason"]
        assert "vacant/abandoned" in job["leads"][1]["review_reason"]
        assert "derelict" in job["leads"][2]["review_reason"]
        assert "3 flagged for review" in job["msg"]

    def test_clean_lead_sorts_before_flagged(self, monkeypatch):
        # A clean damaged roof outranks flagged ones even when its grade is
        # milder: never present a possibly-abandoned property as a top lead.
        job = self._run(monkeypatch, [(2, False), (1, True), (3, False)],
                        vacant=())
        assert [h["grade"] for h in job["leads"]] == [2, 3, 1]
        assert [bool(h.get("needs_review")) for h in job["leads"]] == [
            False, False, True]

    def test_flagged_sort_after_clean_worst_first(self):
        houses = [{"grade": 2, "address": "b", "needs_review": True},
                  {"grade": 1, "address": "a"},
                  {"grade": 1, "address": "c", "needs_review": True},
                  {"grade": 3, "address": "d"}]
        out = pipeline.sort_leads(houses)
        assert [(h["address"], bool(h.get("needs_review"))) for h in out] == [
            ("a", False), ("d", False), ("c", True), ("b", True)]


# ---------------- street-level imagery fallback ----------------

def _photo_jpeg(w=800, h=600):
    import io as _io
    from PIL import Image
    img = Image.new("RGB", (w, h))
    img.putdata([((x * 7) % 256, (y * 5) % 256, ((x + y) * 3) % 256)
                 for y in range(h) for x in range(w)])
    buf = _io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


class TestStreetLevelFallback:
    @pytest.fixture
    def no_aerial(self, monkeypatch):
        monkeypatch.setattr(pipeline, "_centered_esri", lambda *a, **k: None)
        monkeypatch.setattr(pipeline, "_usgs_image", lambda *a, **k: None)

    def test_aerial_wins_over_street(self, monkeypatch):
        monkeypatch.setattr(pipeline, "_centered_esri",
                            lambda lat, lng, z: b"esri")
        monkeypatch.setattr(pipeline, "_mapillary_image",
                            lambda *a: pytest.fail("street fetched"))
        assert pipeline.roof_image(1, 2) == (b"esri", 20, "esri")

    def test_order_mapillary_then_streetview(self, monkeypatch, no_aerial):
        monkeypatch.setattr(pipeline, "_mapillary_image", lambda *a: b"mly")
        monkeypatch.setattr(pipeline, "_streetview_image",
                            lambda *a: pytest.fail("google fetched"))
        assert pipeline.roof_image(1, 2) == (b"mly", 0, "mapillary")
        monkeypatch.setattr(pipeline, "_mapillary_image", lambda *a: None)
        monkeypatch.setattr(pipeline, "_streetview_image", lambda *a: b"sv")
        assert pipeline.roof_image(1, 2) == (b"sv", 0, "streetview")

    def test_nothing_anywhere(self, monkeypatch, no_aerial):
        monkeypatch.setattr(pipeline, "_mapillary_image", lambda *a: None)
        monkeypatch.setattr(pipeline, "_streetview_image", lambda *a: None)
        assert pipeline.roof_image(1, 2, z=19) == (None, 19, "none")

    def test_no_keys_no_requests(self, monkeypatch):
        monkeypatch.delenv("MAPILLARY_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
        monkeypatch.setattr(pipeline, "_http_get",
                            lambda *a, **k: pytest.fail("network used"))
        assert pipeline._mapillary_image(1, 2) is None
        assert pipeline._streetview_image(1, 2) is None

    def test_streetview_skips_billed_fetch_without_pano(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k")
        calls = []
        def fake(url, timeout=20):
            calls.append(url)
            return json.dumps({"status": "ZERO_RESULTS"}).encode()
        monkeypatch.setattr(pipeline, "_http_get", fake)
        assert pipeline._streetview_image(1, 2) is None
        assert len(calls) == 1 and "/metadata?" in calls[0]

    def test_streetview_returns_square_photo(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k")
        photo = _photo_jpeg(640, 640)
        def fake(url, timeout=20):
            if "/metadata?" in url:
                return json.dumps({"status": "OK"}).encode()
            assert "return_error_code=true" in url
            return photo
        monkeypatch.setattr(pipeline, "_http_get", fake)
        out = pipeline._streetview_image(1, 2)
        from PIL import Image
        import io as _io
        assert Image.open(_io.BytesIO(out)).size == (512, 512)

    def test_square_jpeg_rejects_flat_and_non_jpeg(self):
        import io as _io
        from PIL import Image
        buf = _io.BytesIO()
        Image.new("RGB", (640, 640), (200, 200, 200)).save(buf, "JPEG")
        assert pipeline._square_jpeg(buf.getvalue()) is None
        assert pipeline._square_jpeg(b"\x89PNG....") is None
        assert pipeline._square_jpeg(None) is None

    def test_mapillary_picks_camera_facing_house(self):
        lat, lng = 40.0, -80.0
        south = lat - 0.0002  # ~22 m south of the house
        imgs = [
            {"id": "pano", "is_pano": True, "thumb_1024_url": "p",
             "computed_geometry": {"coordinates": [lng, south]},
             "computed_compass_angle": 0},
            {"id": "away", "thumb_1024_url": "a",
             "computed_geometry": {"coordinates": [lng, south]},
             "computed_compass_angle": 180},
            {"id": "far", "thumb_1024_url": "f",
             "computed_geometry": {"coordinates": [lng, lat - 0.002]},
             "computed_compass_angle": 0},
            {"id": "good", "thumb_1024_url": "g",
             "computed_geometry": {"coordinates": [lng, south]},
             "computed_compass_angle": 10},
        ]
        assert pipeline._best_mapillary(lat, lng, imgs)["id"] == "good"
        assert pipeline._best_mapillary(lat, lng, imgs[:3]) is None

    def test_mapillary_fetches_best_thumb(self, monkeypatch):
        monkeypatch.setenv("MAPILLARY_ACCESS_TOKEN", "t")
        photo = _photo_jpeg()
        lat, lng = 40.0, -80.0
        listing = {"data": [{"id": "1", "thumb_1024_url": "https://img/1",
                             "computed_geometry": {"coordinates": [lng, lat - 0.0002]},
                             "computed_compass_angle": 0}]}
        def fake(url, timeout=20):
            if url.startswith(pipeline._MAPILLARY_IMAGES):
                assert "bbox=" in url
                return json.dumps(listing).encode()
            assert url == "https://img/1"
            return photo
        monkeypatch.setattr(pipeline, "_http_get", fake)
        assert pipeline._mapillary_image(lat, lng)

    def test_grader_told_which_images_are_street_level(self, monkeypatch):
        seen = {}
        def fake_call(key, imgs, sources=None):
            seen["sources"] = sources
            return None
        monkeypatch.setattr(pipeline, "_gemini_call", fake_call)
        houses = [{"key": "a", "image_b64": "x", "imagery": "esri"},
                  {"key": "b", "image_b64": "y", "imagery": "streetview"}]
        pipeline.grade_roofs("key", houses)
        assert seen["sources"] == ["esri", "streetview"]

    def test_pinpoint_keeps_street_photo(self, monkeypatch):
        import base64
        monkeypatch.setattr(pipeline, "_centered_esri",
                            lambda *a: pytest.fail("esri closeup fetched"))
        sent = {}
        def fake_json(key, parts, schema, max_tokens=4000):
            sent["prompt"] = parts[0]["text"]
            sent["img"] = parts[1]["inline_data"]["data"]
            return None
        monkeypatch.setattr(pipeline, "_gemini_json", fake_json)
        b64 = base64.b64encode(_photo_jpeg(64, 64)).decode()
        h = {"grade": 2, "lat": 1, "lng": 2, "zoom": 0, "imagery": "mapillary",
             "image_b64": b64, "evidence": ["curling shingles"]}
        pipeline.localize_damage("key", [h])
        assert "street-level photo" in sent["prompt"]
        assert sent["img"] == b64

    def test_streetview_photos_not_cached(self):
        payload = {"zip": "1", "leads": [
            {"imagery": "streetview", "img": "data:x", "damage_img": "data:y",
             "streetview_url": "u"},
            {"imagery": "mapillary", "img": "data:m"}]}
        out = server._cacheable(payload)
        assert out["leads"][0]["img"] == "" and out["leads"][0]["damage_img"] == ""
        assert out["leads"][0]["streetview_url"] == "u"
        assert out["leads"][1]["img"] == "data:m"
        assert payload["leads"][0]["img"] == "data:x"


# ---------------- abandoned lead review (approve / reject) ----------------

class TestLeadReview:
    def _key(self, **kw):
        return server._lead_key(mklead(**kw))

    def _flagged_key(self, address):
        """Seed a flagged lead into the scan cache; return its lead_key."""
        lead = mklead(address=address)
        lead["needs_review"] = True
        lead["review_reason"] = ("Map records mark this building"
                                 " vacant/abandoned")
        seed_cache(leads=[lead])
        return server._lead_key(lead)

    def _review_row(self, client, email, key):
        conn = server._db()
        try:
            return conn.execute(
                "SELECT decision FROM lead_reviews WHERE user_id="
                "(SELECT id FROM users WHERE email=?) AND lead_key=?",
                (email, key)).fetchone()
        finally:
            conn.close()

    def test_approve_consumes_unlock_and_records(self, app_client):
        signup(app_client, "r1@x.com")
        key = self._flagged_key("11 Review Ln")
        r = app_client.post("/api/leads/review",
                            json={"lead_key": key, "action": "approve"})
        assert r.status_code == 200
        assert r.get_json()["lead"]["address"] == "11 Review Ln"
        assert r.get_json()["lead"]["locked"] is False
        assert quota_of(app_client)["unlocks_used"] == 1
        assert quota_of(app_client)["unlocks_left"] == 4  # 5 - 1
        assert self._review_row(app_client, "r1@x.com", key)[0] == "approve"

    def test_approve_unknown_lead_404_spends_nothing(self, app_client):
        signup(app_client, "r1b@x.com")
        key = self._key(address="No Such St")  # never seeded anywhere
        r = app_client.post("/api/leads/review",
                            json={"lead_key": key, "action": "approve"})
        assert r.status_code == 404
        assert quota_of(app_client)["unlocks_used"] == 0
        assert self._review_row(app_client, "r1b@x.com", key) is None

    def test_approve_unflagged_lead_400(self, app_client):
        signup(app_client, "r1c@x.com")
        lead = mklead(address="12 Clean St")  # real lead, not flagged
        seed_cache(leads=[lead])
        key = server._lead_key(lead)
        r = app_client.post("/api/leads/review",
                            json={"lead_key": key, "action": "approve"})
        assert r.status_code == 400
        assert quota_of(app_client)["unlocks_used"] == 0

    def test_approve_exhausted_402_records_nothing(self, app_client):
        signup(app_client, "r2@x.com")
        conn = server._db()
        try:
            conn.execute("UPDATE users SET trial_unlocks_used=5 WHERE email=?",
                         ("r2@x.com",))
            conn.commit()
        finally:
            conn.close()
        key = self._flagged_key("9 Elm St")
        r = app_client.post("/api/leads/review",
                            json={"lead_key": key, "action": "approve"})
        assert r.status_code == 402
        assert r.get_json()["error"] == "trial_unlocks_exhausted"
        # decision rolled back: they can retry after upgrading
        assert self._review_row(app_client, "r2@x.com", key) is None

    def test_reject_grants_one_replacement(self, app_client):
        signup(app_client, "r3@x.com")
        key = self._flagged_key("7 Oak St")
        r = app_client.post("/api/leads/review",
                            json={"lead_key": key, "action": "reject"})
        assert r.status_code == 200
        assert r.get_json()["replacement"] is True
        q = quota_of(app_client)
        assert q["unlocks_left"] == 6  # 5 + 1 replacement
        assert q["unlocks_used"] == 0  # rejection costs nothing
        assert self._review_row(app_client, "r3@x.com", key)[0] == "reject"

    def test_reject_unknown_lead_404_grants_nothing(self, app_client):
        signup(app_client, "r3b@x.com")
        key = self._key(address="Fabricated Ave")  # never seeded
        r = app_client.post("/api/leads/review",
                            json={"lead_key": key, "action": "reject"})
        assert r.status_code == 404
        assert quota_of(app_client)["unlocks_left"] == 5  # no bonus farmed
        assert self._review_row(app_client, "r3b@x.com", key) is None

    def test_reject_unflagged_lead_400_grants_nothing(self, app_client):
        signup(app_client, "r3c@x.com")
        lead = mklead(address="13 Clean St")  # real lead, not flagged
        seed_cache(leads=[lead])
        key = server._lead_key(lead)
        r = app_client.post("/api/leads/review",
                            json={"lead_key": key, "action": "reject"})
        assert r.status_code == 400
        assert quota_of(app_client)["unlocks_left"] == 5  # no bonus farmed
        assert self._review_row(app_client, "r3c@x.com", key) is None

    def test_reject_twice_grants_bonus_once(self, app_client):
        signup(app_client, "r4@x.com")
        key = self._flagged_key("8 Oak St")
        app_client.post("/api/leads/review",
                        json={"lead_key": key, "action": "reject"})
        r = app_client.post("/api/leads/review",
                            json={"lead_key": key, "action": "reject"})
        assert r.status_code == 200
        assert r.get_json()["note"] == "already decided"
        assert quota_of(app_client)["unlocks_left"] == 6  # still just +1

    def test_reject_unlocked_lead_400(self, app_client):
        signup(app_client, "r5@x.com")
        key = self._key()
        app_client.post("/api/leads/unlock", json={"lead_key": key})
        r = app_client.post("/api/leads/review",
                            json={"lead_key": key, "action": "reject"})
        assert r.status_code == 400

    def test_rejected_lead_hidden_from_results(self, app_client, db):
        signup(app_client, "r6@x.com")
        lead = mklead(address="5 Pine St")
        lead["needs_review"] = True
        seed_cache(leads=[lead, mklead(address="6 Pine St")])
        key = server._lead_key(lead)
        app_client.post("/api/leads/review",
                        json={"lead_key": key, "action": "reject"})
        conn = server._db()
        try:
            user = {"id": conn.execute(
                "SELECT id FROM users WHERE email=?",
                ("r6@x.com",)).fetchone()[0], "is_admin": False}
            shaped = server._shape_leads(conn, [lead, mklead(address="6 Pine St")],
                                         user)
        finally:
            conn.close()
        # rejected lead gone; survivor still locked (address masked)
        assert [l["lead_key"] for l in shaped] == [
            server._lead_key(mklead(address="6 Pine St"))]
        assert shaped[0]["locked"] is True

    def test_bad_action_400(self, app_client):
        signup(app_client, "r7@x.com")
        r = app_client.post("/api/leads/review",
                            json={"lead_key": "abc", "action": "maybe"})
        assert r.status_code == 400

    def test_admin_approve_returns_lead_and_commits(self, app_client):
        signup(app_client, "r8@x.com")
        conn = server._db()
        try:
            conn.execute("UPDATE users SET is_admin=1 WHERE email=?",
                         ("r8@x.com",))
            conn.commit()
        finally:
            conn.close()
        key = self._flagged_key("14 Admin Way")
        r = app_client.post("/api/leads/review",
                            json={"lead_key": key, "action": "approve"})
        assert r.status_code == 200
        body = r.get_json()
        assert body["lead"]["address"] == "14 Admin Way"
        assert body["lead"]["locked"] is False
        # decision persisted (was silently rolled back before the fix)
        assert self._review_row(app_client, "r8@x.com", key)[0] == "approve"
        # admin spends nothing
        assert quota_of(app_client)["unlocks_used"] == 0

    def test_admin_reject_unknown_lead_404(self, app_client):
        signup(app_client, "r9@x.com")
        conn = server._db()
        try:
            conn.execute("UPDATE users SET is_admin=1 WHERE email=?",
                         ("r9@x.com",))
            conn.commit()
        finally:
            conn.close()
        r = app_client.post("/api/leads/review",
                            json={"lead_key": self._key(address="Ghost Rd"),
                                  "action": "reject"})
        assert r.status_code == 404


# ---------------- for-sale lookup seam ----------------

class TestForSale:
    def test_unknown_without_api_key(self, db):
        conn = server._db()
        try:
            assert server._for_sale_status(conn, mklead()) is None
        finally:
            conn.close()

    def test_cached_result_used_without_vendor(self, db):
        conn = server._db()
        try:
            lead = mklead(address="11 Sale St")
            conn.execute("INSERT INTO listing_cache (addr_key, for_sale,"
                         " checked_at) VALUES (?,?,?)",
                         (server._addr_key(lead), 1, time.time()))
            conn.commit()
            assert server._for_sale_status(conn, lead, live=False) is True
        finally:
            conn.close()

    def _fake_vendor(self, monkeypatch, payload=None, fail=False):
        seen = {}
        body = json.dumps(payload if payload is not None else []).encode()

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body

        def fake_urlopen(req, timeout=12):
            seen["url"] = req.full_url
            # urllib normalizes header case ("X-Api-Key" -> "X-api-key")
            seen["key"] = req.headers.get("X-api-key")
            if fail:
                raise OSError("boom")
            return Resp()

        monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
        return seen

    def test_vendor_active_means_for_sale(self, monkeypatch):
        monkeypatch.setenv("LISTINGS_API_KEY", "k123")
        seen = self._fake_vendor(
            monkeypatch, [{"status": "Inactive"}, {"status": "Active"}])
        assert server._listing_vendor_lookup(
            "1 Main St", "Morgantown", "WV", "26505") is True
        assert "address=1%20Main%20St" in seen["url"]
        assert seen["key"] == "k123"

    def test_vendor_no_active_means_not_for_sale(self, monkeypatch):
        monkeypatch.setenv("LISTINGS_API_KEY", "k123")
        self._fake_vendor(monkeypatch, [{"status": "Inactive"}])
        assert server._listing_vendor_lookup(
            "1 Main St", "Morgantown", "WV", "26505") is False

    def test_vendor_error_means_unknown(self, monkeypatch):
        monkeypatch.setenv("LISTINGS_API_KEY", "k123")
        self._fake_vendor(monkeypatch, fail=True)
        assert server._listing_vendor_lookup(
            "1 Main St", "Morgantown", "WV", "26505") is None

    def test_unlock_attaches_for_sale_when_known(self, app_client, monkeypatch):
        signup(app_client, "fs@x.com")
        lead = mklead(address="12 Sale St")
        seed_cache(leads=[lead])
        key = server._lead_key(lead)
        monkeypatch.setattr(server, "_listing_vendor_lookup",
                            lambda a, c, s, p: True)
        r = app_client.post("/api/leads/unlock", json={"lead_key": key})
        assert r.status_code == 200
        assert r.get_json()["lead"]["for_sale"] is True


# ---------------- email verification (2026-10-01) ----------------

class TestEmailVerification:
    def _token_for(self, email):
        conn = server._db()
        try:
            row = conn.execute(
                "SELECT token FROM verification_tokens WHERE email=?"
                " ORDER BY created_at DESC LIMIT 1", (email,)).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def test_signup_sends_confirmation_without_session(self, app_client):
        r = signup(app_client, "nv1@t.com", verify=False)
        assert r.status_code == 200
        assert r.get_json()["verify_sent"] is True
        # No session until the email is confirmed.
        assert app_client.get("/api/auth/me").get_json().get("user") is None
        assert self._token_for("nv1@t.com")

    def test_unverified_signup_cannot_scan(self, app_client):
        signup(app_client, "nv2@t.com", verify=False)
        r = app_client.post("/api/scan", json={"zip": "26554"})
        assert r.status_code == 401  # no session at all until verified

    def test_unverified_signup_cannot_unlock_or_review(self, app_client):
        signup(app_client, "nv3@t.com", verify=False)
        r = app_client.post("/api/leads/unlock", json={"lead_key": "x"})
        assert r.status_code == 401
        r = app_client.post("/api/leads/review",
                            json={"lead_key": "x", "action": "approve"})
        assert r.status_code == 401

    def test_valid_token_verifies_and_logs_in(self, app_client):
        signup(app_client, "v1@t.com", verify=False)
        token = self._token_for("v1@t.com")
        r = app_client.get(f"/api/auth/verify?token={token}")
        assert r.status_code == 302
        assert "verified=1" in r.headers["Location"]
        me = app_client.get("/api/auth/me").get_json()
        assert me["user"]["email"] == "v1@t.com"
        # Token is single-use.
        r2 = app_client.get(f"/api/auth/verify?token={token}")
        assert "verify=invalid" in r2.headers["Location"]

    def test_invalid_token_rejected(self, app_client):
        r = app_client.get("/api/auth/verify?token=nope")
        assert r.status_code == 302
        assert "verify=invalid" in r.headers["Location"]

    def test_missing_token_rejected(self, app_client):
        r = app_client.get("/api/auth/verify")
        assert r.status_code == 302
        assert "verify=missing" in r.headers["Location"]

    def test_expired_token_rejected(self, app_client):
        signup(app_client, "v2@t.com", verify=False)
        token = self._token_for("v2@t.com")
        conn = server._db()
        try:
            conn.execute(
                "UPDATE verification_tokens SET expires_at=? WHERE token=?",
                (time.time() - 1, token))
            conn.commit()
        finally:
            conn.close()
        r = app_client.get(f"/api/auth/verify?token={token}")
        assert "verify=expired" in r.headers["Location"]
        assert app_client.get("/api/auth/me").get_json().get("user") is None

    def test_unverified_login_returns_verify_email(self, app_client):
        signup(app_client, "v3@t.com", verify=False)
        r = app_client.post("/api/auth/login",
                            json={"email": "v3@t.com",
                                  "password": "TestPass99!"})
        assert r.status_code == 403
        assert r.get_json()["error"] == "verify_email"
        # Still no session.
        assert app_client.get("/api/auth/me").get_json().get("user") is None

    def test_verified_login_works(self, app_client):
        signup(app_client, "v4@t.com")  # helper auto-verifies
        c2 = server.app.test_client()  # fresh client, no cookies
        r = c2.post("/api/auth/login",
                    json={"email": "v4@t.com", "password": "TestPass99!"})
        assert r.status_code == 200
        assert r.get_json()["user"]["email"] == "v4@t.com"

    def test_verified_user_can_scan(self, app_client, monkeypatch):
        signup(app_client, "v9@t.com")
        monkeypatch.setattr(server, "_run_scan", lambda *a, **k: "job1")
        r = app_client.post("/api/scan", json={"zip": "26554"})
        assert r.status_code == 200

    def test_resend_hides_unknown_addresses(self, app_client):
        r = app_client.post("/api/auth/resend-verification",
                            json={"email": "nobody@t.com"})
        assert r.status_code == 200
        assert r.get_json()["verify_sent"] is True

    def test_resend_rejects_already_verified(self, app_client):
        signup(app_client, "v5@t.com")
        r = app_client.post("/api/auth/resend-verification",
                            json={"email": "v5@t.com"})
        assert r.status_code == 400

    def test_resend_issues_new_token_for_unverified(self, app_client):
        signup(app_client, "v6@t.com", verify=False)
        t1 = self._token_for("v6@t.com")
        conn = server._db()
        try:
            conn.execute(
                "UPDATE verification_tokens SET created_at=? WHERE email=?",
                (time.time() - 120, "v6@t.com"))
            conn.commit()
        finally:
            conn.close()
        r = app_client.post("/api/auth/resend-verification",
                            json={"email": "v6@t.com"})
        assert r.status_code == 200
        t2 = self._token_for("v6@t.com")
        assert t2 and t2 != t1

    def test_failed_email_send_grants_nothing(self, app_client, monkeypatch):
        monkeypatch.setattr(server, "_send_email", lambda *a: False)
        r = app_client.post("/api/auth/signup", json={
            "email": "v7@t.com", "password": "TestPass99!",
            "account_type": "individual", "company_name": ""})
        assert r.status_code == 502
        assert app_client.get("/api/auth/me").get_json().get("user") is None
        # Login retries delivery instead of handing out a session.
        r = app_client.post("/api/auth/login",
                            json={"email": "v7@t.com",
                                  "password": "TestPass99!"})
        assert r.status_code in (403, 502)

    def test_resend_recovers_failed_signup_email(self, app_client,
                                                 monkeypatch):
        monkeypatch.setattr(server, "_send_email", lambda *a: False)
        r = app_client.post("/api/auth/signup", json={
            "email": "v8@t.com", "password": "TestPass99!",
            "account_type": "individual", "company_name": ""})
        assert r.status_code == 502
        monkeypatch.setattr(server, "_send_email", lambda *a: True)
        r = app_client.post("/api/auth/resend-verification",
                            json={"email": "v8@t.com"})
        assert r.status_code == 200
        token = self._token_for("v8@t.com")
        app_client.get(f"/api/auth/verify?token={token}")
        me = app_client.get("/api/auth/me").get_json()
        assert me["user"]["email"] == "v8@t.com"

    def test_grandfathered_account_logs_in_freely(self, app_client):
        # A pre-verification account: the migration marked email_verified=1.
        import secrets as _secrets
        import uuid as _uuid
        salt = _secrets.token_hex(16)
        conn = server._db()
        try:
            conn.execute(
                "INSERT INTO users (id, email, pw_hash, salt, account_type,"
                " company_name, is_admin, created_at, email_verified)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (_uuid.uuid4().hex, "old@t.com",
                 server._hash_pw("TestPass99!", salt), salt,
                 "individual", "", 0, time.time() - 86400 * 30, 1))
            conn.commit()
        finally:
            conn.close()
        r = app_client.post("/api/auth/login",
                            json={"email": "old@t.com",
                                  "password": "TestPass99!"})
        assert r.status_code == 200
        assert r.get_json()["user"]["email"] == "old@t.com"

class TestAddressResolution:
    """Tiered free-geocoder address matching: Census, Nominatim, Photon."""

    @pytest.fixture
    def no_sleep(self, monkeypatch):
        monkeypatch.setattr(pipeline.time, "sleep", lambda *a: None)

    def _house(self, **kw):
        h = {"lat": 39.5, "lng": -80.0, "grade": 2, "address": "",
             "city": "", "state": "", "postcode": "", "county": ""}
        h.update(kw)
        return h

    def test_census_locality_enriches_nominatim_guess(self, monkeypatch,
                                                     no_sleep):
        monkeypatch.setattr(
            pipeline, "_census_locality",
            lambda lat, lng: {"city": "Fairmont", "state": "WV",
                              "county": "Marion"})
        monkeypatch.setattr(
            pipeline, "_nominatim",
            lambda path, params: {"address": {"road": "Rural Rd"}})
        houses = [self._house()]
        pipeline.attach_addresses(houses)
        h = houses[0]
        assert h["address"] == "Rural Rd"
        assert h["address_confidence"] == "estimated"
        assert h["city"] == "Fairmont"
        assert h["county"] == "Marion"

    def test_census_locality_stamped_when_no_street(self, monkeypatch,
                                                    no_sleep):
        monkeypatch.setattr(
            pipeline, "_census_locality",
            lambda lat, lng: {"city": "Fairmont", "state": "WV",
                              "county": "Marion"})
        monkeypatch.setattr(pipeline, "_nominatim",
                            lambda path, params: {})
        monkeypatch.setattr(pipeline, "_photon_reverse",
                            lambda lat, lng: None)
        houses = [self._house()]
        pipeline.attach_addresses(houses)
        h = houses[0]
        assert h["address"] == ""
        assert h["city"] == "Fairmont"
        assert h["county"] == "Marion"

    def test_nominatim_verified_when_house_and_road(self, monkeypatch,
                                                    no_sleep):
        monkeypatch.setattr(pipeline, "_census_locality",
                            lambda lat, lng: {})
        monkeypatch.setattr(
            pipeline, "_nominatim",
            lambda path, params: {"address": {"house_number": "456",
                                              "road": "Oak Ave",
                                              "city": "Morgantown",
                                              "state": "WV",
                                              "postcode": "26505",
                                              "county": "Monongalia County"}})
        houses = [self._house()]
        pipeline.attach_addresses(houses)
        h = houses[0]
        assert h["address"] == "456 Oak Ave"
        assert h["address_confidence"] == "verified"
        assert h["address_source"] == "nominatim"

    def test_nominatim_street_only_is_estimated_guess(self, monkeypatch,
                                                      no_sleep):
        monkeypatch.setattr(pipeline, "_census_locality",
                            lambda lat, lng: {})
        monkeypatch.setattr(
            pipeline, "_nominatim",
            lambda path, params: {"address": {"road": "Rural Rd",
                                              "city": "Fairmont",
                                              "state": "WV"}})
        houses = [self._house()]
        pipeline.attach_addresses(houses)
        h = houses[0]
        assert h["address"] == "Rural Rd"
        assert h["address_confidence"] == "estimated"

    def test_photon_fallback_when_others_miss(self, monkeypatch, no_sleep):
        monkeypatch.setattr(pipeline, "_census_locality",
                            lambda lat, lng: {})
        monkeypatch.setattr(pipeline, "_nominatim",
                            lambda path, params: {})
        monkeypatch.setattr(
            pipeline, "_photon_reverse",
            lambda lat, lng: {"address": "789 Pine St", "city": "Clarksburg",
                              "state": "WV", "postcode": "26301",
                              "county": "Harrison"})
        houses = [self._house()]
        pipeline.attach_addresses(houses)
        h = houses[0]
        assert h["address"] == "789 Pine St"
        assert h["address_source"] == "photon"
        assert h["address_confidence"] == "estimated"

    def test_all_miss_leaves_blank(self, monkeypatch, no_sleep):
        monkeypatch.setattr(pipeline, "_census_locality",
                            lambda lat, lng: {})
        monkeypatch.setattr(pipeline, "_nominatim",
                            lambda path, params: None)
        monkeypatch.setattr(pipeline, "_photon_reverse",
                            lambda lat, lng: None)
        houses = [self._house()]
        pipeline.attach_addresses(houses)
        assert houses[0]["address"] == ""
        assert "address_confidence" not in houses[0]

    def test_osm_tagged_address_labeled_verified(self, monkeypatch,
                                                 no_sleep):
        seen = []
        monkeypatch.setattr(pipeline, "_census_locality",
                            lambda lat, lng: seen.append(1) or {})
        houses = [self._house(address="10 Farm Ln")]
        pipeline.attach_addresses(houses)
        h = houses[0]
        assert h["address"] == "10 Farm Ln"
        assert h["address_confidence"] == "verified"
        assert h["address_source"] == "map_tags"
        assert seen == []  # never re-resolved

    def test_healthy_roofs_skipped(self, monkeypatch, no_sleep):
        calls = []
        monkeypatch.setattr(pipeline, "_census_locality",
                            lambda lat, lng: calls.append(1) or {})
        houses = [self._house(grade=5), self._house(grade=4)]
        pipeline.attach_addresses(houses)
        assert calls == []
        assert all(h["address"] == "" for h in houses)


# ---------------- Stripe webhook auto-fulfillment ----------------

class TestStripeWebhook:
    def _signed(self, event, secret, ts=None):
        import hmac as _hmac
        import hashlib as _hl
        payload = json.dumps(event).encode()
        t = str(int(time.time())) if ts is None else str(ts)
        sig = _hmac.new(secret.encode(), f"{t}.".encode() + payload,
                        _hl.sha256).hexdigest()
        return payload, f"t={t},v1={sig}"

    def _post(self, client, event, secret="whsec_test"):
        payload, sig = self._signed(event, secret)
        return client.post("/api/stripe/webhook", data=payload,
                           headers={"Stripe-Signature": sig,
                                    "Content-Type": "application/json"})

    def test_bad_signature_rejected(self, app_client, db, monkeypatch):
        monkeypatch.setattr(server, "STRIPE_WEBHOOK_SECRET", "whsec_test")
        r = app_client.post("/api/stripe/webhook", data=b"{}",
                            headers={"Stripe-Signature": "t=1,v1=deadbeef"})
        assert r.status_code == 400

    def test_tampered_payload_rejected(self, app_client, db, monkeypatch):
        monkeypatch.setattr(server, "STRIPE_WEBHOOK_SECRET", "whsec_test")
        event = {"id": "evt_x", "type": "checkout.session.completed",
                 "data": {"object": {}}}
        payload, sig = self._signed(event, "whsec_test")
        r = app_client.post("/api/stripe/webhook",
                            data=payload + b"tamper",
                            headers={"Stripe-Signature": sig})
        assert r.status_code == 400

    def test_checkout_applies_plan_by_amount(self, app_client, db, monkeypatch):
        monkeypatch.setattr(server, "STRIPE_WEBHOOK_SECRET", "whsec_test")
        signup(app_client, "buyer@x.com")
        event = {"id": "evt_ck1", "type": "checkout.session.completed",
                 "data": {"object": {"payment_status": "paid",
                    "customer_details": {"email": "buyer@x.com"},
                    "amount_total": 4999, "customer": "cus_1", "metadata": {}}}}
        r = self._post(app_client, event)
        assert r.status_code == 200
        assert quota_of(app_client)["plan"] == "starter"

    def test_metadata_plan_wins_over_amount(self, app_client, db, monkeypatch):
        monkeypatch.setattr(server, "STRIPE_WEBHOOK_SECRET", "whsec_test")
        signup(app_client, "buyer2@x.com")
        event = {"id": "evt_ck2", "type": "checkout.session.completed",
                 "data": {"object": {"payment_status": "paid",
                    "customer_details": {"email": "buyer2@x.com"},
                    "amount_total": 4999, "customer": "cus_2",
                    "metadata": {"plan": "pro"}}}}
        r = self._post(app_client, event)
        assert r.status_code == 200
        assert quota_of(app_client)["plan"] == "pro"

    def test_duplicate_event_idempotent(self, app_client, db, monkeypatch):
        monkeypatch.setattr(server, "STRIPE_WEBHOOK_SECRET", "whsec_test")
        signup(app_client, "buyer3@x.com")
        event = {"id": "evt_dup", "type": "checkout.session.completed",
                 "data": {"object": {"payment_status": "paid",
                    "customer_details": {"email": "buyer3@x.com"},
                    "amount_total": 9999, "metadata": {}}}}
        assert self._post(app_client, event).status_code == 200
        r = self._post(app_client, event)
        assert r.get_json() == {"ok": True, "duplicate": True}

    def test_unknown_email_goes_pending(self, app_client, db, monkeypatch):
        monkeypatch.setattr(server, "STRIPE_WEBHOOK_SECRET", "whsec_test")
        event = {"id": "evt_unk", "type": "checkout.session.completed",
                 "data": {"object": {"payment_status": "paid",
                    "customer_details": {"email": "ghost@x.com"},
                    "amount_total": 4999, "metadata": {}}}}
        r = self._post(app_client, event)
        assert r.status_code == 200
        conn = server._db()
        try:
            row = conn.execute("SELECT email, plan FROM stripe_pending"
                               ).fetchone()
        finally:
            conn.close()
        assert row == ("ghost@x.com", "starter")

    def test_renewal_resets_cycle(self, app_client, db, monkeypatch):
        monkeypatch.setattr(server, "STRIPE_WEBHOOK_SECRET", "whsec_test")
        signup(app_client, "ren@x.com")
        set_plan("ren@x.com", "starter", scans_used=3, unlocks_used=10)
        event = {"id": "evt_ren", "type": "invoice.paid",
                 "data": {"object": {"billing_reason": "subscription_cycle",
                    "customer_email": "ren@x.com", "amount_due": 4999}}}
        r = self._post(app_client, event)
        assert r.status_code == 200
        q = quota_of(app_client)
        assert q["plan"] == "starter"
        assert q["scans_used"] == 0 and q["unlocks_used"] == 0

    def test_initial_invoice_not_double_applied(self, app_client, db,
                                               monkeypatch):
        monkeypatch.setattr(server, "STRIPE_WEBHOOK_SECRET", "whsec_test")
        signup(app_client, "new@x.com")
        event = {"id": "evt_ic", "type": "invoice.paid",
                 "data": {"object": {"billing_reason": "subscription_create",
                    "customer_email": "new@x.com", "amount_due": 4999}}}
        r = self._post(app_client, event)
        assert r.status_code == 200
        assert quota_of(app_client)["plan"] == "trial"

    def test_cancel_downgrades_to_trial(self, app_client, db, monkeypatch):
        monkeypatch.setattr(server, "STRIPE_WEBHOOK_SECRET", "whsec_test")
        signup(app_client, "cx@x.com")
        ck = {"id": "evt_cx1", "type": "checkout.session.completed",
              "data": {"object": {"payment_status": "paid",
                 "customer_details": {"email": "cx@x.com"},
                 "amount_total": 9999, "customer": "cus_9", "metadata": {}}}}
        assert self._post(app_client, ck).status_code == 200
        assert quota_of(app_client)["plan"] == "pro"
        event = {"id": "evt_cx2", "type": "customer.subscription.deleted",
                 "data": {"object": {"customer": "cus_9"}}}
        r = self._post(app_client, event)
        assert r.status_code == 200
        assert quota_of(app_client)["plan"] == "trial"


# ---------------- automatic Street View per listing ----------------

class TestLeadStreetview:
    def test_no_key_unavailable(self, app_client, db, monkeypatch):
        monkeypatch.setattr(server, "GOOGLE_MAPS_API_KEY", "")
        signup(app_client, "sv@t.com")
        r = app_client.get("/api/leads/abcd1234abcd1234/streetview")
        assert r.status_code == 404

    def test_locked_lead_forbidden(self, app_client, db, monkeypatch):
        monkeypatch.setattr(server, "GOOGLE_MAPS_API_KEY", "k")
        signup(app_client, "sv2@t.com")
        payload = seed_cache(leads=[mklead()])
        key = server._lead_key(payload["leads"][0])
        r = app_client.get(f"/api/leads/{key}/streetview")
        assert r.status_code == 403

    def test_unlocked_lead_served_and_cached(self, app_client, db, monkeypatch):
        monkeypatch.setattr(server, "GOOGLE_MAPS_API_KEY", "k")
        calls = []

        def fake_fetch(lat, lng):
            calls.append((lat, lng))
            return b"J" * 5000

        monkeypatch.setattr(server, "_sv_fetch", fake_fetch)
        signup(app_client, "sv3@t.com")
        payload = seed_cache(leads=[mklead()])
        key = server._lead_key(payload["leads"][0])
        app_client.post("/api/leads/unlock", json={"lead_key": key})
        r = app_client.get(f"/api/leads/{key}/streetview")
        assert r.status_code == 200
        assert r.content_type == "image/jpeg"
        assert len(calls) == 1
        r = app_client.get(f"/api/leads/{key}/streetview")
        assert r.status_code == 200
        assert len(calls) == 1  # disk cache on the second hit

    def test_no_imagery_404(self, app_client, db, monkeypatch):
        monkeypatch.setattr(server, "GOOGLE_MAPS_API_KEY", "k")
        monkeypatch.setattr(server, "_sv_fetch", lambda lat, lng: None)
        signup(app_client, "sv4@t.com")
        payload = seed_cache(leads=[mklead()])
        key = server._lead_key(payload["leads"][0])
        app_client.post("/api/leads/unlock", json={"lead_key": key})
        r = app_client.get(f"/api/leads/{key}/streetview")
        assert r.status_code == 404

    def test_bad_key_format_404(self, app_client, db, monkeypatch):
        monkeypatch.setattr(server, "GOOGLE_MAPS_API_KEY", "k")
        signup(app_client, "sv5@t.com")
        r = app_client.get("/api/leads/../../etc/streetview")
        assert r.status_code == 404
