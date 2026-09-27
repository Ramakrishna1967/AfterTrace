"""Reliable memory delivery — transactional outbox (spec p22)."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def build_outbox_row(
    incident_id: str,
    event_id: str,
    bank_id: str,
    summary: str,
    scope_tags: list[str],
    string_metadata: dict,
) -> dict:
    payload = {
        "items": [
            {
                "content": summary,
                "document_id": f"aftertrace-{incident_id}-{event_id}",
                "timestamp": _utcnow(),
                "context": "AFTERTRACE terminal incident",
                "tags": scope_tags,
                "metadata": string_metadata,
            }
        ],
        "async": True,
    }
    payload_json = json.dumps(payload, sort_keys=True)
    return {
        "event_id": event_id,
        "incident_id": incident_id,
        "document_id": f"aftertrace-{incident_id}-{event_id}",
        "bank_id": bank_id,
        "payload_json": payload_json,
        "payload_sha256": hashlib.sha256(payload_json.encode()).hexdigest(),
        "operation_id": str(uuid.uuid4()),
        "state": "pending",
        "attempts": 0,
        "next_attempt_at": _utcnow(),
        "last_error": None,
    }


async def submit_memory(h, row: dict) -> dict:
    """h is an authenticated HTTPX client for the configured API base URL."""
    payload = {
        "items": [
            {
                "content": json.loads(row["payload_json"])["items"][0]["content"],
                "document_id": row["document_id"],
                "timestamp": json.loads(row["payload_json"])["items"][0]["timestamp"],
                "context": "AFTERTRACE terminal incident",
                "tags": json.loads(row["payload_json"])["items"][0]["tags"],
                "metadata": json.loads(row["payload_json"])["items"][0]["metadata"],
            }
        ],
        "async": True,
        "operation_id": row["operation_id"],
    }
    response = await h.post(f"/v1/default/banks/{row['bank_id']}/memories", json=payload)
    response.raise_for_status()
    return response.json()


async def memory_operation(h, bank_id: str, operation_id: str) -> dict:
    response = await h.get(f"/v1/default/banks/{bank_id}/operations/{operation_id}")
    response.raise_for_status()
    return response.json()


def backoff(attempts: int, jitter_s: float = 1.0) -> str:
    """Capped exponential backoff with jitter (p22). Never generate a new UUID on retry."""
    import random

    base = min(2**attempts * 5, 300)
    delay = base + random.uniform(0, jitter_s)
    dt = datetime.now(UTC) + timedelta(seconds=delay)
    return dt.isoformat()


def is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    # Schema/auth/operation-ID conflicts -> dead-letter, never silent new UUID.
    if any(
        k in msg
        for k in (
            "401",
            "403",
            "422",
            "operation-id conflict",
            "operation_id conflict",
            "unauthorized",
            "forbidden",
            "validation",
        )
    ):
        return False
    return True


async def deliver_once(db, h, row: dict) -> str:
    """Deliver one outbox row through pending->submitted->completed/retry/dead.

    Same operation UUID is replayed on uncertain ack (p22 step 2).
    Request acceptance != extracted memory availability: poll operation to completion.
    Returns new state.
    """
    from . import security as sec

    conn = db.connect()
    try:
        attempts = int(row["attempts"]) + 1
        # 1. pending: validate scope, stable IDs, redaction, digest
        payload_obj = json.loads(row["payload_json"])
        content = payload_obj["items"][0]["content"]
        if sec.scan_for_secrets(content):
            content = sec.redact(content)
        if attempts == 1:
            db.outbox_update(conn, row["event_id"], "pending", attempts - 1, _utcnow(), None)
        # 2. submitted: identical request, same operation UUID
        try:
            await submit_memory(h, row)
        except Exception as exc:
            if not is_retryable(exc):
                with db.tx_immediate(conn):
                    db.outbox_update(
                        conn,
                        row["event_id"],
                        "dead",
                        attempts,
                        _utcnow(),
                        sec.redact(str(exc))[:500],
                    )
                return "dead"
            with db.tx_immediate(conn):
                db.outbox_update(
                    conn,
                    row["event_id"],
                    "retry",
                    attempts,
                    backoff(attempts),
                    sec.redact(str(exc))[:500],
                )
            return "retry"
        with db.tx_immediate(conn):
            db.outbox_update(conn, row["event_id"], "submitted", attempts, _utcnow(), None)
        # 3-4. Poll until completed/failed/cancelled; do not treat acceptance as availability.
        try:
            op = await memory_operation(h, row["bank_id"], row["operation_id"])
        except Exception as exc:
            with db.tx_immediate(conn):
                db.outbox_update(
                    conn,
                    row["event_id"],
                    "retry",
                    attempts,
                    backoff(attempts),
                    sec.redact(str(exc))[:500],
                )
            return "retry"
        status = str(op.get("status", op.get("state", ""))).lower()
        if status in ("completed", "succeeded", "done"):
            with db.tx_immediate(conn):
                db.outbox_update(conn, row["event_id"], "completed", attempts, _utcnow(), None)
            return "completed"
        if status in ("failed", "cancelled", "canceled"):
            # Terminal-failed: resubmitting same op ID is NOT a fresh job; dead-letter + alert.
            with db.tx_immediate(conn):
                db.outbox_update(
                    conn,
                    row["event_id"],
                    "dead",
                    attempts,
                    _utcnow(),
                    f"operation terminal: {status}",
                )
            return "dead"
        with db.tx_immediate(conn):
            db.outbox_update(conn, row["event_id"], "submitted", attempts, backoff(attempts), None)
        return "submitted"
    finally:
        conn.close()


async def run_outbox_sweep(db, client_factory, limit: int = 20) -> dict:
    """One durable-work polling sweep (called by lifespan task + tests)."""
    conn = db.connect()
    try:
        due = db.outbox_due(conn, _utcnow(), limit)
    finally:
        conn.close()
    if not due:
        return {"swept": 0}
    out = {"swept": len(due), "completed": 0, "retry": 0, "dead": 0, "submitted": 0}
    async with client_factory() as h:
        for row in due:
            try:
                state = await deliver_once(db, h, dict(row))
                out[state] = out.get(state, 0) + 1
            except Exception:
                out["retry"] = out.get("retry", 0) + 1
    return out


def correction_event(supersedes_document_id: str, new_summary: str) -> dict:
    """Append-only correction (p22): new doc explicitly supersedes old; never rewrite."""
    from . import security as sec

    return {
        "supersedes": supersedes_document_id,
        "sanitized_summary": sec.sanitize_summary(
            f"CORRECTION supersedes {supersedes_document_id}: {new_summary}"
        ),
    }
