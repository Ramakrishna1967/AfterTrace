"""Immutable staging index creation (spec p6)."""
from __future__ import annotations

import hashlib
import math
import re

import httpx


def collection_name(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,120}", value):
        raise ValueError("invalid server-generated collection name")
    return value


async def create_staging(q: httpx.AsyncClient, name: str, dim: int, points: list[dict]) -> str:
    name = collection_name(name)
    r = await q.put(f"/collections/{name}", json={"vectors": {"size": dim, "distance": "Cosine"}})
    r.raise_for_status()
    # Call only for a previously unused, journaled build identity.
    for start in range(0, len(points), 100):
        r = await q.put(
            f"/collections/{name}/points",
            params={"wait": "true", "ordering": "strong"},
            json={"points": points[start:start + 100]},
        )
        r.raise_for_status()
        if r.json()["result"]["status"] != "completed":
            raise RuntimeError("ingestion not observed complete")
    return name  # Not sealed or publishable until verification passes.


def deterministic_vector(text: str, dim: int) -> list[float]:
    """Repeatable finite vector from text hash for transport/integrity fixtures.

    NOT semantic retrieval quality — isolates routing/freshness behavior.
    """
    h = hashlib.sha256(text.encode("utf-8")).digest()
    vals: list[float] = []
    counter = 0
    while len(vals) < dim:
        block = hashlib.sha256(h + counter.to_bytes(4, "little")).digest()
        for i in range(0, len(block), 4):
            if len(vals) >= dim:
                break
            v = int.from_bytes(block[i:i + 4], "little") / 2**32 - 0.5
            vals.append(v)
        counter += 1
    norm = math.sqrt(sum(v * v for v in vals)) or 1.0
    return [v / norm for v in vals]


def build_point(point_id: str, project_id: str, corpus_id: str, document_id: str,
                chunk_id: str, revision: str, text: str, dim: int) -> dict:
    content_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "id": point_id,
        "vector": deterministic_vector(text, dim),
        "payload": {
            "project_id": project_id,
            "corpus_id": corpus_id,
            "document_id": document_id,
            "chunk_id": chunk_id,
            "revision": revision,
            "content_sha256": content_sha,
            "text": text,
        },
    }
