"""Regression tests for RoofScout trustworthiness (spec section 20).

Tests A-I verify the report safety net (_validate_lead_report) and the
Grade 1 QC logic. Test J verifies database failures are logged, not swallowed.
These are unit tests — no Gemini API calls, no network.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import _validate_lead_report


def _base_lead(**kw):
    l = {
        "grade": 2,
        "evidence": ["a pale-gray patch on the front slope"],
        "damage_boxes": [],
        "imagery_source": "Esri World Imagery",
        "imagery_date": "Imagery capture date unavailable.",
    }
    l.update(kw)
    return l


def test_a_normal_roof_vents_not_grade1():
    """Normal roof + vents must NOT be Grade 1."""
    l = _validate_lead_report(_base_lead(
        grade=4, evidence=["four small vent pipes near the ridge"]))
    assert l["grade"] != 1, "vents must never produce Grade 1"


def test_b_normal_roof_hvac_not_grade1():
    """Normal roof + HVAC must NOT be Grade 1."""
    l = _validate_lead_report(_base_lead(
        grade=4, evidence=["rectangular HVAC unit on the flat rear section"]))
    assert l["grade"] != 1, "HVAC must never produce Grade 1"


def test_c_dark_area_shadow_not_grade1():
    """Dark roof area / shadow must NOT automatically become Grade 1."""
    l = _validate_lead_report(_base_lead(
        grade=2, evidence=["large dark area on the north slope, possibly shadow"]))
    assert l["grade"] != 1


def test_d_ordinary_patch_not_grade1():
    """Ordinary patch: Grade 2/3/0, never automatic Grade 1."""
    l = _validate_lead_report(_base_lead(
        grade=3, evidence=["possible patch/repair area near the eave"]))
    assert l["grade"] in (0, 2, 3), f"unexpected grade {l['grade']}"


def test_e_tarp_strong_signal_but_verified():
    """Substantial tarp is a strong signal, but Grade 1 needs verification."""
    # Unverified tarp Grade 1 must be downgraded by the safety net.
    l = _validate_lead_report(_base_lead(
        grade=1, grade1_verified=False,
        tarp_detected=True, tarp_confidence=0.85,
        evidence=["large blue tarp covering the rear slope"]))
    assert l["grade"] != 1, "unverified Grade 1 must not survive the safety net"
    # Verified tarp Grade 1 survives.
    l2 = _validate_lead_report(_base_lead(
        grade=1, grade1_verified=True,
        grade1_verification_reason="substantial tarp confirmed",
        tarp_detected=True, tarp_confidence=0.9,
        evidence=["large blue tarp covering the rear slope"]))
    assert l2["grade"] == 1 and l2["grade1_verified"] is True


def test_f_missing_covering_verified_grade1():
    """Large clearly-missing covering + verification = Grade 1 stands."""
    l = _validate_lead_report(_base_lead(
        grade=1, grade1_verified=True,
        grade1_verification_reason="large missing section with exposed decking",
        evidence=["large section of missing covering, decking visible"]))
    assert l["grade"] == 1


def test_g_ambiguous_imagery_grade0():
    """Ambiguous imagery must be Grade 0 or low-confidence non-Grade-1."""
    l = _validate_lead_report(_base_lead(grade=0, confidence="low"))
    assert l["grade"] != 1


def test_h_verified_grade1_evidence_status():
    """Verified Grade 1 keeps its verification metadata for the report."""
    l = _validate_lead_report(_base_lead(
        grade=1, grade1_verified=True,
        grade1_verification_reason="collapsed section confirmed",
        grade1_verification_confidence="high",
        evidence_type="collapsed section"))
    assert l["grade"] == 1
    assert l["grade1_verification_reason"] == "collapsed section confirmed"


def test_i_unverified_grade1_rejected():
    """Unverified Grade 1 candidate is rejected before reaching the user."""
    l = _validate_lead_report(_base_lead(
        grade=1,  # no grade1_verified flag
        evidence=["dark irregular area"],
        damage_boxes=[{"box_2d": [100, 100, 200, 200],
                       "label": "damaged flashing"}]))
    assert l["grade"] != 1, "safety net must reject unverified Grade 1"
    # Diagnostic label must be softened to evidence-based wording.
    labels = [b["label"] for b in l.get("damage_boxes", [])]
    assert not any("damaged flashing" in x for x in labels), \
        f"diagnostic label leaked through: {labels}"
    assert any("review recommended" in x for x in labels), \
        f"expected softened label, got: {labels}"


def test_j_db_failure_logged_not_swallowed():
    """Database failure during persistence must be logged and surfaced."""
    import io
    import contextlib
    import server as srv

    job_id = "test-job-db-fail"
    with srv._jobs_lock:
        srv._jobs[job_id] = {"status": "running", "owner": "u1", "leads": []}
    # Point DB_PATH at an invalid location to force a failure.
    orig = srv.DB_PATH
    srv.DB_PATH = "/nonexistent-dir-xyz/roofscout.db"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            srv._set_job(job_id, status="done", leads=[], area="Test")
    finally:
        srv.DB_PATH = orig
    out = buf.getvalue()
    assert "CRITICAL" in out and "persistence failed" in out, \
        f"expected logged persistence failure, got: {out!r}"
    with srv._jobs_lock:
        err = srv._jobs[job_id].get("persist_error")
        del srv._jobs[job_id]
    assert err, "persist_error must be recorded on the job"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
