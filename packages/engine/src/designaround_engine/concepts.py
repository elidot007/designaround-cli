"""2D design concepts of a case: generated images, independent reviews, the user's choice.

A concept is the first, cheap step of designing a new product from a brief: an image of
one design direction. Concepts are reviewed by an isolated reviewer (fit to the brief,
manufacturability, no text, distinct from the other concepts and from prior art). The
user picks one, and the CAD Agent then builds the 3D model from it.

Layout: <case>/concepts/<cN>/{image.png, meta.json}. Every step is an evidence event
(concept.created with the image hash, concept.reviewed, decision for the choice).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from .errors import InvalidInput, NotFound
from .workspace import Case, open_case

PASS_RULE = {"fit": 7, "manufacturable": 6}


def _dir(case: Case) -> Path:
    path = case.root / "concepts"
    path.mkdir(exist_ok=True)
    return path


def _ids(case: Case) -> list[str]:
    return sorted((p.name for p in _dir(case).iterdir() if p.is_dir() and p.name.startswith("c") and p.name[1:].isdigit()), key=lambda s: int(s[1:]))


def _meta_path(case: Case, cid: str) -> Path:
    path = _dir(case) / cid / "meta.json"
    if not path.exists():
        raise NotFound(f"Concept {cid} not found in case {case.id}")
    return path


def _read(case: Case, cid: str) -> dict:
    meta = json.loads(_meta_path(case, cid).read_text(encoding="utf-8"))
    meta["image"] = str(_dir(case) / cid / "image.png")
    meta["chosen"] = case.meta.get("chosen_concept") == cid
    return meta


def _write(case: Case, cid: str, meta: dict) -> None:
    data = {k: v for k, v in meta.items() if k not in ("image", "chosen")}
    (_dir(case) / cid / "meta.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


_BLOCKING = {"block", "error", "major", "blocking"}


def _blocking_issue(issue) -> bool:
    """A string issue is a problem to fix. An object blocks unless its severity is detail/info/warning."""

    if isinstance(issue, str):
        return bool(issue.strip())
    if isinstance(issue, dict):
        text = issue.get("issue") or issue.get("message") or issue.get("text") or ""
        severity = str(issue.get("severity") or "major").lower()
        return bool(str(text).strip()) and severity in _BLOCKING
    return False


def review_passes(review: dict) -> bool:
    """Deterministic pass rule over the reviewer's scores (the reviewer does not decide alone)."""

    try:
        fit = float(review.get("fit", 0))
        made = float(review.get("manufacturable", 0))
    except (TypeError, ValueError):
        return False
    if review.get("resembles_prior_art"):
        return False
    if any(_blocking_issue(item) for item in review.get("issues") or []):
        return False
    return fit >= PASS_RULE["fit"] and made >= PASS_RULE["manufacturable"] and not review.get("has_text") and review.get("single_product", True) is not False


def concept_save(case_id: str, image_path: str, *, title: str, idea: str, prompt: str, features: list[str] | None = None, round: int = 1, parent: str | None = None, created_by: str = "agent", model: str | None = None) -> dict:
    case = open_case(case_id)
    src = Path(image_path)
    if not src.exists():
        raise NotFound(f"Concept image not found: {image_path}")
    if parent and not (_dir(case) / parent).exists():
        raise InvalidInput(f"Unknown parent concept {parent}")
    ids = _ids(case)
    cid = f"c{int(ids[-1][1:]) + 1 if ids else 1}"
    folder = _dir(case) / cid
    folder.mkdir()
    shutil.copyfile(src, folder / "image.png")
    digest = hashlib.sha256((folder / "image.png").read_bytes()).hexdigest()
    meta = {
        "id": cid,
        "title": title.strip()[:120],
        "idea": idea.strip()[:1200],
        "features": [str(f)[:200] for f in (features or [])][:12],
        "prompt": prompt[:4000],
        "round": int(round),
        "parent": parent,
        "created_by": created_by,
        "model": model,
        "created": time.time(),
        "sha256": digest,
        "status": "new",
        "review": None,
    }
    _write(case, cid, meta)
    case.event("concept.created", {"concept": cid, "parent": parent, "title": meta["title"], "sha256": digest, "model": model, "created_by": created_by})
    return _read(case, cid)


