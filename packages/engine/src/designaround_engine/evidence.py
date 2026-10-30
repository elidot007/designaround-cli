"""Hashes and an append-only, hash-chained local event log.

The server keeps the authoritative chain (FR-CASE-04). Locally, every engine
output is hashed so results computed on the user's machine can be anchored on
the server (sha256 + timestamp) without uploading confidential files.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

GENESIS = "0" * 64


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_for_dir(root: str | os.PathLike, *, exclude: tuple[str, ...] = ("events.jsonl",)) -> list[dict]:
    root = Path(root)
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in exclude:
            rows.append({"path": str(path.relative_to(root)), "sha256": sha256_file(path), "bytes": path.stat().st_size})
    return rows


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def chain_hash(prev_hash: str, payload: dict) -> str:
    return sha256_bytes(prev_hash.encode("ascii") + canonical(payload))


class EventLog:
    """events.jsonl where each line carries prev_hash and hash."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)

    def _last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = GENESIS
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    last = json.loads(line)["hash"]
        return last

    def append(self, event_type: str, payload: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        prev = self._last_hash()
        body = {"type": event_type, "ts": round(time.time(), 3), "payload": payload}
        record = {**body, "prev_hash": prev, "hash": chain_hash(prev, body)}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def verify(self) -> tuple[bool, int | None]:
        """Return (ok, index_of_first_bad_record)."""

        prev = GENESIS
        for index, record in enumerate(self.read()):
            body = {k: record[k] for k in ("type", "ts", "payload")}
            if record.get("prev_hash") != prev or record.get("hash") != chain_hash(prev, body):
                return False, index
            prev = record["hash"]
        return True, None
