"""Narrow Qdrant HTTP adapter (spec p20)."""
from __future__ import annotations

import httpx


class OutcomeUnknown(RuntimeError):
    pass


async def resolve_alias(q: httpx.AsyncClient, alias: str) -> str:
    r = await q.get("/aliases")
    r.raise_for_status()
    matches = [
        x["collection_name"]
        for x in r.json()["result"]["aliases"]
        if x["alias_name"] == alias
    ]
    if len(matches) != 1:
        raise ValueError("alias missing or ambiguous")
    return matches[0]


async def switch_alias(q: httpx.AsyncClient, alias: str, expected_old: str, sealed_target: str) -> dict:
    """Atomic alias delete+create. Caller owns pause/drain/journal/approval/single-writer.

    No hidden retries. Ambiguous adapter failures -> OutcomeUnknown.
    """
    actual = await resolve_alias(q, alias)
    if actual != expected_old:
        raise ValueError("alias changed before dispatch")
    payload = {"actions": [
        {"delete_alias": {"alias_name": alias}},
        {"create_alias": {"alias_name": alias, "collection_name": sealed_target}},
    ]}
    try:
        response = await q.post("/collections/aliases", json=payload)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise OutcomeUnknown("inspect journal and alias state") from exc
    try:
        observed = await resolve_alias(q, alias)
    except httpx.HTTPError as exc:
        raise OutcomeUnknown("write ack received; read unavailable") from exc
    if observed != sealed_target:
        raise OutcomeUnknown("acknowledgement and observed state differ")
    return {"before": actual, "after": observed}


def qdrant_client(settings) -> httpx.AsyncClient:
    headers = {}
    if getattr(settings, "qdrant_api_key", ""):
        headers["api-key"] = settings.qdrant_api_key
    return httpx.AsyncClient(base_url=settings.qdrant_url, headers=headers, timeout=15.0)
