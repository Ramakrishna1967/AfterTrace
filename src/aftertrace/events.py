"""Durable event replay / SSE envelope (spec p23)."""

from __future__ import annotations

import json


def envelope(seq: int, incident_id: str, kind: str, time: str, data: dict) -> dict:
    return {"seq": seq, "incident_id": incident_id, "kind": kind, "time": time, "data": data}


def sse_format(event: dict) -> str:
    return f"id: {event['seq']}\nevent: {event['kind']}\ndata: {json.dumps(event)}\n\n"