def concept_review(case_id: str, cid: str, review: dict) -> dict:
    """Store an independent review. status becomes passed or needs_revision."""

    case = open_case(case_id)
    meta = _read(case, cid)
    review = dict(review)
    review["pass"] = review_passes(review)
    review["reviewed_at"] = time.time()
    meta["review"] = review
    if meta.get("status") != "superseded":
        meta["status"] = "passed" if review["pass"] else "needs_revision"
    _write(case, cid, meta)
    case.event("concept.reviewed", {"concept": cid, "pass": review["pass"], "fit": review.get("fit"), "manufacturable": review.get("manufacturable"), "issues": (review.get("issues") or [])[:6], "model": review.get("model")})
    return _read(case, cid)


def concept_supersede(case_id: str, cid: str, by: str) -> dict:
    case = open_case(case_id)
    meta = _read(case, cid)
    meta["status"] = "superseded"
    meta["superseded_by"] = by
    _write(case, cid, meta)
    return _read(case, cid)


def concepts(case_id: str, *, include_superseded: bool = False) -> list[dict]:
    case = open_case(case_id)
    rows = [_read(case, cid) for cid in _ids(case)]
    return rows if include_superseded else [r for r in rows if r.get("status") != "superseded"]


def concept_show(case_id: str, cid: str) -> dict:
    return _read(open_case(case_id), cid)


def concept_choose(case_id: str, cid: str, *, person: str | None = None, note: str | None = None) -> dict:
    """The user's choice of a design direction (a human decision in the evidence log)."""

    case = open_case(case_id)
    meta = _read(case, cid)
    if meta.get("status") == "superseded":
        raise InvalidInput(f"{cid} was replaced by {meta.get('superseded_by')}; choose that one instead.")
    case.update(chosen_concept=cid)
    case.event("decision", {"actor": "user", "decision": f"chose design concept {cid}", "detail": {"concept": cid, "title": meta.get("title"), "note": note}, "person": person, "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return _read(case, cid)


def concept_sheet(case_id: str, ids: list[str] | None = None, *, out: str | None = None, cell: int = 520) -> dict:
    """Grid of concept images with their ids (what the user and the reviewers compare)."""

    from PIL import Image, ImageDraw, ImageFont

    case = open_case(case_id)
    rows = [r for r in concepts(case_id) if not ids or r["id"] in ids]
    if not rows:
        raise NotFound("No concepts on this case yet.")
    cols = min(len(rows), 4 if len(rows) != 4 else 2) if len(rows) > 2 else len(rows)
    lines = -(-len(rows) // cols)
    label_h = 44
    sheet = Image.new("RGB", (cols * cell, lines * (cell + label_h)), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=26)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    for i, row in enumerate(rows):
        x, y = (i % cols) * cell, (i // cols) * (cell + label_h)
        img = Image.open(row["image"]).convert("RGB")
        img.thumbnail((cell - 16, cell - 16))
        sheet.paste(img, (x + (cell - img.width) // 2, y + label_h + (cell - img.height) // 2))
        review = row.get("review") or {}
        score = f"  fit {review.get('fit')}/10" if review.get("fit") is not None else ""
        mark = "  [chosen]" if row.get("chosen") else ""
        draw.text((x + 14, y + 10), f"{row['id']}{score}{mark}", fill=(18, 19, 21), font=font)
    target = Path(out) if out else _dir(case) / f"sheet-{'-'.join(r['id'] for r in rows)}.png"
    sheet.save(target)
    return {"path": str(target), "concepts": [r["id"] for r in rows]}
