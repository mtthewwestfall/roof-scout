"""Roof Scout test suite: caps, admin, password gate, isolation, dedup.

Run: cd /tmp/rsfix && python -m pytest tests/ -q
"""
import json
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

    def test_vacant_buildings_dont_take_pool_slots(self, monkeypatch):
        near_vacant = [{"lat": 39.0 + i * 0.0001, "lng": -80.0, "address": "",
                        "building": "house", "vacant": True} for i in range(200)]
        farther = [{"lat": 39.1 + i * 0.001, "lng": -80.0, "address": "",
                    "building": "house"} for i in range(50)]
        monkeypatch.setattr(pipeline, "_overpass_buildings",
                            lambda *a, **k: near_vacant + farther)
        monkeypatch.setattr(pipeline, "_area_context", lambda *a: {})
        monkeypatch.setattr(pipeline, "_grid_points", lambda *a, **k: [])
        out = pipeline.candidate_roofs("12345", (39.0, -80.0, "X", "City"), 20)
        assert len(out) == 50 and not any(h["vacant"] for h in out)

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

    def test_vacant_buildings_dropped_before_triage(self, monkeypatch):
        cands = self._cands(4)
        cands[1]["vacant"] = True
        cands[3]["vacant"] = True
        monkeypatch.setattr(pipeline, "roof_image",
                            lambda lat, lng, z=20: (b"img", z, "esri"))
        seen = []

        def prescreener(b64s):
            seen.append(len(b64s))
            return [5] * len(b64s)

        out = pipeline.prescreen_damage("k", cands, 4, prescreener=prescreener)
        assert [c["address"] for c in out] == ["0 Main St", "2 Main St"]
        assert seen == [2]  # vacant pair never even triaged

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

    def _run(self, monkeypatch, grades):
        houses = self._cands(len(grades))
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

    def test_abandoned_roofs_never_become_leads(self, monkeypatch):
        job = self._run(monkeypatch, [(1, True), (2, False), (3, True)])
        assert job["status"] == "done"
        assert [h["grade"] for h in job["leads"]] == [2]


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
