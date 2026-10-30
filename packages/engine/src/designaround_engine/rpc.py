"""JSON-lines RPC bridge used by the embedded Pi Agent team (agents/).

Request:  {"id": 1, "method": "version_from_program", "params": {...}}
Response: {"id": 1, "result": ...} | {"id": 1, "error": {"code", "message", "details"}}

Only the whitelisted functions below are callable. The process is started by
the agent runner with the same DESIGNAROUND_HOME as the CLI.
"""

from __future__ import annotations

import json
import sys
import traceback

from . import api, concepts
from .errors import EngineError

METHODS = {
    "templates": api.templates,
    "case_show": api.case_show,
    "case_update": api.case_update,
    "decision": api.decision,
    "versions": api.versions,
    "version_show": api.version_show,
    "version_from_template": api.version_from_template,
    "version_from_program": api.version_from_program,
    "try_program": api.try_program,
    "model_image": api.model_image,
    "decision_card": api.decision_card,
    "risk_report": api.risk_report,
    "compare_sheet": api.compare_sheet,
    "deliverables": api.deliverables,
    "baseline_list": api.baseline_list,
    "sample_case": api.sample_case,
    "accept_version": api.accept_version,
    "tweak": api.tweak,
    "set_claimed": api.set_claimed,
    "compare_versions": api.compare_versions,
    "prior_art_list": api.prior_art_list,
    "prior_art_add": api.prior_art_add,
    "shape_scores": api.shape_scores,
    "pair_image": api.pair_image,
    "comparison_image": api.comparison_image,
    "evaluate": api.evaluate,
    "review_set": api.review_set,
    "originality_check": api.originality_check,
    "dfm_check": api.dfm_check,
    "reconstruction_gate": api.reconstruction_gate,
    "photo_check": api.photo_check,
    "make_viewpack": api.make_viewpack,
    "filing_pack": api.filing_pack,
    "similar_candidates": api.similar_candidates,
    "timeline": api.timeline,
    "quote": api.quote,
    "evidence_head": api.evidence_head,
    "observer_rubric": api.observer_rubric,
    "observer_prompt": api.observer_prompt,
    "parse_observer_reply": api.parse_observer_reply,
    "combine_observer_samples": api.combine_observer_samples,
    "contact_sheet_for": api.contact_sheet_for,
    "params_schema": api.params_schema,
    "agent_event": api.agent_event,
    "case_photos": api.case_photos,
    "set_original": api.set_original,
    "concept_save": concepts.concept_save,
    "concept_review": concepts.concept_review,
    "concept_supersede": concepts.concept_supersede,
    "concepts": concepts.concepts,
    "concept_show": concepts.concept_show,
    "concept_sheet": concepts.concept_sheet,
}


def handle(line: str) -> dict:
    try:
        request = json.loads(line)
    except json.JSONDecodeError as exc:
        return {"id": None, "error": {"code": "bad_request", "message": str(exc)}}
    rid = request.get("id")
    method = request.get("method")
    fn = METHODS.get(method)
    if fn is None:
        return {"id": rid, "error": {"code": "unknown_method", "message": f"Unknown method {method}", "details": {"methods": sorted(METHODS)}}}
    try:
        return {"id": rid, "result": fn(**(request.get("params") or {}))}
    except EngineError as exc:
        return {"id": rid, "error": exc.to_dict()}
    except TypeError as exc:
        return {"id": rid, "error": {"code": "bad_params", "message": str(exc)}}
    except Exception as exc:  # noqa: BLE001
        return {"id": rid, "error": {"code": "internal", "message": f"{type(exc).__name__}: {exc}", "details": {"trace": traceback.format_exc()[-1500:]}}}


def main() -> int:
    out = sys.stdout
    sys.stdout = sys.stderr  # library prints must not corrupt the protocol
    out.write(json.dumps({"ready": True, "methods": sorted(METHODS)}) + "\n")
    out.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        out.write(json.dumps(handle(line), ensure_ascii=False, default=str) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
