"""Visible watermarks for shared copies of case files (FR-CASE-09).

Images get a tiled diagonal mark plus a footer strip; PDFs get the same text stamped
on every page. The originals in the case are never touched: marked copies are written
to a separate folder. 3D files (STEP/STL/3MF/GLB) cannot carry a visible mark and are
copied as they are.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path

# CJK-capable fonts that ship with the OS (Pillow needs a TrueType file for Chinese).
CJK_FONTS = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc",
]
IMAGE = {".png", ".jpg", ".jpeg", ".webp"}


def _font(size: int):
    from PIL import ImageFont

    for path in CJK_FONTS:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size), True
            except OSError:
                continue
    try:
        return ImageFont.load_default(size), False
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default(), False


def mark_image(src: Path, dst: Path, text: str, ascii_text: str) -> None:
    from PIL import Image, ImageDraw

    with Image.open(src) as im:
        base = im.convert("RGBA")
    w, h = base.size
    size = max(14, min(w, h) // 30)
    font, cjk = _font(size)
    label = text if cjk else ascii_text
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    left, top, right, bottom = probe.textbbox((0, 0), label, font=font)
    tile = Image.new("RGBA", (right - left + size * 5, bottom - top + size * 5), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((size * 2 - left, size * 2 - top), label, font=font, fill=(190, 30, 30, 70))
    tile = tile.rotate(28, expand=True, resample=Image.Resampling.BICUBIC)
    big = Image.new("RGBA", (w + 2 * tile.width, h + 2 * tile.height), (0, 0, 0, 0))
    for row, y in enumerate(range(0, big.height - tile.height + 1, tile.height)):
        for x in range((tile.width // 2) if row % 2 else 0, big.width - tile.width + 1, tile.width):
            big.alpha_composite(tile, (x, y))
    layer = big.crop((tile.width, tile.height, tile.width + w, tile.height + h))
    # Footer strip that stays readable on any background; the text shrinks to fit the width.
    foot_size = size
    while foot_size > 9:
        foot_font, _ = _font(foot_size)
        fl, ft, fr, fb = probe.textbbox((0, 0), label, font=foot_font)
        if fr - fl <= w - foot_size:
            break
        foot_size = int(foot_size * 0.85)
    foot_font, _ = _font(foot_size)
    fl, ft, fr, fb = probe.textbbox((0, 0), label, font=foot_font)
    strip_h = int((fb - ft) * 1.8) + 4
    draw = ImageDraw.Draw(layer)
    draw.rectangle((0, h - strip_h, w, h), fill=(255, 255, 255, 200))
    draw.text((foot_size // 2 - fl, h - strip_h + (strip_h - (fb - ft)) // 2 - ft), label, font=foot_font, fill=(150, 20, 20, 255))
    out = Image.alpha_composite(base, layer)
    dst.parent.mkdir(parents=True, exist_ok=True)
    suffix = dst.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        out.convert("RGB").save(dst, quality=92)
    elif suffix == ".webp":
        out.save(dst, quality=92)
    else:
        out.save(dst)


def mark_pdf(src: Path, dst: Path, text: str) -> None:
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    from .viewpack import _ensure_cjk

    font = _ensure_cjk()
    step_x = stringWidth(text, font, 13) + 70  # one copy per slot, with a gap: never overlapping
    step_y = 84
    reader = PdfReader(str(src))
    writer = PdfWriter()
    overlays: dict[tuple[int, int], object] = {}
    for page in reader.pages:
        box = page.mediabox
        w, h = float(box.width), float(box.height)
        key = (round(w), round(h))
        if key not in overlays:
            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(w, h))
            c.setFillColorRGB(0.75, 0.12, 0.12)
            c.setFillAlpha(0.16)
            c.setFont(font, 13)
            c.saveState()
            c.translate(w / 2, h / 2)
            c.rotate(30)
            span = max(w, h)
            row = 0
            y = -span
            while y <= span:
                x = -span + (step_x / 2 if row % 2 else 0)
                while x <= span:
                    c.drawCentredString(x, y, text)
                    x += step_x
                y += step_y
                row += 1
            c.restoreState()
            c.setFillAlpha(0.85)
            foot = 8
            while foot > 5 and stringWidth(text, font, foot) > w - 36:
                foot -= 0.5
            c.setFont(font, foot)
            c.drawString(18, 10, text)
            c.save()
            overlays[key] = PdfReader(io.BytesIO(buf.getvalue())).pages[0]
        page.merge_translated_page(overlays[key], float(box.left), float(box.bottom))
        writer.add_page(page)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as handle:
        writer.write(handle)


def mark_copy(src: Path, dst: Path, text: str, ascii_text: str) -> bool:
    """Write a marked copy of ``src`` to ``dst``; returns whether a mark was applied."""

    suffix = src.suffix.lower()
    if suffix in IMAGE:
        mark_image(src, dst, text, ascii_text)
        return True
    if suffix == ".pdf":
        mark_pdf(src, dst, text)
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return False
