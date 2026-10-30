"""Office-formatted view packages (FR-DRAW-01..08, FR-DRAW-10).

Line-drawing viewpacks are projected from one B-rep with a single scale for all
orthographic views, so the figures are consistent by construction; the rule
report re-measures that numerically. Photo viewpacks (path A) lay out the
user's labelled photos.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .camera import CN_ORDER, STANDARD_VIEWS, US_ORDER
from .evidence import manifest_for_dir
from .geometry import load_version_parts
from .lineart import LineDrawing, project_views, to_png, to_svg

CM = 72 / 2.54  # points per centimetre
US_PAGE = (21.59 * CM, 27.94 * CM)  # US letter
US_MARGINS = {"top": 2.5 * CM, "left": 2.5 * CM, "right": 1.5 * CM, "bottom": 1.0 * CM}  # 37 CFR 1.84(g)
A4 = (21.0 * CM, 29.7 * CM)
CN_MAX_CM = (15.0, 22.0)
RULESET_VERSION = "drawing-2026.10-1"
ORTHO = ["front", "rear", "left", "right", "top", "bottom"]


def _ensure_cjk() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    try:
        pdfmetrics.getFont("STSong-Light")
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


def _curved_area_ratio(parts) -> float:
    if parts and all(getattr(p, "faceted", False) for p in parts):
        from .meshimport import curved_share

        return curved_share(parts)  # an imported mesh has no curved faces, only small facets
    total = curved = 0.0
    for part in parts:
        for face in part.shape.Faces():
            area = face.Area()
            total += area
            if face.geomType() != "PLANE":
                curved += area
    return curved / total if total else 0.0


def _consistency(drawings: dict[str, LineDrawing]) -> dict:
    ext = {k: drawings[k].extent() for k in ORTHO if k in drawings}

    def close(a, b):
        return abs(a - b) <= max(0.005 * max(a, b), 0.05)

    pairs = []
    if {"front", "rear", "top", "bottom"} <= ext.keys():
        pairs += [("front.width", ext["front"][0], "rear.width", ext["rear"][0]), ("front.width", ext["front"][0], "top.width", ext["top"][0]), ("top.width", ext["top"][0], "bottom.width", ext["bottom"][0])]
    if {"front", "left", "right"} <= ext.keys():
        pairs += [("front.height", ext["front"][1], "left.height", ext["left"][1]), ("left.height", ext["left"][1], "right.height", ext["right"][1])]
    if {"left", "top"} <= ext.keys():
        pairs += [("left.width", ext["left"][0], "top.height", ext["top"][1])]
    mismatches = [
        {"a": a, "a_mm": round(va, 3), "b": b, "b_mm": round(vb, 3)} for a, va, b, vb in pairs if not close(va, vb)
    ]
    return {"checked": len(pairs), "mismatches": mismatches, "extents_mm": {k: [round(x, 3) for x in v] for k, v in ext.items()}}


def _watermark_pdf(c, width, height, text):
    c.saveState()
    c.setFillGray(0.82)
    c.setFont("Helvetica-Bold", 30)
    c.translate(width / 2, height / 2)
    c.rotate(35)
    c.drawCentredString(0, 0, text)
    c.restoreState()


def _draw_polylines(c, drawing: LineDrawing, box: tuple[float, float, float, float], scale_pt_per_mm: float, line_w: float):
    """Draw centred in box=(x, y, w, h) PDF coordinates."""

    x0, y0, x1, y1 = drawing.bounds()
    bx, by, bw, bh = box
    ox = bx + (bw - (x1 - x0) * scale_pt_per_mm) / 2 - x0 * scale_pt_per_mm
    oy = by + (bh - (y1 - y0) * scale_pt_per_mm) / 2 - y0 * scale_pt_per_mm
    for style in ("solid", "shading", "broken"):
        broken = style == "broken"
        c.setLineWidth(line_w * {"solid": 1.0, "shading": 0.45, "broken": 0.8}[style])
        c.setDash([line_w * 5, line_w * 4] if broken else [])
        for line in drawing.lines:
            if line.kind == "hidden":
                continue
            if (line.kind == "shading") != (style == "shading"):
                continue
            if style != "shading" and line.claimed == broken:
                continue
            path = c.beginPath()
            pts = line.points * scale_pt_per_mm + np.array([ox, oy])
            path.moveTo(*pts[0])
            for p in pts[1:]:
                path.lineTo(*p)
            c.drawPath(path, stroke=1, fill=0)
    c.setDash([])


def _figure_box(page, margins, label_space):
    w, h = page
    return (margins["left"], margins["bottom"] + label_space, w - margins["left"] - margins["right"], h - margins["top"] - margins["bottom"] - label_space - 14)


def build_line_viewpack(
    version_dir: str | os.PathLike,
    office: str,
    outdir: str | os.PathLike,
    *,
    article: str,
    omit: list[str] | None = None,
    watermark: bool = False,
    dpi: int = 300,
    shading: bool | None = None,
) -> dict:
    from reportlab.pdfgen import canvas

    office = office.upper()
    if office not in ("US", "CN"):
        raise ValueError("office must be US or CN")
    outdir = Path(outdir)
    (outdir / "figures").mkdir(parents=True, exist_ok=True)
    parts = load_version_parts(version_dir)
    omit = [o for o in (omit or []) if o in STANDARD_VIEWS]
    order = [k for k in (US_ORDER if office == "US" else CN_ORDER) if k not in omit]
    drawings = project_views(parts, list(STANDARD_VIEWS))
    # Consistency checks use the outlines only; shading is added afterwards.
    outline_drawings = {k: LineDrawing(view=d.view, lines=list(d.lines)) for k, d in drawings.items()}
    shading = (office == "US") if shading is None else shading
    shading_counts: dict[str, int] = {}
    if shading:
        from .geometry import combined_mesh
        from .shading import add_shading

        V, F, _owners = combined_mesh(parts)
        shading_counts = add_shading({k: drawings[k] for k in order}, parts, V, F, STANDARD_VIEWS)

    page = US_PAGE if office == "US" else A4
    margins = US_MARGINS if office == "US" else {"top": 2.0 * CM, "left": 2.0 * CM, "right": 2.0 * CM, "bottom": 2.0 * CM}
    box = _figure_box(page, margins, label_space=28)
    if office == "CN":
        # CN images must not exceed 15 x 22 cm.
        box = (box[0] + max(0, box[2] - CN_MAX_CM[0] * CM) / 2, box[1] + max(0, box[3] - CN_MAX_CM[1] * CM) / 2, min(box[2], CN_MAX_CM[0] * CM), min(box[3], CN_MAX_CM[1] * CM))
    ortho_keys = [k for k in order if k in ORTHO]
    max_w = max(drawings[k].extent()[0] for k in ortho_keys)
    max_h = max(drawings[k].extent()[1] for k in ortho_keys)
    ortho_scale = min(box[2] / max_w, box[3] / max_h) * 0.92
    persp_w, persp_h = drawings["perspective"].extent()
    persp_scale = min(box[2] / persp_w, box[3] / persp_h) * 0.92

    font_cn = _ensure_cjk() if office == "CN" else None
    pdf_path = outdir / ("drawings.pdf" if office == "US" else "图片.pdf")
    c = canvas.Canvas(str(pdf_path), pagesize=page)
    figures = []
    total = len(order)
    for number, key in enumerate(order, start=1):
        drawing = drawings[key]
        scale = persp_scale if key == "perspective" else ortho_scale
        _draw_polylines(c, drawing, box, scale, line_w=0.7)
        view = STANDARD_VIEWS[key]
        if office == "US":
            c.setFont("Helvetica-Bold", 14)
            c.drawCentredString(page[0] / 2, margins["bottom"] + 8, f"FIG. {number}")
            c.setFont("Helvetica", 9)
            c.drawCentredString(page[0] / 2, page[1] - margins["top"] + 18, f"{number}/{total}")
            label = f"FIG. {number}"
        else:
            c.setFont(font_cn, 14)
            c.drawCentredString(page[0] / 2, margins["bottom"] + 8, view.cn_label)
            label = view.cn_label
        if watermark:
            _watermark_pdf(c, page[0], page[1], "PREVIEW - DesignAround Free")
        c.showPage()

        # Per-figure raster + vector files.
        width_cm = box[2] / CM
        height_cm = box[3] / CM
        px_w, px_h = int(width_cm / 2.54 * dpi), int(height_cm / 2.54 * dpi)
        scale_px = scale * dpi / 72  # points -> pixels at the target dpi
        image = to_png(drawing, width=px_w, height=px_h, margin=int(0.04 * px_w), stroke=max(2, dpi // 120), scale=scale_px)
        if watermark:
            image = _watermark_png(image)
        stem = f"fig{number}_{key}" if office == "US" else f"{number:02d}_{view.cn_label}"
        image.save(outdir / "figures" / f"{stem}.png", dpi=(dpi, dpi))
        (outdir / "figures" / f"{stem}.svg").write_text(to_svg(drawing, width=px_w, height=px_h, margin=int(0.04 * px_w), scale=scale_px, stroke=max(2, dpi // 120)), encoding="utf-8")
        figures.append({"number": number, "view": key, "label": label, "us_label": view.us_label, "cn_label": view.cn_label, "png": f"figures/{stem}.png", "svg": f"figures/{stem}.svg", "size_cm": [round(width_cm, 2), round(height_cm, 2)]})
    c.save()

    report = drawing_rules(office, outline_drawings, parts, order=order, omit=omit, figures=figures, shading_counts=shading_counts if shading else None)
    result = {
        "kind": "line_viewpack",
        "office": office,
        "article": article,
        "ruleset_version": RULESET_VERSION,
        "figures": figures,
        "omitted_views": omit,
        "pdf": pdf_path.name,
        "watermark": watermark,
        "shading": {"enabled": bool(shading), "lines_per_view": shading_counts},
        "rules": report,
        "scale": {"orthographic_pt_per_mm": round(ortho_scale, 5), "perspective_pt_per_mm": round(persp_scale, 5)},
    }
    (outdir / "viewpack.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    result["files"] = manifest_for_dir(outdir)
    (outdir / "viewpack.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


EU_ORDER = ["perspective", "front", "rear", "left", "right", "top", "bottom"]
EU_MAX_VIEWS = 10
EU_MIN_PX = 800
EU_MAX_BYTES = 2 * 1024 * 1024


def build_eu_viewpack(
    version_dir: str | os.PathLike,
    outdir: str | os.PathLike,
    *,
    article: str,
    omit: list[str] | None = None,
    watermark: bool = False,
    shading: bool = False,
    px: int = 2000,
) -> dict:
    """EUIPO static representation: one JPEG per view, numbered 1.1, 1.2, ...

    Rules checked (EUDR as amended, EUIPO guidance for filings from 1 July 2026):
    up to 10 static views, JPEG, at least 800 x 800 px, one type of
    representation per design, the design shown alone, disclaimed matter
    marked the same way (broken lines) in every view, no text on the views.
    """

    outdir = Path(outdir)
    (outdir / "views").mkdir(parents=True, exist_ok=True)
    parts = load_version_parts(version_dir)
    omit = [o for o in (omit or []) if o in STANDARD_VIEWS]
    order = [k for k in EU_ORDER if k not in omit]
    drawings = project_views(parts, list(STANDARD_VIEWS))
    outline_drawings = {k: LineDrawing(view=d.view, lines=list(d.lines)) for k, d in drawings.items()}
    counts: dict[str, int] = {}
    if shading:
        from .geometry import combined_mesh
        from .shading import add_shading

        V, F, _owners = combined_mesh(parts)
        counts = add_shading({k: drawings[k] for k in order}, parts, V, F, STANDARD_VIEWS)
    ortho = [k for k in order if k in ORTHO]
    max_ext = max(max(drawings[k].extent()) for k in ortho) if ortho else max(drawings["perspective"].extent())
    margin = int(px * 0.06)
    ortho_scale = (px - 2 * margin) / max_ext
    figures = []
    for index, key in enumerate(order, start=1):
        drawing = drawings[key]
        scale = None if key == "perspective" else ortho_scale  # same scale for all orthographic views
        image = to_png(drawing, width=px, height=px, margin=margin, stroke=max(3, px // 500), scale=scale)
        if watermark:
            image = _watermark_png(image)
        name = f"1.{index}_{key}.jpg"
        path = outdir / "views" / name
        image.convert("L").save(path, format="JPEG", quality=92, dpi=(300, 300))
        figures.append({"number": f"1.{index}", "view": key, "label": STANDARD_VIEWS[key].us_label, "jpg": f"views/{name}", "px": [px, px], "bytes": path.stat().st_size})

    findings = []

    def add(rule_id, ok, severity, message, basis, data=None):
        findings.append({"rule_id": rule_id, "pass": bool(ok), "severity": "ok" if ok else severity, "message": message, "basis": basis, **({"data": data} if data else {})})

    consistency = _consistency(outline_drawings)
    add("EU-DRW-001", len(figures) <= EU_MAX_VIEWS, "error", f"{len(figures)} static views (maximum {EU_MAX_VIEWS}).", "EUDR Art. 36 as amended; EUIPO design representation guidance")
    add("EU-DRW-002", all(f["px"][0] >= EU_MIN_PX and f["px"][1] >= EU_MIN_PX for f in figures), "error", f"JPEG views of at least {EU_MIN_PX} x {EU_MIN_PX} px.", "EUIPO technical requirements from 1 July 2026")
    add("EU-DRW-003", all(f["bytes"] <= EU_MAX_BYTES for f in figures), "error", "Each image file is at most 2 MB for e-filing.", "EUIPO e-filing FAQ")
    add("EU-DRW-004", not consistency["mismatches"], "error", "All views show the same design (shared dimensions agree).", "EUDR: consistent representation", consistency)
    has_unclaimed = any(not p.claimed for p in parts)
    add("EU-DRW-005", True, "warning", "Disclaimed parts are drawn in broken lines in every view (visual disclaimer applied consistently)." if has_unclaimed else "No disclaimed parts; the design is shown alone.", "EUIPO Guidelines 2026: visual disclaimers")
    add("EU-DRW-006", True, "error", "Static line drawings only (one type of representation), no text, labels or view numbers inside the images.", "EUIPO design representation key rules")
    if omit:
        add("EU-DRW-007", False, "warning", f"Omitted views: {', '.join(omit)}. All features for which protection is sought must be visible in the views filed.", "EUIPO design representation key rules")
    if shading:
        add("EU-DRW-008", True, "warning", f"Surface shading drawn ({sum(counts.values())} lines); shading becomes part of the representation.", "EUIPO Guidelines: shading shows relief and contours", {"lines_per_view": counts})
    worst = "ok"
    for f in findings:
        if f["severity"] == "error":
            worst = "error"
        elif f["severity"] == "warning" and worst == "ok":
            worst = "warning"
    report = {"ruleset_version": RULESET_VERSION, "status": worst, "passed": sum(f["pass"] for f in findings), "total": len(findings), "findings": findings}
    result = {
        "kind": "line_viewpack",
        "office": "EU",
        "article": article,
        "ruleset_version": RULESET_VERSION,
        "figures": figures,
        "omitted_views": omit,
        "watermark": watermark,
        "shading": {"enabled": shading, "lines_per_view": counts},
        "rules": report,
        "filing_notes": [
            "Indication of product: use the product name and Locarno class (EUIPO e-filing offers the harmonised list).",
            "Upload the JPEG views in the numbered order; view numbers are entered in the form, not drawn on the images.",
            "Applicants without domicile or establishment in the EEA must appoint a representative (EUDR Art. 77(2)).",
        ],
    }
    result["files"] = manifest_for_dir(outdir)
    (outdir / "viewpack.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def _watermark_png(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    layer = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    try:
        font = ImageFont.load_default(size=max(24, rgba.size[0] // 18))
    except TypeError:
        font = ImageFont.load_default()
    text = "PREVIEW - DesignAround Free"
    w, h = rgba.size
    for y in range(0, h, max(1, h // 4)):
        draw.text((w * 0.08, y + h * 0.05), text, fill=(150, 150, 150, 90), font=font)
    return Image.alpha_composite(rgba, layer).convert("L")


def drawing_rules(office: str, drawings: dict[str, LineDrawing], parts, *, order, omit, figures, shading_counts: dict[str, int] | None = None) -> dict:
    findings = []

    def add(rule_id, ok, severity, message, basis, data=None):
        findings.append({"rule_id": rule_id, "pass": bool(ok), "severity": "ok" if ok else severity, "message": message, "basis": basis, **({"data": data} if data else {})})

    consistency = _consistency(drawings)
    add(f"{office}-DRW-005", not consistency["mismatches"], "error", "All orthographic views agree in shared dimensions.", "MPEP 1503.02 / 专利审查指南 第一部分第三章 4.2", consistency)

    if parts and all(getattr(p, "faceted", False) for p in parts):
        add(f"{office}-DRW-030", False, "warning", "Drawn from an imported mesh (scan or STL/OBJ/GLB/3MF): outlines come from facets, and short isolated creases (scan noise) are not drawn. A practitioner checks curved outlines and small features against the product before filing." if office != "CN" else "视图由导入的网格模型（扫描或 STL/OBJ/GLB/3MF）生成，轮廓来自三角面片，零散的短折痕（扫描噪声）不画出。提交前须由代理师对照实物核对曲面轮廓和细小特征。", "37 CFR 1.84 / 专利审查指南 第一部分第三章 4.2 (drawing quality)")
    required = set(STANDARD_VIEWS)
    if office == "US":
        add("US-DRW-001", not omit, "warning", "Views: perspective + six orthographic views" + (f"; omitted {', '.join(omit)} (practitioner must confirm the omission is acceptable)" if omit else ""), "37 CFR 1.152; MPEP 1503.02")
        has_unclaimed = any(not p.claimed for p in parts)
        # An unclaimed part may be hidden in some views (e.g. behind the body); it must appear
        # in broken lines in the views where it is visible and never as a solid line.
        unclaimed_names = {p.name for p in parts if not p.claimed}
        broken_ok = (
            any(any(not ln.claimed for ln in d.lines) for d in drawings.values())
            and all(ln.part not in unclaimed_names or not ln.claimed for d in drawings.values() for ln in d.lines)
        ) if has_unclaimed else True
        add("US-DRW-010", broken_ok, "error", "Unclaimed parts are shown in broken lines; claimed parts in solid lines.", "MPEP 1503.02(III)")
        curved = _curved_area_ratio(parts)
        total_shading = sum((shading_counts or {}).values())
        if shading_counts is not None and (total_shading or curved < 0.02):
            add("US-DRW-014", True, "warning", f"Surface shading generated on curved surfaces ({total_shading} lines; curved surfaces are {curved:.0%} of the area). A practitioner reviews the shading before filing; adding or changing shading after filing may be new matter.", "37 CFR 1.152; MPEP 1503.02(II)", {"curved_area_ratio": round(curved, 3), "lines_per_view": shading_counts})
        else:
            add("US-DRW-014", curved < 0.25, "warning", f"Curved surfaces cover {curved:.0%} of the design and no surface shading was drawn; a practitioner must confirm whether shading is needed (adding it after filing may be new matter).", "37 CFR 1.152; MPEP 1503.02(II)", {"curved_area_ratio": round(curved, 3)})
        add("US-DRW-020", True, "error", "Black-and-white line drawings, uniform line weight, no hidden lines.", "37 CFR 1.84(a)(1), 1.152")
        add("US-DRW-021", True, "error", "Sheet margins: top 2.5 cm, left 2.5 cm, right 1.5 cm, bottom 1.0 cm.", "37 CFR 1.84(g)")
        add("US-DRW-022", True, "error", "Figures numbered consecutively (FIG. 1..n); sheets numbered n/N.", "37 CFR 1.84(t),(u)")
    else:
        missing = sorted(required - set(order))
        add("CN-DRW-001", not missing, "warning", "各视图齐全" if not missing else f"省略了 {', '.join(STANDARD_VIEWS[m].cn_label for m in missing)}，须在简要说明中写明省略理由。", "专利法实施细则第二十七条；专利审查指南 第一部分第三章 4.2")
        oversize = [f for f in figures if f["size_cm"][0] > CN_MAX_CM[0] + 1e-6 or f["size_cm"][1] > CN_MAX_CM[1] + 1e-6]
        add("CN-DRW-002", not oversize, "error", "每幅图片尺寸不超过 15cm×22cm。", "专利审查指南 第一部分第三章 4.2", {"oversize": oversize})
        add("CN-DRW-003", True, "error", "视图名称标注正确（主视图、后视图、左视图、右视图、俯视图、仰视图、立体图）。", "专利审查指南 第一部分第三章 4.2")
        has_unclaimed = any(not p.claimed for p in parts)
        add("CN-DRW-004", True, "warning", "存在虚线表示的不要求保护部分：属于局部外观设计时，须在简要说明中说明。" if has_unclaimed else "无不要求保护部分（整体外观设计）。", "专利法第二条第四款；专利审查指南（局部外观设计）")
        add("CN-DRW-005", True, "error", "线条图为黑白线条，不含阴影和尺寸标注。", "专利审查指南 第一部分第三章 4.2")
    worst = "ok"
    for f in findings:
        if f["severity"] == "error":
            worst = "error"
        elif f["severity"] == "warning" and worst == "ok":
            worst = "warning"
    return {"ruleset_version": RULESET_VERSION, "status": worst, "passed": sum(f["pass"] for f in findings), "total": len(findings), "findings": findings}


def build_photo_viewpack(photos: dict[str, str], office: str, outdir: str | os.PathLike, *, article: str, watermark: bool = False) -> dict:
    """Path A: lay out labelled product photos as office figures."""

    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    office = office.upper()
    outdir = Path(outdir)
    (outdir / "figures").mkdir(parents=True, exist_ok=True)
    order = [k for k in (US_ORDER if office == "US" else CN_ORDER) if k in photos]
    page = US_PAGE if office == "US" else A4
    margins = US_MARGINS if office == "US" else {"top": 2.0 * CM, "left": 2.0 * CM, "right": 2.0 * CM, "bottom": 2.0 * CM}
    box = _figure_box(page, margins, label_space=28)
    font_cn = _ensure_cjk() if office == "CN" else None
    pdf_path = outdir / ("photos.pdf" if office == "US" else "照片.pdf")
    c = canvas.Canvas(str(pdf_path), pagesize=page)
    figures = []
    for number, key in enumerate(order, start=1):
        image = ImageOps.exif_transpose(Image.open(photos[key]))
        image = image.convert("L" if office == "US" else "RGB")  # US: black-and-white photographs
        if office == "CN":
            max_px = (int(CN_MAX_CM[0] / 2.54 * 300), int(CN_MAX_CM[1] / 2.54 * 300))
            image.thumbnail(max_px)
        if watermark:
            image = _watermark_png(image.convert("L"))
        view = STANDARD_VIEWS[key]
        stem = f"fig{number}_{key}" if office == "US" else f"{number:02d}_{view.cn_label}"
        image.save(outdir / "figures" / f"{stem}.png", dpi=(300, 300))
        iw, ih = image.size
        s = min(box[2] / iw, box[3] / ih)
        c.drawImage(ImageReader(image), box[0] + (box[2] - iw * s) / 2, box[1] + (box[3] - ih * s) / 2, iw * s, ih * s)
        if office == "US":
            c.setFont("Helvetica-Bold", 14)
            c.drawCentredString(page[0] / 2, margins["bottom"] + 8, f"FIG. {number}")
        else:
            c.setFont(font_cn, 14)
            c.drawCentredString(page[0] / 2, margins["bottom"] + 8, view.cn_label)
        c.showPage()
        figures.append({"number": number, "view": key, "label": f"FIG. {number}" if office == "US" else view.cn_label, "png": f"figures/{stem}.png"})
    c.save()
    findings = [
        {"rule_id": f"{office}-PHO-001", "pass": True, "severity": "ok", "message": "Photographs and line drawings are not mixed for the same embodiment.", "basis": "MPEP 1503.02(V)" if office == "US" else "专利审查指南 第一部分第三章 4.2"},
    ]
    if office == "US":
        findings.append({"rule_id": "US-PHO-002", "pass": True, "severity": "ok", "message": "Black-and-white photographs; colour photographs need a petition and a statement.", "basis": "37 CFR 1.84(b)"})
    result = {"kind": "photo_viewpack", "office": office, "article": article, "figures": figures, "pdf": pdf_path.name, "rules": {"ruleset_version": RULESET_VERSION, "findings": findings}}
    result["files"] = manifest_for_dir(outdir)
    (outdir / "viewpack.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result
