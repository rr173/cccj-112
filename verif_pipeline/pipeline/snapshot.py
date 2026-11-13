"""Content-addressed snapshot identifiers.

The snapshot id is a hash over the *frozen* analysis inputs: the
analysis configuration, the manifest, the station table, the selected
forecast files (content hashes), and the selected observation versions
(their content, not the raw file). Identical frozen inputs therefore
always produce the same id, and observation corrections issued after
the obs cutoff cannot change an already frozen analysis.
"""
from __future__ import annotations

import hashlib
import json


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot_id(payload: dict) -> str:
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256_text(canon)[:16]
