"""Local case workspace.

Layout under ``$DESIGNAROUND_HOME/cases/<case_id>/``::

    case.json                 metadata, constraints, applicant, server link
    events.jsonl              hash-chained event log (evidence)
    versions/<vid>/           one design version (see cad.py) + version.json
    prior_art/<pa_id>/        meta.json + figs/*.png (prior-art figures)
    evaluations/<vid>.json    risk evaluation of a version against prior art
    disclosures.json          public disclosure timeline (FR-FILE-11)
    filings/<office>/<vid>/   viewpacks and filing packages
    runs/<run_id>/            agent run logs (NDJSON)
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path

from .errors import InvalidInput, NotFound
from .evidence import EventLog


def home() -> Path:
    root = Path(os.environ.get("DESIGNAROUND_HOME") or Path.home() / ".designaround")
    root.mkdir(parents=True, exist_ok=True)
    return root


def cases_root() -> Path:
    path = home() / "cases"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text[:24] or "case"


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default=None):
    if not path.exists():
        if default is not None:
            return default
        raise NotFound(f"{path.name} not found")
    return json.loads(path.read_text(encoding="utf-8"))


class Case:
    def __init__(self, root: Path) -> None:
        self.root = root
        if not (root / "case.json").exists():
            raise NotFound(f"Case not found: {root.name}")

    # ------------------------------------------------------------- basics
    @property
    def id(self) -> str:
        return self.root.name

    @property
    def meta(self) -> dict:
        return _read_json(self.root / "case.json")

    def update(self, **fields) -> dict:
        meta = self.meta
        meta.update(fields)
        meta["updated"] = time.time()
        _write_json(self.root / "case.json", meta)
        return meta

    @property
    def log(self) -> EventLog:
        return EventLog(self.root / "events.jsonl")

    def event(self, kind: str, payload: dict) -> dict:
        return self.log.append(kind, payload)

    # ----------------------------------------------------------- versions
    def versions_dir(self) -> Path:
        path = self.root / "versions"
        path.mkdir(exist_ok=True)
        return path

    def new_version_id(self) -> str:
        existing = [p.name for p in self.versions_dir().iterdir() if p.is_dir() and re.fullmatch(r"v\d+", p.name)]
        numbers = [int(name[1:]) for name in existing]
        return f"v{(max(numbers) + 1) if numbers else 1}"

    def version_path(self, vid: str) -> Path:
        path = self.versions_dir() / vid
        if not path.exists():
            raise NotFound(f"Version {vid} not found in case {self.id}", details={"versions": self.version_ids()})
        return path

    def version_ids(self) -> list[str]:
        ids = [p.name for p in self.versions_dir().iterdir() if (p / "version.json").exists()]
        return sorted(ids, key=lambda s: int(s[1:]) if s[1:].isdigit() else 10**9)

    def version_meta(self, vid: str) -> dict:
        return _read_json(self.version_path(vid) / "version.json")

    def write_version_meta(self, vid: str, meta: dict) -> None:
        _write_json(self.versions_dir() / vid / "version.json", meta)

    def versions(self) -> list[dict]:
        return [self.version_meta(v) for v in self.version_ids()]

    def original_version(self) -> str | None:
        # A design built from a chosen concept replaces an earlier placeholder original.
        pointer = self.meta.get("original")
        if pointer and (self.versions_dir() / pointer).exists():
            return pointer
        metas = self.versions()
        for meta in metas:
            if meta.get("kind") == "original":
                return meta["id"]
        # A draft (a 3D build the reviewer did not accept) is never the baseline.
        ids = [m["id"] for m in metas if m.get("kind") != "draft"]
        return ids[0] if ids else None

    # ---------------------------------------------------------- prior art
    def prior_art_dir(self) -> Path:
        path = self.root / "prior_art"
        path.mkdir(exist_ok=True)
        return path

    def prior_art(self) -> list[dict]:
        rows = []
        for path in sorted(self.prior_art_dir().iterdir()):
            if (path / "meta.json").exists():
                meta = _read_json(path / "meta.json")
                meta["figures"] = [str(p.relative_to(self.root)) for p in sorted((path / "figs").glob("*")) if p.is_file()]
                rows.append(meta)
        return rows

    def prior_art_path(self, pa_id: str) -> Path:
        path = self.prior_art_dir() / pa_id
        if not (path / "meta.json").exists():
            raise NotFound(f"Prior art {pa_id} not found", details={"prior_art": [p["id"] for p in self.prior_art()]})
        return path

    def add_prior_art(self, meta: dict, figures: list[tuple[str, bytes]]) -> dict:
        pa_id = re.sub(r"[^A-Za-z0-9_.-]", "_", meta.get("id") or f"pa{secrets.token_hex(3)}")
        path = self.prior_art_dir() / pa_id
        (path / "figs").mkdir(parents=True, exist_ok=True)
        for name, data in figures:
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
            (path / "figs" / safe).write_bytes(data)
        meta = {**meta, "id": pa_id, "added": time.time()}
        _write_json(path / "meta.json", meta)
        return meta

    # -------------------------------------------------------------- misc
    def evaluation_path(self, vid: str) -> Path:
        return self.root / "evaluations" / f"{vid}.json"

    def disclosures(self) -> list[dict]:
        return _read_json(self.root / "disclosures.json", default=[])

    def set_disclosures(self, rows: list[dict]) -> None:
        _write_json(self.root / "disclosures.json", rows)

    def filing_dir(self, office: str, vid: str) -> Path:
        path = self.root / "filings" / office.upper() / vid
        path.mkdir(parents=True, exist_ok=True)
        return path

    def run_dir(self, run_id: str) -> Path:
        path = self.root / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def summary(self) -> dict:
        meta = self.meta
        return {
            **meta,
            "root": str(self.root),
            "versions": [
                {k: v for k, v in m.items() if k in ("id", "kind", "label", "parent", "created_by", "created", "risk", "qualified", "concept", "review")}
                for m in self.versions()
            ],
            "original": self.original_version(),
            "prior_art": [{k: p.get(k) for k in ("id", "office", "number", "title", "status", "source")} for p in self.prior_art()],
        }


def create_case(
    name: str,
    *,
    article: str | None = None,
    article_zh: str | None = None,
    category: str | None = None,
    jurisdictions: list[str] | None = None,
    platforms: list[str] | None = None,
) -> Case:
    if not name or not name.strip():
        raise InvalidInput("Case name is required.")
    case_id = f"{_slug(name)}-{secrets.token_hex(3)}"
    root = cases_root() / case_id
    root.mkdir(parents=True)
    meta = {
        "id": case_id,
        "name": name.strip(),
        "article": article or name.strip(),
        "article_zh": article_zh,
        "category": category,
        "jurisdictions": [j.upper() for j in (jurisdictions or ["US"])],
        "platforms": platforms or ["amazon"],
        "constraints": {},
        "applicant": {},
        "created": time.time(),
        "status": "open",
    }
    _write_json(root / "case.json", meta)
    case = Case(root)
    case.event("case.created", {"name": meta["name"], "jurisdictions": meta["jurisdictions"]})
    return case


def open_case(case_id: str) -> Case:
    root = cases_root() / case_id
    if not root.exists():
        # allow unique prefix
        matches = [p for p in cases_root().iterdir() if p.name.startswith(case_id)]
        if len(matches) == 1:
            root = matches[0]
    return Case(root)


def _thumb(case: Case, meta: dict) -> str | None:
    """Preview for the case list: the original design, else the chosen or newest concept."""

    original = case.original_version()
    if original:
        for kind in ("render", "lineart"):
            path = case.version_path(original) / "views" / f"perspective.{kind}.png"
            if path.exists():
                return str(path)
    concepts_dir = case.root / "concepts"
    chosen = meta.get("chosen_concept")
    if chosen and (concepts_dir / chosen / "image.png").exists():
        return str(concepts_dir / chosen / "image.png")
    if concepts_dir.is_dir():
        images = sorted(concepts_dir.glob("c*/image.png"), key=lambda p: p.stat().st_mtime)
        if images:
            return str(images[-1])
    return None


def list_cases() -> list[dict]:
    rows = []
    for path in sorted(cases_root().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if (path / "case.json").exists():
            meta = _read_json(path / "case.json")
            row = {k: meta.get(k) for k in ("id", "name", "article", "jurisdictions", "status", "created")}
            row["root"] = str(path)
            versions = path / "versions"
            row["versions"] = sum(1 for v in versions.iterdir() if (v / "version.json").exists()) if versions.is_dir() else 0
            row["thumb"] = _thumb(Case(path), meta)
            rows.append(row)
    return rows


def delete_case(case_id: str) -> None:
    case = open_case(case_id)
    shutil.rmtree(case.root)
