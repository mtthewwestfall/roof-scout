"""Strict second-pass quality control for RoofScout Grade 1 candidates."""
from __future__ import annotations

import base64

import pipeline


_STRICT_PROMPT = """You are the final quality-control reviewer for an aerial residential roof lead.

A previous model marked this roof as Grade 1 (failing/replacement-level). Your job is NOT to be generous. Confirm Grade 1 only when the image contains strong, unmistakable, visible evidence of severe roof failure.

CONFIRM GRADE 1 ONLY when you can clearly see one or more of:
- a substantial temporary roof tarp covering a meaningful roof area
- a large section of missing roof covering with exposed underlayment/decking
- a major open hole or missing roof section
- a visibly collapsed or strongly sagging roof section
- severe structural deformation
- unmistakable widespread material failure that is clearly replacement-level from the image

DO NOT confirm Grade 1 for:
- discoloration, staining, fading, or rough texture alone
- ordinary aging or granule/color variation
- ponding alone
- ordinary patches or maintenance repairs
- shadows, glare, compression artifacts, or trees
- vents, plumbing stacks, HVAC units, ducts, skylights, chimneys, solar panels, satellite dishes, or other normal rooftop equipment
- a dark rectangle/open-looking shape unless it is clearly a real roof opening
- a small localized defect that is better described as repair-level

If there is visible real damage but it is NOT clearly failing/replacement-level, return Grade 2 or 3. If the roof cannot be reliably judged, return 0.

Return only the requested JSON object."""

_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "grade": {"type": "INTEGER"},
        "confirmed": {"type": "BOOLEAN"},
        "confidence": {"type": "STRING", "enum": ["low", "medium", "high"]},
        "reason": {"type": "STRING"},
        "evidence_type": {"type": "STRING"},
    },
    "required": ["grade", "confirmed", "confidence", "reason", "evidence_type"],
}


def _review_one(h: dict, api_key: str):
    image_b64 = h.get("image_b64")
    if not image_b64 or not api_key:
        return None

    # Use a tighter Esri crop when available. If Esri cannot provide a crop,
    # keep the exact validated image already used by the first grader.
    image = None
    try:
        z = int(h.get("image_zoom") or 20)
        image = pipeline._centered_esri(
            float(h["lat"]), float(h["lng"]), min(20, z + 1)
        )
    except Exception:
        image = None
    review_b64 = base64.b64encode(image).decode() if image else image_b64

    parts = [
        {"text": _STRICT_PROMPT},
        {"text": "Review only the target roof centered in this image."},
        {"inline_data": {"mime_type": "image/jpeg", "data": review_b64}},
    ]
    try:
        return pipeline._gemini_json(api_key, parts, _SCHEMA, max_tokens=700)
    except Exception:
        return None


def verify_grade1_candidates(api_key: str, houses: list[dict], progress=None):
    """Fail-closed second pass: only verified severe roofs remain Grade 1."""
    candidates = [
        h for h in houses
        if int(h.get("grade", 0) or 0) == 1 and h.get("image_b64")
    ]
    if not candidates:
        return houses

    for idx, h in enumerate(candidates, 1):
        raw = _review_one(h, api_key)

        if not isinstance(raw, dict):
            h["grade"] = 0
            h["grade1_verified"] = False
            h["grade1_verification_confidence"] = "low"
            h["grade1_verification_reason"] = (
                "Final Grade 1 verification was unavailable; "
                "replacement-level failure could not be confirmed."
            )
            h["evidence_type"] = "unverified"
        else:
            confirmed = (
                bool(raw.get("confirmed"))
                and int(raw.get("grade", 0) or 0) == 1
            )
            confidence = str(raw.get("confidence", "low"))[:10]
            reason = str(raw.get("reason", ""))[:300]
            evidence_type = str(
                raw.get("evidence_type", "visible severe failure")
            )[:100]

            if confirmed and confidence in ("medium", "high"):
                h["grade"] = 1
                h["grade1_verified"] = True
                h["grade1_verification_confidence"] = confidence
                h["grade1_verification_reason"] = reason
                h["evidence_type"] = evidence_type
            else:
                fallback = int(raw.get("grade", 0) or 0)
                h["grade"] = fallback if fallback in (0, 2, 3) else 0
                h["grade1_verified"] = False
                h["grade1_verification_confidence"] = confidence
                h["grade1_verification_reason"] = (
                    reason or
                    "Visible evidence did not meet the strict Grade 1 threshold."
                )
                h["evidence_type"] = evidence_type or "not confirmed"
                ev = list(h.get("evidence") or [])
                ev.append(
                    "AI quality control did not confirm replacement-level "
                    "failure from the available aerial image."
                )
                h["evidence"] = ev[:3]

        if progress:
            progress(
                "grade", idx, len(candidates),
                f"Quality-checking potential Grade 1 roofs: "
                f"{idx}/{len(candidates)}",
            )

    return houses


_original_grade_roofs = pipeline.grade_roofs


def _grade_roofs_with_qc(api_key, houses, progress=None, grader=None, evidencer=None):
    out = _original_grade_roofs(
        api_key, houses, progress, grader=grader, evidencer=evidencer
    )
    return verify_grade1_candidates(api_key, out, progress=progress)


pipeline.grade_roofs = _grade_roofs_with_qc
