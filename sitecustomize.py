"""Roof Scout runtime tuning.

Keeps the existing app architecture intact while improving roof coverage,
automatic widening, severe-damage/tarp recall, lead ordering, and cache
invalidation after scan-engine changes.
"""
from __future__ import annotations

try:
    import math
    import urllib.request
    import json
    import pipeline

    _original_overpass = pipeline._overpass_buildings

    def _parse_overpass_buildings(data):
        out = []
        for el in (data or {}).get("elements", []):
            c, t = el.get("center"), el.get("tags", {})
            if not c:
                continue
            num, street = t.get("addr:housenumber"), t.get("addr:street")
            out.append({
                "address": f"{num} {street}" if num and street else "",
                "lat": float(c["lat"]),
                "lng": float(c["lon"]),
                "building": t.get("building", ""),
                "vacant": pipeline._vacant_flag(t),
            })
        return out

    def _wide_overpass(zipcode, lat0, lng0, limit=120):
        """Return a large pool of real building footprints.

        The core lookup covers roughly 1.6 km. If that is thin, immediately
        add progressively wider Overpass boxes. Later scan rounds exclude
        previously checked cells and therefore naturally move outward.
        """
        target = max(int(limit), 600)
        try:
            base = _original_overpass(zipcode, lat0, lng0, limit=target)
        except Exception:
            base = []

        seen = {
            pipeline._cell_of(b["lat"], b["lng"])
            for b in base
            if b.get("lat") is not None and b.get("lng") is not None
        }
        out = list(base)

        for half_km in (3.2, 6.4, 10.0):
            if len(out) >= max(target, 500):
                break
            r = half_km / 111.0
            cosla = max(0.2, math.cos(math.radians(lat0)))
            s, n = lat0 - r, lat0 + r
            w, e = lng0 - r / cosla, lng0 + r / cosla
            q = (
                f'[out:json][timeout:50];'
                f'(way["building"]({s},{w},{n},{e}););'
                f'out center tags {max(target, 1000)};'
            )
            for ep in (
                "https://overpass-api.de/api/interpreter",
                "https://overpass.kumi.systems/api/interpreter",
                "https://overpass.private.coffee/api/interpreter",
            ):
                try:
                    req = urllib.request.Request(
                        ep,
                        data=q.encode(),
                        headers={
                            "User-Agent": "RoofScout/1.0 (residential roof condition finder)",
                            "Content-Type": "text/plain",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=55) as resp:
                        data = json.loads(resp.read().decode("utf-8", "replace"))
                    extra = _parse_overpass_buildings(data)
                    for b in extra:
                        cell = pipeline._cell_of(b["lat"], b["lng"])
                        if cell not in seen:
                            seen.add(cell)
                            out.append(b)
                    if extra:
                        break
                except Exception:
                    continue

        return out

    pipeline._overpass_buildings = _wide_overpass
    pipeline.MICRO_SCAN_POOL = max(getattr(pipeline, "MICRO_SCAN_POOL", 200), 500)

    _original_candidates = pipeline.candidate_roofs

    def _spread_candidates(zipcode, center, count, progress=None, exclude_cells=None):
        houses, footprints_ok = _original_candidates(
            zipcode, center, count, progress, exclude_cells=exclude_cells
        )

        # Never promote blind grid points to roof candidates.
        if not footprints_ok:
            return [], False
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

    tarp_rule = """

TARP / SEVERE-DAMAGE RULE: A temporary tarp visibly covering the target roof is direct evidence of active roof failure or storm repair. A substantial roof tarp supports grade 1; a smaller localized roof tarp supports grade 2 unless other visible evidence makes the roof failing. Do not require blue: black, gray, white, green, brown, and other temporary coverings count when clearly on the roof. Also look for large bare/exposed areas, missing material, collapsed or sagging sections, and obvious emergency patches. Do not confuse pool covers, yard tarps, vehicle covers, tents, or ground objects with a roof tarp.
"""
    if tarp_rule not in pipeline.GRADE_PROMPT:
        pipeline.GRADE_PROMPT += tarp_rule
    if tarp_rule not in pipeline.PRESCREEN_PROMPT:
        pipeline.PRESCREEN_PROMPT += tarp_rule

    roof_equipment_rule = """

ROOF-EQUIPMENT RULE: Do NOT treat normal roof fixtures as damage or as a reason to lower the roof grade. Vents, plumbing stacks, HVAC units, ducts, skylights, solar panels, chimneys, satellite dishes, and normal rooftop equipment are expected roof features. Do not count the mere presence, shape, shadow, or protrusion of these fixtures as damage. Only count a fixture when there is visible evidence of damage to the fixture itself, missing/broken material around it, failed or exposed flashing, an opening, active leakage evidence, or another clearly abnormal condition. In particular, ordinary circular roof vents must never by themselves make a roof a Grade 1, Grade 2, or Grade 3 lead.
"""
    if roof_equipment_rule not in pipeline.GRADE_PROMPT:
        pipeline.GRADE_PROMPT += roof_equipment_rule
    if roof_equipment_rule not in pipeline.PRESCREEN_PROMPT:
        pipeline.PRESCREEN_PROMPT += roof_equipment_rule

    # Make the micro-pass explicitly hunt for the highest-value damage first.
    micro_priority_rule = """

MICRO-SCAN PRIORITY: This is a search pass, not the final grade. Search every candidate for emergency tarps and likely Grade 1 failure first. A substantial roof tarp, exposed underlayment/deck, collapse, major bare roof area, or severe structural deformation should score 5 (the strongest micro signal and likely final Grade 1). A smaller/localized roof tarp or clear missing/lifted material should score 4 (likely final Grade 2). Ordinary visible wear should score 3 (likely final Grade 3). Never let a healthy roof outrank a tarp/severe-damage roof. Ignore pool covers, yard tarps, cars, tents, and ground objects. Do not score normal vents, HVAC equipment, ducts, skylights, chimneys, solar panels, or other ordinary roof fixtures as damage unless the fixture or its surrounding roof/flashing is visibly damaged.
"""
    if micro_priority_rule not in pipeline.PRESCREEN_PROMPT:
        pipeline.PRESCREEN_PROMPT += micro_priority_rule

    _original_prescreen = pipeline.prescreen_damage

    def _prescreen_more_results(api_key, cands, count, progress=None, prescreener=None):
        requested = int(count)
        target = max(requested, 30) if requested >= 20 else requested
        return _original_prescreen(
            api_key, cands, target, progress, prescreener=prescreener
        )

    pipeline.prescreen_damage = _prescreen_more_results

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
    print(f"Roof Scout tuning skipped: {_roofscout_tuning_error}", flush=True)

# Micro-first widening: run several cheap triage rounds outward before spending
# the deep grading budget. This makes the search order: likely 1/tarp -> 2 -> 3,
# rather than stopping as soon as the first damaged roof is found.
try:
    import server

    _original_widened_scan = server._do_widened_scan
    server._WIDEN_ROUNDS = max(int(getattr(server, "_WIDEN_ROUNDS", 3)), 6)

    def _micro_first_widened_scan(job_id, zipcode, center, count, user_id, progress,
                                  area, cache, grader=None, prescreener=None):
        if not (server.GEMINI_KEY or grader or prescreener):
            return _original_widened_scan(
                job_id, zipcode, center, count, user_id, progress,
                area, cache, grader=grader, prescreener=prescreener
            )

        exclude = server._seen_cells(user_id, zipcode)
        triaged_all = []
        footprints_ok = True
        rounds = int(getattr(server, "_WIDEN_ROUNDS", 6))

        for rnd in range(rounds):
            cands, fok = pipeline.candidate_roofs(
                zipcode, center, count, progress, exclude_cells=exclude
            )
            if not cands:
                break
            footprints_ok = footprints_ok and fok
            server._set_job(job_id, footprints_ok=footprints_ok)

            pipeline.prescreen_damage(
                server.GEMINI_KEY, cands, count, progress,
                prescreener=prescreener
            )
            triaged = [c for c in cands if "micro_score" in c]
            if not triaged:
                break
            triaged_all.extend(triaged)
            exclude |= {
                pipeline._cell_of(c["lat"], c["lng"])
                for c in triaged
                if c.get("lat") is not None and c.get("lng") is not None
            }

            counts = {
                5: sum(1 for c in triaged_all if c.get("micro_score") == 5),
                4: sum(1 for c in triaged_all if c.get("micro_score") == 4),
                3: sum(1 for c in triaged_all if c.get("micro_score") == 3),
            }
            if progress:
                progress(
                    "prescreen", len(triaged_all), max(len(triaged_all), 1),
                    f"Micro-search round {rnd + 1}/{rounds}: "
                    f"{counts[5]} severe/tarp, {counts[4]} clear damage, "
                    f"{counts[3]} aging — widening for more severe roofs…"
                )

        if not triaged_all:
            server._fail_job(job_id,
                             "No real roof footprints found in the widened area. "
                             "Your scan was refunded.")
            return False

        # Micro score is intentionally ordered like the final scale:
        # 5 ~= Grade 1, 4 ~= Grade 2, 3 ~= Grade 3. Tarp/severe signals are
        # pushed to the front by the prescreen prompt before this sort.
        ranked = sorted(
            triaged_all,
            key=lambda h: (
                -int(h.get("micro_score", 0)),
                bool(h.get("vacant")),
                h.get("address", ""),
            )
        )
        # De-duplicate by the same roof cell in case a source returned a
        # slightly different coordinate on another widening round.
        chosen = []
        chosen_cells = set()
        for h in ranked:
            cell = pipeline._cell_of(h["lat"], h["lng"])
            if cell in chosen_cells:
                continue
            chosen_cells.add(cell)
            if h.get("micro_score", 0) >= 3:
                chosen.append(h)
            if len(chosen) >= count:
                break

        # If there are fewer than `count` damaged micro candidates, backfill
        # with the best remaining roofs so the deep grader can verify them.
        if len(chosen) < count:
            for h in ranked:
                cell = pipeline._cell_of(h["lat"], h["lng"])
                if cell in chosen_cells:
                    continue
                chosen_cells.add(cell)
                chosen.append(h)
                if len(chosen) >= count:
                    break

        for h in chosen:
            h["key"] = (h.get("address") or "") + "|" + h.get("postcode", "")

        if progress:
            score_counts = {
                s: sum(1 for h in chosen if h.get("micro_score") == s)
                for s in (5, 4, 3, 2, 1, 0)
            }
            progress(
                "prescreen", len(chosen), len(chosen),
                "Deep scan priority: "
                f"Grade 1/tarp {score_counts[5]} → "
                f"Grade 2 {score_counts[4]} → "
                f"Grade 3 {score_counts[3]}."
            )

        server._run_houses(
            job_id, chosen, area, cache=cache, grader=grader,
            user_id=user_id, footprints_ok=footprints_ok
        )
        found = bool(server._jobs.get(job_id, {}).get("leads"))
        server._record_seen(user_id, zipcode, triaged_all)
        if not found:
            # The micro pass found no final 1-3 roofs. We already exhausted
            # the widened cheap search, so refund rather than pretending the
            # original small area was representative.
            server._refund_user_scan((server._jobs.get(job_id) or {}).get("owner"))
            server._set_job(
                job_id, status="done", leads=[], area=area,
                evaluated=len(chosen),
                msg=("Done — no visibly damaged roofs found after the "
                     f"micro-search widened across {len(triaged_all)} roofs. "
                     "No charge — your scan was refunded.")
            )
        return found

    server._do_widened_scan = _micro_first_widened_scan
except Exception as _micro_widen_error:
    print(f"Roof Scout micro widening skipped: {_micro_widen_error}", flush=True)

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

# Cache/version guard: older cached ZIP results can contain the exact
# "19 properties evaluated" behavior the new widening engine is meant to
# replace. Only reuse results produced by this scan engine version.
try:
    import server

    _SCAN_ENGINE_VERSION = "micro-widen-v4-equipment"
    _original_cache_get = server._cache_get
    _original_cache_put = server._cache_put

    def _cache_get_versioned(zipcode, count):
        payload = _original_cache_get(zipcode, count)
        if not payload or payload.get("_scan_engine_version") != _SCAN_ENGINE_VERSION:
            return None
        return payload

    def _cache_put_versioned(zipcode, count, payload):
        payload = dict(payload)
        payload["_scan_engine_version"] = _SCAN_ENGINE_VERSION
        return _original_cache_put(zipcode, count, payload)

    server._cache_get = _cache_get_versioned
    server._cache_put = _cache_put_versioned
    server._WIDEN_ROUNDS = max(int(getattr(server, "_WIDEN_ROUNDS", 6)), 6)
    print("Roof Scout scan engine: micro-widen-v4-equipment cache guard active", flush=True)
except Exception as _cache_tuning_error:
    print(f"Roof Scout cache tuning skipped: {_cache_tuning_error}", flush=True)