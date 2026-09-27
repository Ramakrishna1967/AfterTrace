"""Deterministic hash-based FIXTURE vectors. NOT real embeddings.

Label: transport/integrity fixture only. Used to isolate routing/freshness
without an embedding API. Must never be presented as semantic quality.
"""

from __future__ import annotations

import hashlib
import math
import struct

from .config import VECTOR_DIM


def fixture_vector(text: str, dim: int = VECTOR_DIM) -> list[float]:
    """Derive a repeatable finite unit vector from text hash."""
    out: list[float] = []
    counter = 0
    while len(out) < dim:
        h = hashlib.sha256(f"{text}#{counter}".encode()).digest()
        # 8 floats per 32-byte hash (4 bytes each)
        for i in range(0, 32, 4):
            if len(out) >= dim:
                break
            (u,) = struct.unpack(">I", h[i : i + 4])
            # map to [-1, 1]
            out.append((u / 4294967295.0) * 2.0 - 1.0)
        counter += 1
    norm = math.sqrt(sum(x * x for x in out))
    if norm == 0 or not math.isfinite(norm):
        raise ValueError("non-finite fixture vector")
    return [x / norm for x in out]


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))
