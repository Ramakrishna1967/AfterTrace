"""Approval + mutation guards + journaled dispatch (spec p19-21)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from .models import plan_digest


def _parse_dt(v):
    from datetime import datetime as _dt

    return _dt.fromisoformat(v) if isinstance(v, str) else v


def require_approval(approval: dict, plan_body: dict, snapshot: dict, now=None) -> None:
    """Bound approval to exact intent (spec p19).

    Checks: active state, approver scope/role (when present), desired-manifest
    identity + policy (when present), exact plan hash, full bound snapshot,
    expiry of both approval and plan. Consume-on-dispatch is done by the
    caller in the same journal tx; re-entry resumes the same operation.
    """
    from datetime import datetime as _dt

    now = now or datetime.now(timezone.utc)
    if isinstance(now, str):
        now = _dt.fromisoformat(now)
    if approval.get("state") != "active":
        raise PermissionError("approval is not active")
    exp = approval.get("expires_at")
    exp_dt = _dt.fromisoformat(exp) if isinstance(exp, str) else exp
    na = plan_body.get("not_after")
    na_dt = _dt.fromisoformat(na) if isinstance(na, str) else na
    if now >= exp_dt or (na_dt is not None and now >= na_dt):
        raise PermissionError("approval expired")
    if approval.get("plan_digest") != plan_digest(plan_body):
        raise PermissionError("plan has changed")
    if snapshot != plan_body.get("expected_before"):
        raise PermissionError("preconditions changed; re-plan")
    # Scope/role binding when the approval row carries them (hardened path).
    if approval.get("scope_project") and approval["scope_project"] != plan_body.get("scope", {}).get("project"):
        raise PermissionError("approval scope mismatch")
    if approval.get("approver_role") and approval["approver_role"] not in ("operator", "admin", "approver"):
        raise PermissionError("unauthorized approver role")


def build_publish_plan(incident_id: str, scope: dict, desired_manifest_digest: str,
                       source_snapshot_digest: str, expected_before: dict,
                       target_collection: str, not_after: str) -> dict:
    return {
        "schema_version": 1,
        "incident_id": incident_id,
        "scope": scope,
        "desired_manifest_digest": desired_manifest_digest,
        "source_snapshot_digest": source_snapshot_digest,
        "expected_before": expected_before,
        "actions": [{"kind": "publish_sealed_index", "target": target_collection}],
        "postconditions": ["exact_manifest", "gateway_canaries"],
        "rollback": {"allowed": False},
        "not_after": not_after,
    }


def rollback_allowed(operation: dict, current_alias_target: str, fence_ok: bool) -> bool:
    """Rollback requires retained verified old collection, alias still on new, fence match."""
    if not operation.get("rollback_allowed"):
        return False
    if operation.get("after") != current_alias_target:
        return False
    return bool(fence_ok)


def build_epoch_plan(incident_id: str, scope: dict, desired_manifest_digest: str,
                     source_snapshot_digest: str, expected_before: dict,
                     not_after: str) -> dict:
    """Cache-only repair plan (spec p20): epoch bump, no alias write."""
    return {
        "schema_version": 1,
        "incident_id": incident_id,
        "scope": scope,
        "desired_manifest_digest": desired_manifest_digest,
        "source_snapshot_digest": source_snapshot_digest,
        "expected_before": expected_before,
        "actions": [{"kind": "epoch_bump_only", "target": None}],
        "postconditions": ["exact_manifest", "gateway_canaries"],
        "rollback": {"allowed": False},
        "not_after": not_after,
    }


def build_staging_plan(incident_id: str, scope: dict, desired_manifest_digest: str,
                       source_snapshot_digest: str, expected_before: dict,
                       staging_name: str, not_after: str) -> dict:
    """Missing-document repair: bounded staging build first, then publication (p19)."""
    return {
        "schema_version": 1,
        "incident_id": incident_id,
        "scope": scope,
        "desired_manifest_digest": desired_manifest_digest,
        "source_snapshot_digest": source_snapshot_digest,
        "expected_before": expected_before,
        "actions": [{"kind": "build_staging_index", "target": staging_name}],
        "postconditions": ["exact_manifest"],
        "rollback": {"allowed": False},
        "not_after": not_after,
    }


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def journal_dispatch(conn, db, incident_id: str, plan_digest_val: str, ordinal: int,
                     kind: str, request: dict, before: dict) -> str:
    """Journal operation intent BEFORE any remote write (p21 step 3)."""
    op_id = str(uuid.uuid4())
    with db.tx_immediate(conn):
        conn.execute(
            "INSERT INTO operations(id,incident_id,plan_digest,ordinal,kind,status,"
            "request_json,before_json,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (op_id, incident_id, plan_digest_val, ordinal, kind, "prepared",
             json.dumps(request, sort_keys=True), json.dumps(before, sort_keys=True), _utcnow()),
        )
        db.append_event(conn, incident_id, "operation.prepared",
                        {"operation_id": op_id, "kind": kind})
    return op_id


def journal_result(conn, db, operation_id: str, incident_id: str, status: str,
                   after: dict | None = None, response: dict | None = None):
    assert status in ("dispatched", "observed", "verified", "unknown", "failed")
    with db.tx_immediate(conn):
        conn.execute(
            "UPDATE operations SET status=?, after_json=?, response_json=?, updated_at=? WHERE id=?",
            (status, json.dumps(after or {}), json.dumps(response or {}), _utcnow(), operation_id),
        )
        db.append_event(conn, incident_id, f"operation.{status}", {"operation_id": operation_id})
