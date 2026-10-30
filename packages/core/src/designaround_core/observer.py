"""The isolated observer's rubric. One source of truth for the hosted observer
(server), the BYOK observer (embedded Pi Agent) and host mode.

The observer never sees the designer's rationale; it only sees the candidate
views, the prior-art figures and the claimed-scope notes.
"""

from __future__ import annotations

import json
import re

RUBRIC_VERSION = "observer-2026.10-2"

SYSTEM_PROMPT = """You are an independent visual reviewer who imitates the "ordinary observer" used when comparing a product design with a registered design (US design patent / CN design patent / EU registered design).

You receive ONE comparison sheet for ONE prior-art right:
- top row "CANDIDATE": line drawings of the candidate product (perspective, front, right side, top);
- bottom row "PRIOR ART": the figures of the registered design (views may be in a different order or orientation).
You may also receive notes on what the prior art claims (solid lines are claimed; broken lines are not claimed; CN partial designs claim only the indicated portion).

How to judge:
1. Compare only the claimed portions of the prior art. Ignore broken-line (dashed) environment.
2. Judge the overall visual impression of the product as a whole, across all views. Match views by content, not by position on the sheet.
3. Give little weight to features that almost every product of this type has (for a storage box: a rectangular or rounded-rectangular footprint, a lid, flat side walls) and to features dictated by function. Give most weight to the features that make the prior art distinctive: its proportions, the form of its lid, handles, rims, feet, ribs, surface patterns and profile lines.
4. Small changes to corner radii, chamfers, scale or colour do not change the overall impression.
5. If the field is crowded with similar registered designs, smaller differences are enough to create a different impression; if the prior art is distinctive, the candidate must differ in the distinctive features.
6. Never state that a design "does not infringe", is "safe" or is "guaranteed". You produce a risk score for a professional to review.

Score anchors (10 = substantially the same to an ordinary observer):
- 9-10: same design; differences only visible on close side-by-side study.
- 7-8: same overall impression with minor differences.
- 5-6: shares the main design concept, but at least one distinctive feature clearly differs.
- 3-4: different overall impression; shares the general product concept and some secondary features.
- 0-2: clearly different designs; shared features are only those common to products of this type.

Return ONLY a JSON object:
{"score": <0-10 number>,
 "similar": ["<top shared visual features, max 3>"],
 "different": ["<top differences, max 3>"],
 "distinctive_features_of_prior_art": ["<max 3>"],
 "uncertain": "<what could change the score, e.g. hidden views, scope unclear>"}"""

USER_TEMPLATE = """Product type: {article}
Prior art: {number} ({office}) — {title}
Claimed-scope notes: {scope}
Field: {field_size} other registered designs of this product type were found in screening.
Candidate version: {version}; sheet: {view}
Score how similar the overall visual impression of the candidate is to the CLAIMED design of the prior art."""


def user_prompt(*, number: str, office: str | None, title: str | None, scope: str | None, version: str, view: str, article: str | None = None, field_size: int | None = None) -> str:
    return USER_TEMPLATE.format(
        article=article or "consumer product",
        number=number,
        office=office or "?",
        title=title or "",
        scope=scope or "none provided; assume solid lines are claimed",
        field_size=field_size if field_size is not None else "unknown",
        version=version,
        view="multi-view comparison sheet" if view == "multi" else f"single view ({view})",
    )


def parse_reply(text: str) -> dict:
    """Extract the JSON object from a model reply and validate the score."""

    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ValueError("observer reply has no JSON object")
    data = json.loads(match.group(0))
    score = float(data.get("score"))
    if not 0 <= score <= 10:
        raise ValueError("observer score out of range")
    data["score"] = score
    for key in ("similar", "different", "distinctive_features_of_prior_art"):
        value = data.get(key) or []
        data[key] = [str(v) for v in value][:3] if isinstance(value, list) else [str(value)]
    data["rubric_version"] = RUBRIC_VERSION
    return data


SAMPLES = 3  # independent judgments per comparison; the median is used


def combine(samples: list[dict]) -> dict:
    """Median of independent judgments (single VLM judgments vary by +-2 points).

    The reported lists come from the judgment closest to the median.
    """

    if not samples:
        raise ValueError("no observer samples")
    scores = sorted(float(s["score"]) for s in samples)
    n = len(scores)
    median = scores[n // 2] if n % 2 else (scores[n // 2 - 1] + scores[n // 2]) / 2
    pick = min(samples, key=lambda s: abs(float(s["score"]) - median))
    return {
        **pick,
        "score": round(median, 2),
        "samples": [float(s["score"]) for s in samples],
        "spread": round(scores[-1] - scores[0], 2),
        "rubric_version": RUBRIC_VERSION,
    }


def rubric() -> dict:
    return {"version": RUBRIC_VERSION, "system": SYSTEM_PROMPT, "user_template": USER_TEMPLATE}
