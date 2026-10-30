"""Risk fusion (FR-EVAL-02). Version 0 is *uncalibrated*: it combines the
CPU shape score with the isolated observer's 0–10 score. Calibration against
practitioner labels replaces the weights once the D4 dataset exists.

Output wording is always "risk assessment", never "non-infringing".
"""

from __future__ import annotations

# Silhouettes are a recall signal (which prior art to look at): every storage
# box has a rounded-rectangle outline, so silhouette similarity saturates for
# a whole product type. Once the observer (median of 3 multi-view judgements)
# has scored a right, its overall-impression score governs; the silhouette only
# nudges. With these weights a candidate qualifies against a right when the
# observer scores <= ~3.3/10 ("different overall impression" band) even at
# full silhouette similarity. Live check (DeepSeek V4.1 Flash, 2026-10-27):
# same-type boxes score 4-6, restyled candidates 2-4.
FUSION = {"version": "fusion-v2-uncalibrated", "w_shape": 0.1, "w_judge": 0.9, "shape_floor": 0.50, "shape_ceiling": 0.92}
LEVELS = [(0.66, "high"), (0.40, "medium"), (0.18, "low")]
QUALIFY_THRESHOLD = 0.40  # a candidate qualifies when every reviewed prior right is below this
# Silhouettes are for recall: the REVIEW_TOP_K most similar prior rights (by
# silhouette, for the version being evaluated) must have an observer score.
# Prior rights outside the top K without an observer score are "screened out".
REVIEW_TOP_K = 3


def shape_risk(shape_score: float) -> float:
    lo, hi = FUSION["shape_floor"], FUSION["shape_ceiling"]
    return max(0.0, min(1.0, (shape_score - lo) / (hi - lo)))


def fuse(shape_score: float, judge_score: float | None) -> dict:
    s = shape_risk(shape_score)
    if judge_score is None:
        risk = s
        basis = "shape-only"
    else:
        j = max(0.0, min(1.0, judge_score / 10.0))
        risk = FUSION["w_shape"] * s + FUSION["w_judge"] * j
        basis = "shape+observer"
    return {"risk": round(risk, 4), "level": level(risk), "basis": basis, "fusion": FUSION["version"]}


def level(risk: float) -> str:
    for threshold, name in LEVELS:
        if risk >= threshold:
            return name
    return "not_seen"


def summarize(per_right: list[dict], *, top_k: int = REVIEW_TOP_K) -> dict:
    """per_right is sorted by silhouette similarity (most similar first).

    Counted rows: every row with an observer score, plus the top_k rows by
    silhouette (provisional shape-only risk while their observer score is
    pending). A version qualifies when nothing is pending and every counted
    row is below the threshold.
    """

    if not per_right:
        return {"max_risk": 0.0, "level": "not_seen", "qualified": False, "reason": "no prior art evaluated", "pending_observer": [], "review_top_k": top_k, "screened_out": 0}
    counted, pending, screened_out = [], [], 0
    for index, row in enumerate(per_right):
        observed = row.get("basis") == "shape+observer"
        if observed or index < top_k:
            counted.append(row)
            if not observed:
                pending.append(row.get("prior_art_id"))
        else:
            screened_out += 1
    worst = max(counted, key=lambda r: r["risk"])
    qualified = not pending and worst["risk"] < QUALIFY_THRESHOLD
    return {
        "max_risk": worst["risk"],
        "level": worst["level"],
        "worst_prior_art": worst.get("prior_art_id"),
        "qualified": qualified,
        "threshold": QUALIFY_THRESHOLD,
        "pending_observer": pending,
        "review_top_k": top_k,
        "screened_out": screened_out,
        **({"reason": f"observer score pending for {', '.join(pending)}"} if pending else {}),
    }
