"""Prior-art figures.

* Inline ``data:image/...;base64`` thumbnails from ClearComply (small; good
  enough for a first ranking).
* Full-size US design drawing sheets from the USPTO Patent Public Search API
  (ppubs.uspto.gov, anonymous public session), split into single figures.
  Requests are sequential and spaced out; only the top candidates are fetched.
"""

from __future__ import annotations

import base64
import binascii
import io
import re
import time
from urllib.parse import urlencode

import httpx
import numpy as np
from PIL import Image

MAX_FIGURE_BYTES = 6 * 1024 * 1024
PPUBS = "https://ppubs.uspto.gov"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36 designaround-cli"
_DATA_URI = re.compile(r"^data:image/(png|jpeg|jpg|gif|webp);base64,(.+)$", re.I | re.S)


def valid_image(data: bytes) -> bool:
    try:
        Image.open(io.BytesIO(data)).verify()
        return True
    except Exception:  # noqa: BLE001
        return False


def decode_data_uri(uri: str) -> tuple[bytes, str] | None:
    match = _DATA_URI.match(uri.strip())
    if not match:
        return None
    try:
        data = base64.b64decode(match.group(2), validate=False)
    except (binascii.Error, ValueError):
        return None
    if len(data) > MAX_FIGURE_BYTES or not valid_image(data):
        return None
    ext = {"jpeg": ".jpg", "jpg": ".jpg"}.get(match.group(1).lower(), "." + match.group(1).lower())
    return data, ext


def split_figures(sheet: bytes, *, max_figures: int = 4) -> list[Image.Image]:
    """Crop the drawings on one sheet; drop headers, FIG. labels and sheet numbers."""

    from scipy import ndimage

    image = Image.open(io.BytesIO(sheet)).convert("L")
    ink = np.asarray(image) < 128
    step = max(1, min(ink.shape) // 700)
    small = ink[::step, ::step]
    lab, count = ndimage.label(ndimage.binary_dilation(small, iterations=max(4, 48 // step)))
    if not count:
        return []
    sizes = ndimage.sum(small, lab, index=range(1, count + 1))
    biggest = float(max(sizes))
    out = []
    for index, sl in enumerate(ndimage.find_objects(lab), start=1):
        if sl is None or sizes[index - 1] < 0.15 * biggest:
            continue
        h, w = (sl[0].stop - sl[0].start) * step, (sl[1].stop - sl[1].start) * step
        if h < 0.06 * ink.shape[0] and w < 0.06 * ink.shape[1]:
            continue
        top, left = sl[0].start * step, sl[1].start * step
        pad = 12
        box = (max(0, left - pad), max(0, top - pad), min(image.width, left + w + pad), min(image.height, top + h + pad))
        out.append((sizes[index - 1], box))
    out.sort(key=lambda r: (r[1][1], r[1][0]))
    crops = [lighten_hatching(image.crop(box), (1400, 1400)) for _, box in out[:max_figures]]
    return crops


def lighten_hatching(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Make dark design figures readable for people and vision models.

    Some registered designs are drawn with large solid-black or near-solid
    shaded areas. Large solid regions are turned into light grey with their
    boundary kept black, so contours, ribs and panel lines stay visible.
    Thin lines and normal hatching are unchanged. Then area-downscale.
    """

    from scipy import ndimage

    gray = image.convert("L")
    ink = np.asarray(gray) < 128
    if ink.mean() > 0.25:
        # dense areas: solid fill or hatching too fine to read (>= 55 % ink in a 9x9 window)
        density = ndimage.uniform_filter(ink.astype(np.float32), size=9)
        dense = ndimage.binary_opening(density > 0.55, iterations=2)
        edge = dense & ~ndimage.binary_erosion(dense, iterations=2)
        out = np.where(ink, 0, 255).astype(np.uint8)
        out[dense] = 200
        out[edge] = 0
        gray = Image.fromarray(out, "L")
    gray.thumbnail(size, Image.Resampling.LANCZOS)
    return gray


class UsptoFigures:
    """Full-size drawing sheets of granted US designs via the public PPUBS API."""

    def __init__(self, *, delay: float = 0.7, timeout: float = 30.0) -> None:
        self.delay = delay
        self.client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Origin": PPUBS, "Referer": f"{PPUBS}/pubwebapp/"})
        self.token: str | None = None

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _wait(self) -> None:
        time.sleep(self.delay)

    def _session(self) -> str:
        if self.token:
            return self.token
        self.client.get(f"{PPUBS}/pubwebapp/")
        response = self.client.post(f"{PPUBS}/api/users/me/session", content="-1", headers={"Content-Type": "application/json"})
        token = response.headers.get("x-access-token")
        if response.status_code != 200 or not token:
            raise RuntimeError(f"PPUBS session failed ({response.status_code})")
        self.token = token
        return token

    def sheets(self, number: str, *, max_sheets: int = 4) -> list[bytes]:
        digits = re.sub(r"\D", "", number)
        if not digits:
            return []
        token = self._session()
        headers = {"x-access-token": token}
        payload = {"cursorMarker": "*", "databaseFilters": [{"databaseName": "USPAT"}], "fields": ["documentId", "patentNumber", "type"], "op": "", "pageSize": 3, "q": f"(D{digits}).pn.", "searchType": 0, "sort": "score desc"}
        response = self.client.post(f"{PPUBS}/api/searches/generic", json=payload, headers={**headers, "Content-Type": "application/json"})
        if response.status_code != 200:
            return []
        docs = response.json().get("patents") or response.json().get("docs") or []
        doc = next((d for d in docs if re.sub(r"\D", "", str(d.get("patentNumber") or "")) == digits), None)
        if doc is None:
            return []
        self._wait()
        meta = self.client.get(f"{PPUBS}/api/patents/metadata/{doc['documentId']}", params={"source": doc.get("type") or "USPAT", "requestToken": token}, headers=headers)
        if meta.status_code != 200:
            return []
        m = meta.json()
        location, filename = m.get("imageLocation"), m.get("imageFileName")
        start, end = int(m.get("drawingsStart") or 0), int(m.get("drawingsEnd") or 0)
        if not (location and filename and start):
            return []
        match = re.match(r"^(.*?)(\d+)(\.[A-Za-z0-9]+)$", filename)
        out = []
        for page in range(start, max(start, end) + 1)[:max_sheets]:
            name = f"{match.group(1)}{str(page).zfill(len(match.group(2)))}{match.group(3)}" if match else filename
            self._wait()
            image = self.client.get(f"{PPUBS}/api/image/convert?" + urlencode({"url": f"{location}/{name}", "requestToken": token}), headers=headers)
            if image.status_code == 200 and len(image.content) <= MAX_FIGURE_BYTES and valid_image(image.content):
                out.append(image.content)
        return out


def us_design_number(number: str | None) -> bool:
    return bool(number) and bool(re.match(r"^(?:US\s?)?D\s?\d", str(number).strip(), re.I))
