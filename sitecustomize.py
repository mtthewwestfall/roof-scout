"""Roof Scout runtime tuning.

These patches keep the existing app architecture intact while improving
roof coverage, severe-damage/tarp recall, and lead ordering.
"""
from __future__ import annotations

try:
    import pipeline

    # The footprint query was a major recall bottleneck. Give each scan a
    # much larger real-building pool, then spread that pool geographically
    # instead of taking only the roofs nearest the ZIP centroid.
    _original_overpass = pipeline._overpass_buildings

    def _overpass_more_buildings(zipcode, lat0, lng0, limit=120):
        return _original_overpass(zipcode, lat0, lng0, limit=max(int(limit), 600))

    pipeline._overpass_buildings = _overpass_more_buildings
    pipeline.MICRO_SCAN_POOL = max(getattr(pipeline, "MICRO_SCAN_POOL", 200), 500)

    _original_candidates = pipeline.candidate_roofs

    def _spread_candidates(zipcode, center, count, progress=None, exclude_cells=None):
        houses, footprints_ok = _original_candidates(
            zipcode, center, count, progress, exclude_cells=exclude_cells)
        if len(houses) <= 1:
            return houses, footprints_ok

        # Stratify the available real footprints into a 5x5 grid around the
        # ZIP center and round-robin the nearest roof from each cell. This
        # prevents a dense neighborhood beside the centroid from consuming
        # the whole scan while leaving the edges untouched.
        lat0, lng0 = center[0], center[1]
        bins = {}
        for h in houses:
            dy = (h["lat"] - lat0) * 111.0
            dx = (h["lng"] - lng0) * 111.0 * max(0.2, __import__("math").cos(__import__("math").radians(lat0)))
            bx = max(-2, min(2, int(dx / 0.9)))
            by = max(-2, min(2, int(dy / 0.9)))
            bins.setdefault((bx, by), []).append(h)

        for bucket in bins.values():
            bucket.sort(key=lambda h: (h["lat"] - lat0) ** 2 + (h["lng"] - lng0) ** 2)

        ordered = []
        keys = sorted(bins, key=lambda k: k[0] * k[0] + k[1] * k[1])
        while keys:
            next_keys = []
            for key in keys:
                bucket = bins[key]
                if bucket:
                    ordered.append(bucket.pop(0))
                if bucket:
                    next_keys.append(key)
            keys = next_keys

        return ordered, footprints_ok

    pipeline.candidate_roofs = _spread_candidates

    # Explicitly teach triage and deep grading that temporary roof tarps are
    # high-value damage evidence. Color is irrelevant; location is what
    # matters. Also call out other visible severe-failure signals so the model
    # does not over-rely on color or one particular failure pattern.
    tarp_rule = (
        "\n\nTARP / SEVERE-DAMAGE RULE: A temporary tarp visibly covering the target "
        "roof is direct evidence of active roof failure or storm repair. A "
        "substantial roof tarp supports grade 1; a smaller localized roof tarp "
        "supports grade 2 unless other visible evidence makes the roof failing. "
        "Do not require blue: black, gray, white, green, brown, and other "
        "temporary coverings count when clearly on the roof. Also look for large "
        "bare/exposed areas, missing material, collapsed or sagging sections, and "
        "obvious emergency patches. Do not confuse pool covers, yard tarps, "
        "vehicle covers, tents, or ground objects with a roof tarp."
    )
    if tarp_rule not in pipeline.GRADE_PROMPT:
        pipeline.GRADE_PROMPT += tarp_rule
    if tarp_rule not in pipeline.PRESCREEN_PROMPT:
        pipeline.PRESCREEN_PROMPT += tarp_rule

    # Keep the existing scan result size at 30, but make sure tarp-confirmed
    # and severe candidates win ties rather than getting buried by healthy
    # roofs from a dense part of the ZIP.
    _original_prescreen = pipeline.prescreen_damage

    def _prescreen_more_results(api_key, cands, count, progress=None, prescreener=None):
        target = max(int(count), 30) if int(count) >= 20 else int(count)
        return _original_prescreen(api_key, cands, target, progress, prescreener=prescreener)

    pipeline.prescreen_damage = _prescreen_more_results

    _original_grade_roofs = pipeline.grade_roofs

    def _grade_with_tarp_flags(api_key, houses, progress=None, grader=None, evidencer=None):
        out = _original_grade_roofs(api_key, houses, progress, grader=grader, evidencer=evidencer)
        for h in out:
            text = " ".join(str(x) for x in (h.get("evidence") or []))
            text += " " + str(h.get("obstruction") or "")
            text_lower = text.lower()
            h["tarp_detected"] = "tarp" in text_lower or "tarpaulin" in text_lower
            if h["tarp_detected"]:
                h["tarp_confidence"] = "high" if h.get("grade") in (1, 2) else "medium"
        return out

    pipeline.grade_roofs = _grade_with_tarp_flags

    _original_sort = pipeline.sort_leads

    def _sort_leads_with_tarp_priority(houses):
        return sorted(
            houses,
            key=lambda h: (
                bool(h.get("needs_review")),
                _original_sort.__globals__["_GRADE_ORDER"].get(h.get("grade", 0), 5),
                not bool(h.get("tarp_detected")),
                h.get("address", ""),
            ),
        )

    pipeline.sort_leads = _sort_leads_with_tarp_priority

except Exception as _roofscout_tuning_error:
    # Never prevent the app from starting if runtime tuning cannot load.
    print(f"Roof Scout tuning skipped: {_roofscout_tuning_error}", flush=True)

# The UI currently defaults to 20. Normalize that legacy value to the tuned
# 30-result scan while still respecting explicit smaller selections.
try:
    from flask import Request

    _original_get_json = Request.get_json

    def _get_json_tuned(self, *args, **kwargs):
        data = _original_get_json(self, *args, **kwargs)
        if self.path == "/api/scan" and isinstance(data, dict):
            data = dict(data)
            try:
                requested = int(data.get("count", 30))
            except Exception:
                requested = 30
            if requested == 20:
                data["count"] = 30
            else:
                data["count"] = max(5, min(30, requested))
        return data

    Request.get_json = _get_json_tuned
except Exception as _request_tuning_error:
    print(f"Roof Scout request tuning skipped: {_request_tuning_error}", flush=True)
