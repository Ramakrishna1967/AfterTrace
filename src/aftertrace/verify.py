"""Verification — exact index checks + serving probes (spec p10-11)."""

from __future__ import annotations

import hashlib
import math


async def scan_collection(q, name: str, max_points: int = 10000) -> dict:
    offset, seen_offsets, points = None, set(), {}
    while True:
        body: dict = {"limit": 100, "with_payload": True, "with_vector": True}
        if offset is not None:
            body["offset"] = offset
        r = await q.post(f"/collections/{name}/points/scroll", json=body)
        r.raise_for_status()
        result = r.json()["result"]
        for point in result["points"]:
            key = str(point["id"])
            if key in points:
                raise ValueError("duplicate point observed during scan")
            points[key] = point
            if len(points) > max_points:
                raise ValueError("verification scan budget exceeded")
        offset = result.get("next_page_offset")
        if offset is None:
            return points
        marker = str(offset)
        if marker in seen_offsets:
            raise ValueError("non-progressing scroll cursor")
        seen_offsets.add(marker)


def compare_index(manifest, points: dict) -> list[str]:
    expected = {c.point_id: c for c in manifest.chunks}
    errors: list[str] = []
    if set(expected) != set(points):
        errors.append("point identity set differs")
    for key in set(expected) & set(points):
        spec, point = expected[key], points[key]
        payload, vector = point.get("payload", {}), point.get("vector")
        fields = {
            "project_id": manifest.project_id,
            "corpus_id": manifest.corpus_id,
            "document_id": spec.document_id,
            "chunk_id": spec.chunk_id,
            "revision": spec.revision,
            "content_sha256": spec.content_sha256,
        }
        if any(payload.get(k) != v for k, v in fields.items()):
            errors.append(f"{key}: payload mismatch")
        text = payload.get("text")
        if (
            not isinstance(text, str)
            or hashlib.sha256(text.encode()).hexdigest() != spec.content_sha256
        ):
            errors.append(f"{key}: content bytes mismatch")
        if (
            not isinstance(vector, list)
            or len(vector) != manifest.vector_size
            or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vector)
        ):
            errors.append(f"{key}: invalid vector")
    return errors


def check_canary(response: dict, canary, route: dict) -> tuple[bool, str]:
    required_route = ("collection_name", "generation", "cache_epoch")
    for key in required_route:
        if response.get("route", {}).get(key) != route[key]:
            return False, f"routing mismatch: {key}"
    hits = response.get("hits", [])
    found = [p for p in hits if str(p.get("point_id")) == canary.expected_point_id]
    if not found:
        return False, "expected point absent from top-k"
    hit = found[0]
    if hit.get("revision") != canary.expected_revision:
        return False, "wrong document revision"
    if hit.get("content_sha256") != canary.expected_content_sha256:
        return False, "wrong normalized content hash"
    text = hit.get("text")
    if (
        not isinstance(text, str)
        or hashlib.sha256(text.encode()).hexdigest() != canary.expected_content_sha256
    ):
        return False, "returned content bytes differ"
    return True, "expected point, revision and route observed"


async def probe_twice(gateway, canary, route: dict, principal: dict) -> dict:
    # Use the same query/cache/filter path as normal traffic.
    query = {
        "query_vector": list(canary.query_vector),
        "top_k": canary.top_k,
        "filters": {},
        "query_id": canary.query_id,
    }
    first = await gateway.query(query, principal, verifier=True)
    second = await gateway.query(query, principal, verifier=True)
    checks = [check_canary(first, canary, route), check_canary(second, canary, route)]
    return {
        "passed": all(ok for ok, _ in checks),
        "checks": checks,
        "cache_hits": [first.get("cache_hit"), second.get("cache_hit")],
        "route": route,
    }
