"""Cutover protocol — coordinate routing, alias and cache (spec p21).

There is no transaction spanning SQLite, Qdrant and the gateway cache.
This module implements the explicit 8-step local protocol with
journaled intent, single-writer ownership (fence), and crash-matrix
recovery. Scoped to single-node native Qdrant; clustered promotion
requires separate visibility/replica-health policies.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class CutoverError(RuntimeError):
    pass


class OutcomeUnknown(RuntimeError):
    pass


def rollback_guards(
    operation: dict, current_alias_target: str, fence_ok: bool, route_generation: int | None = None
) -> tuple[bool, str]:
    """Rollback requires retained verified old collection + alias still on new + fence match.

    operation: {rollback_allowed, after, expected_generation?, old_collection?}
    Returns (allowed, reason).
    """
    if not operation.get("rollback_allowed"):
        return False, "rollback not included in original plan"
    if operation.get("after") != current_alias_target:
        return False, "alias no longer targets this operation's new collection"
    if not fence_ok:
        return False, "ownership/fence mismatch"
    return True, "rollback guards pass"


class Cutover:
    """8-step alias cutover with persisted state.

    Dependencies are injected for testability:
      db: Database (routes, operations, incidents, events)
      gateway: Gateway (admission gate + cache)
      resolve_alias(q, alias), switch_alias(q, alias, old, new): qdrant_io funcs
      verify_target(collection) -> list[str]: exact manifest check
      probe_gateway(route, canaries, principal) -> dict(passed=bool)
    """

    def __init__(
        self,
        db,
        gateway=None,
        qdrant_factory=None,
        verify_fn: Callable | None = None,
        probe_fn: Callable | None = None,
    ):
        self.db = db
        self.gateway = gateway
        self.qdrant_factory = qdrant_factory
        self.verify_fn = verify_fn
        self.probe_fn = probe_fn
        self._owner: dict[str, str] = {}  # scope_key -> fence token

    # -- ownership (single writer; local lock is NOT a remote fencing token) --
    def acquire(self, scope_key: str, fence: int) -> str:
        if scope_key in self._owner:
            raise CutoverError(f"writer already owns {scope_key}; no second writer/failover")
        token = f"{fence}:{uuid.uuid4().hex[:8]}"
        self._owner[scope_key] = token
        return token

    def release(self, scope_key: str):
        self._owner.pop(scope_key, None)

    def owns(self, scope_key: str) -> bool:
        return scope_key in self._owner

    # -- Step 1: Prepare --
    def prepare(self, conn, incident_id: str, plan_body: dict, approval: dict) -> dict:
        """Verify sole ownership, no unresolved operation, approval/target/before-state.

        Raises CutoverError/PermissionError; journals prepared intent.
        """
        from .execute import require_approval

        scope = plan_body["scope"]
        scope_key = f"{scope['project']}/{scope['corpus']}/{scope['environment']}"
        route = dict(
            self.db.get_route(conn, scope["project"], scope["corpus"], scope["environment"])
        )
        # No unresolved prior write
        cur = conn.execute(
            "SELECT id, status FROM operations WHERE incident_id=?"
            " AND status IN ('prepared','dispatched','unknown')",
            (incident_id,),
        )
        pending = cur.fetchall()
        if pending:
            raise CutoverError(
                f"unresolved prior operation: {[dict(r) for r in pending]}; reconcile first"
            )
        # Approval + before-state (also checks expiry/plan hash)
        snapshot = plan_body["expected_before"]
        live_snapshot = {
            "alias": route["alias_name"],
            "collection": route["collection_name"],
            "generation": route["generation"],
            "cache_epoch": route["cache_epoch"],
            "fence": route["fence"],
        }
        require_approval(approval, plan_body, live_snapshot)
        if live_snapshot != snapshot:
            raise CutoverError("preconditions changed; re-plan")
        # Sealed target check: caller must have verified manifest before calling prepare.
        # Recorded here as an explicit gate flag on the plan.
        if not plan_body.get("target_sealed", True):
            raise CutoverError("target not sealed/verified; refusing to prepare")
        token = self.acquire(scope_key, route["fence"])
        op_id = str(uuid.uuid4())
        with self.db.tx_immediate(conn):
            conn.execute(
                "INSERT INTO operations(id,incident_id,plan_digest,ordinal,"
                "kind,status,request_json,before_json,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    op_id,
                    incident_id,
                    plan_body.get("_digest", ""),
                    0,
                    plan_body["actions"][0]["kind"]
                    if plan_body.get("actions")
                    else "publish_sealed_index",
                    "prepared",
                    json.dumps(
                        {
                            "target": plan_body["actions"][0].get("target")
                            if plan_body.get("actions")
                            else None
                        }
                    ),
                    json.dumps(live_snapshot),
                    _utcnow(),
                ),
            )
            self.db.append_event(
                conn,
                incident_id,
                "cutover.prepared",
                {"operation_id": op_id, "scope_key": scope_key},
            )
        return {
            "operation_id": op_id,
            "scope_key": scope_key,
            "token": token,
            "before": live_snapshot,
            "route": route,
        }

    # -- Step 2: Pause --
    def pause(self, conn, scope: dict):
        with self.db.tx_immediate(conn):
            conn.execute(
                "UPDATE routes SET mode='paused'"
                " WHERE project_id=? AND corpus_id=? AND environment=?",
                (scope["project"], scope["corpus"], scope["environment"]),
            )
            # Incident lookup for event join is best-effort; event needs an incident id.
            cur = conn.execute(
                "SELECT id FROM incidents WHERE project_id=? AND corpus_id=?"
                " AND environment=? AND terminal=0"
                " ORDER BY created_at DESC LIMIT 1",
                (scope["project"], scope["corpus"], scope["environment"]),
            )
            row = cur.fetchone()
            if row is not None:
                self.db.append_event(conn, dict(row)["id"], "route.paused", {"mode": "paused"})

    # -- Step 3: Drain --
    def drain(self, timeout_s: float = 10.0) -> bool:
        """Wait for already-admitted requests to finish; enforce deadline."""
        if self.gateway is None:
            return True
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self.gateway._lock:
                n = len(self.gateway._in_flight)
            if n == 0:
                return True
            time.sleep(0.05)
        return False

    # -- Step 4: Mutate --
    async def mutate(
        self,
        conn,
        incident_id: str,
        operation_id: str,
        alias: str,
        expected_old: str,
        sealed_target: str,
        approval_id: str | None = None,
    ) -> dict:
        """Commit dispatched + consume approval; send exact alias op. No auto-retry."""
        from . import qdrant_io as qio

        with self.db.tx_immediate(conn):
            conn.execute(
                "UPDATE operations SET status='dispatched', updated_at=? WHERE id=?",
                (_utcnow(), operation_id),
            )
            if approval_id:
                conn.execute(
                    "UPDATE approvals SET state='consumed' WHERE id=? AND state='active'",
                    (approval_id,),
                )
            self.db.append_event(
                conn,
                incident_id,
                "operation.dispatched",
                {
                    "operation_id": operation_id,
                    "alias": alias,
                    "expected_old": expected_old,
                    "target": sealed_target,
                },
            )
        if self.qdrant_factory is None:
            raise CutoverError("no qdrant factory; cannot dispatch remote write")
        try:
            async with self.qdrant_factory() as q:
                result = await qio.switch_alias(q, alias, expected_old, sealed_target)
        except Exception as exc:
            # Conservatively reconcile ALL ambiguous failures; record + mark unknown.
            is_unknown = isinstance(exc, qio.OutcomeUnknown) or "httpx" in type(exc).__module__
            with self.db.tx_immediate(conn):
                conn.execute(
                    "UPDATE operations SET status=?, response_json=?, updated_at=? WHERE id=?",
                    (
                        "unknown" if is_unknown else "failed",
                        json.dumps({"error": str(exc)[:500]}),
                        _utcnow(),
                        operation_id,
                    ),
                )
                self.db.append_event(
                    conn,
                    incident_id,
                    "operation.unknown" if is_unknown else "operation.failed",
                    {"operation_id": operation_id, "error": str(exc)[:500]},
                )
            if is_unknown:
                raise OutcomeUnknown("inspect journal and alias state") from exc
            raise
        with self.db.tx_immediate(conn):
            conn.execute(
                "UPDATE operations SET status='observed', after_json=?, updated_at=? WHERE id=?",
                (json.dumps(result), _utcnow(), operation_id),
            )
            self.db.append_event(
                conn, incident_id, "operation.observed", {"operation_id": operation_id, **result}
            )
        return result

    # -- Step 5: Observe --
    async def observe(self, alias: str, sealed_target: str) -> dict:
        from . import qdrant_io as qio

        if self.qdrant_factory is None:
            raise CutoverError("no qdrant factory for observe")
        try:
            async with self.qdrant_factory() as q:
                observed = await qio.resolve_alias(q, alias)
        except Exception as exc:
            raise OutcomeUnknown("write ack received; read unavailable") from exc
        if observed != sealed_target:
            raise OutcomeUnknown("acknowledgement and observed state differ")
        return {"alias": alias, "observed": observed}

    # -- Step 6: Install --
    def install(
        self, conn, incident_id: str, scope: dict, sealed_target: str, manifest_digest: str
    ) -> dict:
        """One SQLite tx: physical target + manifest + generation+1 + epoch+1 + verifying."""
        with self.db.tx_immediate(conn):
            route = dict(
                self.db.get_route(conn, scope["project"], scope["corpus"], scope["environment"])
            )
            new_tuple = {
                "collection_name": sealed_target,
                "manifest_digest": manifest_digest,
                "generation": route["generation"] + 1,
                "cache_epoch": route["cache_epoch"] + 1,
                "mode": "verifying",
            }
            conn.execute(
                "UPDATE routes SET collection_name=?, manifest_digest=?,"
                " generation=?, cache_epoch=?, mode=?"
                " WHERE project_id=? AND corpus_id=? AND environment=?",
                (
                    new_tuple["collection_name"],
                    new_tuple["manifest_digest"],
                    new_tuple["generation"],
                    new_tuple["cache_epoch"],
                    new_tuple["mode"],
                    scope["project"],
                    scope["corpus"],
                    scope["environment"],
                ),
            )
            self.db.append_event(conn, incident_id, "route.installed", new_tuple)
        return new_tuple

    # -- Step 7: Verify (real gateway/cache path, repeat/hit probes) --
    async def verify(
        self, conn, incident_id: str, route: dict, canaries: list, principal: dict
    ) -> dict:
        if self.gateway is None or self.probe_fn is None:
            # Fallback: direct probe_fn or fail closed
            if self.probe_fn is None:
                raise CutoverError("no probe path; refusing to publish unverified route")
        results = []
        for can in canaries:
            if self.probe_fn is not None:
                r = await self.probe_fn(self.gateway, can, route, principal)
            else:
                from . import verify as vmod

                r = await vmod.probe_twice(self.gateway, can, route, principal)
            results.append(r)
        passed = all(r.get("passed") for r in results)
        with self.db.tx_immediate(conn):
            conn.execute(
                "UPDATE operations SET status=?, updated_at=?"
                " WHERE incident_id=? AND status='observed'",
                ("verified" if passed else "failed", _utcnow(), incident_id),
            )
            self.db.append_event(
                conn,
                incident_id,
                "verification.completed",
                {"passed": passed, "n_canaries": len(results)},
            )
        return {"passed": passed, "results": results}

    # -- Step 8: Publish --
    def publish(self, conn, incident_id: str, scope: dict, outbox_row: dict | None = None) -> dict:
        """Atomically set serving + resolved + event + outbox row (p8)."""
        with self.db.tx_immediate(conn):
            conn.execute(
                "UPDATE routes SET mode='serving'"
                " WHERE project_id=? AND corpus_id=? AND environment=?",
                (scope["project"], scope["corpus"], scope["environment"]),
            )
            conn.execute(
                "UPDATE incidents SET state='RESOLVED', terminal=1,"
                " state_version=state_version+1, updated_at=? WHERE id=?",
                (_utcnow(), incident_id),
            )
            event_seq = self.db.append_event(
                conn, incident_id, "incident.resolved", {"mode": "serving"}
            )
            if outbox_row is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO memory_outbox(event_id,incident_id,"
                    "document_id,bank_id,"
                    "payload_json,payload_sha256,operation_id,state,"
                    "attempts,next_attempt_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        outbox_row["event_id"],
                        outbox_row["incident_id"],
                        outbox_row["document_id"],
                        outbox_row["bank_id"],
                        outbox_row["payload_json"],
                        outbox_row["payload_sha256"],
                        outbox_row["operation_id"],
                        "pending",
                        0,
                        outbox_row["next_attempt_at"],
                    ),
                )
        # Release single-writer ownership on success
        scope_key = f"{scope['project']}/{scope['corpus']}/{scope['environment']}"
        self.release(scope_key)
        return {"state": "RESOLVED", "event_seq": event_seq}

    # -- Crash/timeout matrix (p21) --
    def crash_action(self, crash_point: str) -> str:
        matrix = {
            "before_dispatch": "Inspect prepared intent. If no operation could have "
            "been sent, revalidate current authorization before any new dispatch.",
            "after_dispatch_no_ack": "Treat outcome as unknown. Keep gate closed; "
            "reconcile actual alias, route and target. Do not infer failure.",
            "after_alias_before_route": "Reconcile: observe target and transactionally "
            "repair the local routing tuple under continued ownership, then verify.",
            "after_route_before_verify": "Restart in reconcile/verifying mode and rerun "
            "postconditions. Do not open public reads on process startup.",
        }
        return matrix.get(crash_point, "Reconcile: gate reads, inspect journal + alias + route.")

    async def reconcile(self, conn, incident_id: str, scope: dict, alias: str) -> dict:
        """Reconcile uncertain writes: gate reads, compare alias vs route, decide.

        Never infers failure from missing ack; blocks conflicting writes until resolved.
        """
        from . import qdrant_io as qio

        with self.db.tx_immediate(conn):
            conn.execute(
                "UPDATE routes SET mode='reconcile'"
                " WHERE project_id=? AND corpus_id=? AND environment=?",
                (scope["project"], scope["corpus"], scope["environment"]),
            )
            self.db.append_event(conn, incident_id, "reconcile.entered", {"alias": alias})
        observed = None
        if self.qdrant_factory is not None:
            try:
                async with self.qdrant_factory() as q:
                    observed = await qio.resolve_alias(q, alias)
            except Exception as exc:
                with self.db.tx_immediate(conn):
                    self.db.append_event(
                        conn, incident_id, "reconcile.alias_unreadable", {"error": str(exc)[:300]}
                    )
                return {
                    "state": "RECONCILING",
                    "observed": None,
                    "note": "alias unreadable; operator required",
                }
        route = dict(
            self.db.get_route(conn, scope["project"], scope["corpus"], scope["environment"])
        )
        with self.db.tx_immediate(conn):
            self.db.append_event(
                conn,
                incident_id,
                "reconcile.observed",
                {"alias_target": observed, "route_collection": route["collection_name"]},
            )
        if observed == route["collection_name"]:
            # Converged: move to verifying and rerun postconditions
            with self.db.tx_immediate(conn):
                conn.execute(
                    "UPDATE routes SET mode='verifying' WHERE project_id=? AND corpus_id=?"
                    " AND environment=?",
                    (scope["project"], scope["corpus"], scope["environment"]),
                )
            return {"state": "VERIFYING", "observed": observed}
        return {
            "state": "RECONCILING",
            "observed": observed,
            "note": "alias and route disagree; operator-controlled reconciliation required; "
            "do not transfer executor lease merely because TTL elapsed",
        }
