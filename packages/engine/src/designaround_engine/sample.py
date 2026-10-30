"""Free sample case (FR-UX-05): the whole flow without credits, network or a model.

The sample is a lidded storage box with three ILLUSTRATIVE prior designs (line drawings
of other boxes generated here, numbered SAMPLE-*), illustrative observer scores, a
design-around candidate and a free watermarked view pack. Everything is labelled as
sample data, so nobody mistakes it for a real search result.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from . import api, cad, views
from .templates import get_template_source

SAMPLE_NOTE = "示例数据（SAMPLE）：在先设计和观察者评分都是演示用的，不是真实检索结果。Sample data: the prior designs and observer scores are illustrative, not real results."

# Variants of the template that play the "registered designs" of the sample.
PRIOR = [
    ("SAMPLE-D0001", "Storage box (sample)", {"handle_style": "arched", "corner_radius": 20.0, "lid_overhang": 5.0, "taper_deg": 3.5}, 6.0,
     ["相同的圆角矩形箱体和外伸箱盖", "箱盖中央拱形提手"], ["箱体略高", "侧面无加强筋"]),
    ("SAMPLE-D0002", "Stackable bin (sample)", {"handle_style": "bar", "corner_radius": 10.0, "rib_count": 3, "lid_dome": 8.0}, 4.0,
     ["整体为带盖箱"], ["侧面三道加强筋", "拱起的箱盖", "直条提手"]),
    ("SAMPLE-D0003", "Food container (sample)", {"handle_style": "none", "corner_radius": 40.0, "height": 120.0, "lid_height": 14.0}, 2.0,
     ["都有可拆卸箱盖"], ["很矮的扁平比例", "大圆角近似椭圆", "没有提手"]),
]
CANDIDATE = {"handle_style": "recessed", "lid_dome": 14.0, "rib_count": 4, "corner_radius": 8.0, "taper_deg": 1.5}


def _figures(overrides: dict, tmp: Path, name: str) -> list[str]:
    out = tmp / name
    cad.build_program(get_template_source("storage_box"), overrides, out)
    views.render_views(out, keys=["perspective", "front", "left", "top"], renders=False, size=900)
    return [str(out / "views" / f"{k}.lineart.png") for k in ("perspective", "front", "left", "top")]


def create_sample(*, lang: str = "zh") -> dict:
    name = "示例：带盖收纳箱" if lang == "zh" else "Sample: lidded storage box"
    case = api.case_create(name, article="storage box", article_zh="收纳箱", jurisdictions=["US", "CN"])
    cid = case["id"]
    api.case_update(cid, status="sample", description=SAMPLE_NOTE, use_zh="用于收纳衣物和杂物的带盖箱", design_points_zh=["箱盖内凹式提手", "拱起的箱盖", "侧面四道竖向加强筋"])
    original = api.version_from_template(cid, "storage_box", {}, created_by="sample", label="原款（示例）" if lang == "zh" else "Original (sample)")
    with tempfile.TemporaryDirectory(prefix="da-sample-") as tmp:
        prior_ids = []
        for number, title, overrides, _score, _sim, _diff in PRIOR:
            pa = api.prior_art_add(cid, number=number, office="US", title=title, figures=_figures(overrides, Path(tmp), number), status="SAMPLE", date="2024-01-01", owner="Sample data", source="sample", scope_notes=SAMPLE_NOTE)
            prior_ids.append(pa["id"])
    judge = {}
    for pid, (_n, _t, _o, score, similar, different) in zip(prior_ids, PRIOR, strict=True):
        judge[pid] = {"score": score, "similar": similar, "different": different, "rationale": "示例评分（不是模型给出的）。Illustrative score, not a model judgment.", "model": "sample", "rubric_version": None}
    api.evaluate(cid, original["id"], judge=judge, judge_source="sample")
    candidate = api.tweak(cid, original["id"], CANDIDATE, created_by="sample", label="改型方案（示例）" if lang == "zh" else "Design-around (sample)", rationale="改提手为内凹式、箱盖拱起、侧面加强筋，改变主要设计要点。")
    lower = {pid: {**j, "score": max(1.0, j["score"] - 3.0), "different": j["different"] + ["内凹式提手", "拱起的箱盖", "四道加强筋"]} for pid, j in judge.items()}
    api.evaluate(cid, candidate["id"], judge=lower, judge_source="sample")
    api.originality_check(cid, candidate["id"])
    api.dfm_check(cid, candidate["id"], "injection")
    api.decision(cid, "chose the sample design-around candidate", {"version": candidate["id"]}, person="Sample user")
    vp = api.make_viewpack(cid, candidate["id"], "US", watermark=True)
    return {"case": cid, "original": original["id"], "candidate": candidate["id"], "prior_art": prior_ids, "viewpack": vp.get("dir") or vp.get("path"), "note": SAMPLE_NOTE}
