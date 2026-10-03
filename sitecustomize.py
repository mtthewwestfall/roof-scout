"""Roof Scout runtime tuning.

These patches keep the existing app architecture intact while improving
roof coverage, severe-damage/tarp recall, and lead ordering.
"""
from __future__ import annotations

try:
    import math
    import pipeline

    # Give each scan a large real-building pool. The core pipeline remains
    # unchanged, while this runtime hook raises the effective ceiling.
    _original_overpass = pipeline._overpass_buildings

    def _overpass_more_buildings(zipcode, lat0, lng0, limit=120):
        return _original_overpass(zipcode, lat0, lng0, limit=max(int(limit), 600))

    pipeline._overpass_buildings = _overpass_more_buildings
    pipeline.MICRO_SCAN_POOL = max(getattr(pipeline, "MICRO_SCAN_POOL", 200), 500)

    # Spread candidate roofs geographically so a dense pocket next to the ZIP
    # centroid does not consume the whole scan. Never promote blind grid points
    # to roof candidates: only OSM building footprints are valid scan targets.
    _original_candidates = pipeline.candidate_roofs

    def _spread_candidates(zipcode, center, count, progress=None, exclude_cells=None):
        houses, footprints_ok = _original_candidates(
            zipcode, center, count, progress, exclude_cells=exclude_cells)

        # A failed/empty footprint lookup must never turn into arbitrary road,
        # forest, yard, or field coordinates that get sent to the vision model.
        if not footprints_ok:
            return [], False

        # The legacy candidate function tops up with blind grid points when
        # footprints are thin. Those points have no building tag; discard them.
        houses = [h for h in houses if h.get("building")]
        if not houses:
            return [], True

        if len(houses) <= 1:
            return houses, footprints_ok

        lat0, lng0 = center[0], center[1]
        bins = {}
        for h in houses:
            dy = (h["lat"] - lat0) * 111.0
            dx = (h["lng"] - lng0) * 111.0 * max(
                0.2, math.cos(math.radians(lat0))
            )
            bx = max(-2, min(2, int(dx / 0.9)))
            by = max(-2, min(2, int(dy / 0.9)))
            bins.setdefault((bx, by), []).append(h)

        for bucket in bins.values():
            bucket.sort(key=lambda h: (
                (h["lat"] - lat0) ** 2 + (h["lng"] - lng0) ** 2
            ))

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
    # strong damage evidence. Color is irrelevant; location is what matters.
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

    # Preserve the existing requested scan sizes, but ensure the normal 20-roof
    # request produces the tuned 30-result target.
    _original_prescreen = pipeline.prescreen_damage

    def _prescreen_more_results(api_key, cands, count, progress=None, prescreener=None):
        requested = int(count)
        target = max(requested, 30) if requested >= 20 else requested
        return _original_prescreen(
            api_key, cands, target, progress, prescreener=prescreener
        )

    pipeline.prescreen_damage = _prescreen_more_results

    # Add explicit tarp metadata from the model's own evidence so the UI/API
    # can distinguish tarp-confirmed leads from ordinary low grades.
    _original_grade_roofs = pipeline.grade_roofs

    def _grade_with_tarp_flags(api_key, houses, progress=None, grader=None, evidencer=None):
        out = _original_grade_roofs(
            api_key, houses, progress, grader=grader, evidencer=evidencer
        )
        for h in out:
            text = " ".join(str(x) for x in (h.get("evidence") or []))
            text += " " + str(h.get("obstruction") or "")
            lower = text.lower()
            h["tarp_detected"] = "tarp" in lower or "tarpaulin" in lower
            if h["tarp_detected"]:
                h["tarp_confidence"] = (
                    "high" if h.get("grade") in (1, 2) else "medium"
                )
        return out

    pipeline.grade_roofs = _grade_with_tarp_flags

    # Keep the existing grade ordering, then prefer tarp-confirmed leads within
    # the same review/grade bucket.
    _grade_order = getattr(pipeline, "_GRADE_ORDER", {})

    def _sort_leads_with_tarp_priority(houses):
        return sorted(
            houses,
            key=lambda h: (
                bool(h.get("needs_review")),
                _grade_order.get(h.get("grade", 0), 5),
                not bool(h.get("tarp_detected")),
                h.get("address", ""),
            ),
        )

    pipeline.sort_leads = _sort_leads_with_tarp_priority

except Exception as _roofscout_tuning_error:
    # Never prevent the app from starting if runtime tuning cannot load.
    print(f"Roof Scout tuning skipped: {_roofscout_tuning_error}", flush=True)

# Normalize the legacy 20-roof API request to the tuned 30-result scan while
# still respecting explicit smaller selections.
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
