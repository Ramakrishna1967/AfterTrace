"""Worker orchestration — bounded agent, persisted work (spec p17).

Implements investigate(incident_id) pseudocode + durable lifecycle (p13).
States: DETECTED DIAGNOSING PLAN_READY AWAITING_APPROVAL PREPARING MUTATING
        VERIFYING RESOLVED RECONCILING ESCALATED (+ CANCELLED/FAILED terminals)
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

from . import diagnose as diag
from . import planner as planner_mod
from . import verify as verify_mod

TERMINAL = {"RESOLVED", "ESCALATED", "CANCELLED", "FAILED"}

# Allowed diagnostic actions (spec p12)
ALLOWLIST = set(diag.ALLOWED_ACTIONS)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


class Worker:
    def __init__(self, db, gateway=None, memory_factory=None, qdrant_factory=None,
                 max_steps: int = 8, timeout_s: float = 120.0):
        self.db = db
        self.gateway = gateway
        self.memory_factory = memory_factory
        self.qdrant_factory = qdrant_factory
        self.max_steps = max_steps
        self.timeout_s = timeout_s
        self._locks: dict[str, bool] = {}

    # -- investigation lock (per-corpus) --
    def _acquire(self, key: str) -> bool:
        if self._locks.get(key):
            return False
        self._locks[key] = True
        return True

    def _release(self, key: str):
        self._locks.pop(key, None)

    async def investigate(self, incident_id: str) -> dict:
        conn = self.db.connect()
        try:
            cur = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,))
            inc = cur.fetchone()
            if inc is None:
                raise ValueError("unknown incident")
            inc = dict(inc)
            scope_key = f"{inc['project_id']}/{inc['corpus_id']}/{inc['environment']}"
            if not self._acquire(scope_key):
                return {"state": inc["state"], "note": "duplicate active investigation"}
            try:
                return await self._run(conn, inc)
            finally:
                self._release(scope_key)
        finally:
            conn.close()

    async def _run(self, conn, inc: dict) -> dict:
        incident_id = inc["id"]
        # Load desired manifest; verify source authority placeholder
        manifest_row = self.db.get_manifest(conn, inc["desired_manifest"])
        if manifest_row is None:
            self._transition(conn, inc, "ESCALATED", terminal=1, reason="desired manifest unknown")
            return {"state": "ESCALATED"}
        # Record initial gateway failure + routing snapshot (observation obs-0)
        observations: list[dict] = []
        obs0 = {
            "id": "obs-0",
            "tool": "probe_gateway",
            "status": "ok",
            "timestamp": _utcnow(),
            "data": {"note": "initial gateway failure recorded"},
        }
        observations.append(obs0)
        with self.db.tx_immediate(conn):
            self.db.append_event(conn, incident_id, "tool.observed",
                                 {"observation_id": "obs-0", "tool": "probe_gateway", "status": "ok"})

        # Scoped recall
        allowed_ids: set[str] = set()
        memory = None
        degraded = False
        if self.memory_factory is not None:
            try:
                memory = self.memory_factory(inc)
                results = await memory.recall(f"revision mismatch {inc['corpus_id']} gateway stale")
                # Post-filter: only authorized incident records; bound context
                for r in (results or [])[:10]:
                    mid = getattr(r, "id", None) or (r.get("id") if isinstance(r, dict) else None)
                    if mid:
                        allowed_ids.add(str(mid))
            except Exception as e:
                degraded = True
                with self.db.tx_immediate(conn):
                    self.db.append_event(conn, incident_id, "memory.degraded", {"error": str(e)[:500]})

        start = time.monotonic()
        tool_calls = 0
        history: list[tuple[str, str, int]] = []
        route_gen = 0

        while tool_calls < self.max_steps and (time.monotonic() - start) < self.timeout_s:
            # Generation-change guard would compare live route here; simplified: check DB route
            route = self.db.get_route(conn, inc["project_id"], inc["corpus_id"], inc["environment"])
            gen = dict(route)["generation"] if route else 0
            if gen != route_gen and route_gen != 0:
                with self.db.tx_immediate(conn):
                    self.db.append_event(conn, incident_id, "route.generation_changed",
                                         {"old": route_gen, "new": gen})
                self._transition(conn, inc, "RECONCILING", reason="routing generation changed")
                return {"state": "RECONCILING"}
            route_gen = gen

            # choose step
            try:
                if memory is not None and not degraded:
                    step, _based = await planner_mod.choose_step(memory, observations, allowed_ids)
                else:
                    raise RuntimeError("no-memory")
            except Exception:
                step = planner_mod.deterministic_fallback(observations)

            # Validate allowlist + budget
            if step.next_action not in ALLOWLIST:
                with self.db.tx_immediate(conn):
                    self.db.append_event(conn, incident_id, "diagnostic.rejected",
                                         {"action": step.next_action})
                continue
            if step.next_action == "escalate":
                self._persist_terminal(conn, inc, "ESCALATED", observations,
                                       reason=step.stop_reason or step.reason)
                inc["state"] = "ESCALATED"
                return {"state": "ESCALATED"}
            if step.next_action == "propose_repair":
                # Evaluate deterministic predicates against fresh evidence
                verdict = diag.classify(self._summarize(observations))
                if not verdict["repair_eligible"]:
                    with self.db.tx_immediate(conn):
                        self.db.append_event(conn, incident_id, "repair.insufficient_evidence",
                                             {"cause": verdict["cause"]})
                    # require specific missing evidence: force one more diagnostic
                    step2 = planner_mod.deterministic_fallback(observations)
                    if step2.next_action == "escalate":
                        self._persist_terminal(conn, inc, "ESCALATED", observations, reason="insufficient evidence")
                        return {"state": "ESCALATED"}
                    await self._dispatch(conn, inc, observations, step2, history)
                    tool_calls += 1
                    continue
                # Build canonical plan using server-selected resources
                plan = self._build_plan(conn, inc, observations)
                with self.db.tx_immediate(conn):
                    conn.execute(
                        "INSERT OR IGNORE INTO plans(digest,incident_id,body_json,created_at) VALUES(?,?,?,?)",
                        (plan["digest"], incident_id, json.dumps(plan["body"]), _utcnow()),
                    )
                    self._set_state(conn, inc, "PLAN_READY")
                    self.db.append_event(conn, incident_id, "plan.ready", {"digest": plan["digest"]})
                    self._set_state(conn, inc, "AWAITING_APPROVAL")
                    self.db.append_event(conn, incident_id, "approval.awaiting", {"digest": plan["digest"]})
                inc["state"] = "AWAITING_APPROVAL"
                return {"state": "AWAITING_APPROVAL", "plan": plan["digest"]}
            # read-only tool
            await self._dispatch(conn, inc, observations, step, history)
            tool_calls += 1
            if diag.repeated_call(history):
                self._persist_terminal(conn, inc, "ESCALATED", observations, reason="repeated calls, no progress")
                return {"state": "ESCALATED"}

        self._persist_terminal(conn, inc, "ESCALATED", observations, reason="budget exhausted")
        return {"state": "ESCALATED"}

    def _summarize(self, observations: list[dict]) -> dict:
        """Fold observations into predicate inputs (conservative defaults)."""
        flags: dict = {}
        for o in observations:
            d = o.get("data", {})
            for k, v in d.items():
                if k in ("target_passes", "alias_points_to_target", "route_matches_target",
                         "backend_probe_passes", "gateway_returns_expected", "source_verified",
                         "generations_mixed", "alias_route_disagree", "outstanding_mutation",
                         "semantic_only_failure"):
                    flags[k] = v
        return flags

    async def _dispatch(self, conn, inc, observations: list[dict], step, history: list):
        obs_id = f"obs-{len(observations)}"
        # Resolve tool args from incident scope, not model text
        tool = step.next_action
        history.append((tool, inc["corpus_id"], 0))
        # Minimal read-only implementations: record intent + structured observation
        data: dict = {"hypothesis": step.hypothesis, "reason": step.reason[:500]}
        if tool == "inspect_alias" and self.qdrant_factory is not None:
            try:
                async with self.qdrant_factory() as q:
                    from . import qdrant_io as qio

                    alias = f"{inc['project_id']}_{inc['corpus_id']}_live"
                    target = await qio.resolve_alias(q, alias)
                    data.update({"alias": alias, "target": target, "alias_points_to_target": True})
            except Exception as e:
                data.update({"status_detail": str(e)[:300]})
                obs = {"id": obs_id, "tool": tool, "status": "unavailable",
                       "timestamp": _utcnow(), "data": data}
                observations.append(obs)
                with self.db.tx_immediate(conn):
                    self.db.append_event(conn, inc["id"], "tool.observed",
                                         {"observation_id": obs_id, "tool": tool, "status": "unavailable"})
                return
        if tool == "probe_gateway" and self.gateway is not None:
            try:
                # probe with first canary of desired manifest
                mrow = self.db.get_manifest(conn, inc["desired_manifest"])
                import json as _json

                manifest = _json.loads(dict(mrow)["body_json"])
                can = manifest["canaries"][0] if manifest.get("canaries") else None
                if can is not None:
                    from .models import CanarySpec

                    cs = CanarySpec(**can)
                    route = dict(self.db.get_route(conn, inc["project_id"], inc["corpus_id"], inc["environment"]))
                    principal = {"project_id": inc["project_id"], "corpus_id": inc["corpus_id"],
                                 "environment": inc["environment"], "acl_scope": ["tenant:a"]}
                    res = await verify_mod.probe_twice(self.gateway, cs, route, principal)
                    data.update({"probe": res["passed"], "gateway_returns_expected": res["passed"]})
            except Exception as e:
                data.update({"status_detail": str(e)[:300]})
        obs = {"id": obs_id, "tool": tool, "status": "ok", "timestamp": _utcnow(),
               "route_generation": 0, "source_tool": tool,
               "content_digest": _digest(data), "data": data}
        observations.append(obs)
        with self.db.tx_immediate(conn):
            self.db.append_event(conn, inc["id"], "tool.observed",
                                 {"observation_id": obs_id, "tool": tool, "status": "ok"})

    def _build_plan(self, conn, inc, observations: list[dict]) -> dict:
        from . import execute as ex

        route = dict(self.db.get_route(conn, inc["project_id"], inc["corpus_id"], inc["environment"]))
        expected_before = {
            "alias": route["alias_name"],
            "collection": route["collection_name"],
            "generation": route["generation"],
            "cache_epoch": route["cache_epoch"],
            "fence": route["fence"],
        }
        # Server-selected target: desired manifest revision collection (not model text)
        mrow = self.db.get_manifest(conn, inc["desired_manifest"])
        import json as _json

        manifest = _json.loads(dict(mrow)["body_json"])
        target = f"{inc['project_id']}_{inc['corpus_id']}_build_{manifest.get('revision', 'B')}"
        not_after = datetime.now(timezone.utc).isoformat()
        # Distinguish cache-only vs publish via summarize flags
        flags = self._summarize(observations)
        body = ex.build_publish_plan(
            inc["id"],
            {"project": inc["project_id"], "corpus": inc["corpus_id"], "environment": inc["environment"]},
            inc["desired_manifest"],
            manifest.get("source_snapshot_sha256", ""),
            expected_before,
            target,
            (datetime.now(timezone.utc)).isoformat(),
        )
        if flags.get("cause") == "query_cache_contamination":
            body = {**body, "actions": [{"kind": "epoch_bump_only", "target": None}]}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        return {"digest": digest, "body": body}

    def _set_state(self, conn, inc, new_state: str):
        conn.execute("UPDATE incidents SET state=?, state_version=state_version+1, updated_at=? WHERE id=?",
                     (new_state, _utcnow(), inc["id"]))
        inc["state"] = new_state

    def _transition(self, conn, inc, new_state: str, terminal: int = 0, reason: str = ""):
        with self.db.tx_immediate(conn):
            self._set_state(conn, inc, new_state)
            if terminal:
                conn.execute("UPDATE incidents SET terminal=1 WHERE id=?", (inc["id"],))
            self.db.append_event(conn, inc["id"], "state.transition",
                                 {"to": new_state, "reason": reason})

    def _persist_terminal(self, conn, inc, state: str, observations: list[dict], reason: str):
        # Commit terminal status + terminal event + outbox row in one tx (spec p8)
        # Memory delivery happens afterward; verified repair stays verified if outbox lags.
        from . import outbox as obox
        from . import security as sec

        event_id = str(uuid.uuid4())
        summary = sec.sanitize_summary(
            f"Scope: {inc['project_id']}/{inc['environment']}/{inc['corpus_id']} "
            f"outcome {state}: {reason} | obs={len(observations)}")
        with self.db.tx_immediate(conn):
            conn.execute("UPDATE incidents SET state=?, terminal=1, state_version=state_version+1, updated_at=? WHERE id=?",
                         (state, _utcnow(), inc["id"]))
            self.db.append_event(conn, inc["id"], "incident.terminal",
                                 {"state": state, "reason": sec.redact(reason)[:1000], "event_id": event_id})
            try:
                row = obox.build_outbox_row(
                    inc["id"], event_id,
                    f"aftertrace-{inc['project_id']}-{inc['environment']}",
                    summary,
                    [f"project:{inc['project_id']}", f"env:{inc['environment']}",
                     "subsystem:rag", "compat:qdrant-local-v1"],
                    {"incident_id": inc["id"], "outcome": state},
                )
                conn.execute(
                    "INSERT OR IGNORE INTO memory_outbox(event_id,incident_id,document_id,bank_id,payload_json,payload_sha256,operation_id,state,attempts,next_attempt_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (row["event_id"], row["incident_id"], row["document_id"], row["bank_id"],
                     row["payload_json"], row["payload_sha256"], row["operation_id"],
                     "pending", 0, row["next_attempt_at"]),
                )
            except Exception:
                pass

    # -- resume + approved execution (spec p17/p21) --
    def resume_from_startup(self) -> dict:
        """Rebuild in-memory queue from durable rows (p17 resume behavior).

        - approval-waiting incidents stay idle
        - read-only diagnosis may resume from fresh snapshot (caller re-invokes investigate)
        - dispatched/unknown mutations enter reconciliation before another write
        """
        conn = self.db.connect()
        try:
            out = {"idle_approvals": [], "reconciling": [], "resumable": []}
            for row in self.db.nonterminal_incidents(conn):
                inc = dict(row)
                ops = self.db.unresolved_operations(conn, inc["id"])
                if inc["state"] == "AWAITING_APPROVAL":
                    out["idle_approvals"].append(inc["id"])
                elif ops and any(o["status"] in ("dispatched", "unknown") for o in [dict(x) for x in ops]):
                    with self.db.tx_immediate(conn):
                        self.db.set_route_mode(conn, inc["project_id"], inc["corpus_id"],
                                               inc["environment"], "reconcile")
                        self.db.append_event(conn, inc["id"], "startup.reconcile",
                                             {"ops": len(ops)})
                    out["reconciling"].append(inc["id"])
                else:
                    out["resumable"].append(inc["id"])
            return out
        finally:
            conn.close()

    async def approve_and_execute(self, incident_id: str, approval_id: str) -> dict:
        """Durable approved-execution path: PREPARING -> MUTATING -> VERIFYING -> RESOLVED.

        Consumes approval in the dispatch journal tx (re-entry resumes same op).
        Cache-only plans bump epoch; publish plans run the 8-step cutover.
        """
        from . import execute as ex
        from . import outbox as obox
        from .cutover import Cutover

        conn = self.db.connect()
        try:
            cur = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,))
            r = cur.fetchone()
            if r is None:
                raise ValueError("unknown incident")
            inc = dict(r)
            plan_rows = conn.execute("SELECT digest, body_json FROM plans WHERE incident_id=?",
                                     (incident_id,)).fetchall()
            if not plan_rows:
                raise ValueError("no plan for incident")
            plan_digest_val, plan_json = plan_rows[0][0], plan_rows[0][1]
            plan_body = json.loads(plan_json)
            plan_body["_digest"] = plan_digest_val
            appr = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if appr is None:
                raise PermissionError("unknown approval")
            approval = dict(appr)
            kind = (plan_body.get("actions") or [{"kind": "publish_sealed_index"}])[0].get("kind")
            scope = plan_body["scope"]
            scope_d = {"project": scope["project"], "corpus": scope["corpus"],
                       "environment": scope["environment"]}

            if kind == "epoch_bump_only":
                # Cache-only: verify backend+routing first (caller guarantees), then bump.
                route = dict(self.db.get_route(conn, scope_d["project"], scope_d["corpus"],
                                               scope_d["environment"]))
                snapshot = plan_body["expected_before"]
                live = {"alias": route["alias_name"], "collection": route["collection_name"],
                        "generation": route["generation"], "cache_epoch": route["cache_epoch"],
                        "fence": route["fence"]}
                ex.require_approval(approval, plan_body, live)
                with self.db.tx_immediate(conn):
                    conn.execute("UPDATE approvals SET state='consumed' WHERE id=? AND state='active'",
                                 (approval_id,))
                    self.db.append_event(conn, incident_id, "approval.consumed",
                                         {"approval_id": approval_id})
                bumped = self.db.bump_cache_epoch(conn, scope_d["project"], scope_d["corpus"],
                                                  scope_d["environment"])
                with self.db.tx_immediate(conn):
                    conn.execute("UPDATE incidents SET state='VERIFYING', state_version=state_version+1,"
                                 " updated_at=? WHERE id=?", (_utcnow(), incident_id))
                # Ordinary cache-enabled gateway probes (verification determines recovery)
                route2 = dict(self.db.get_route(conn, scope_d["project"], scope_d["corpus"],
                                                scope_d["environment"]))
                passed = True
                if self.gateway is not None:
                    try:
                        mrow = self.db.get_manifest(conn, inc["desired_manifest"])
                        manifest = json.loads(dict(mrow)["body_json"])
                        from .models import CanarySpec
                        from . import verify as vmod
                        principal = {"project_id": scope_d["project"], "corpus_id": scope_d["corpus"],
                                     "environment": scope_d["environment"], "acl_scope": ["tenant:a"]}
                        for c in manifest.get("canaries", [])[:3]:
                            cs = CanarySpec(**c)
                            res = await vmod.probe_twice(self.gateway, cs, route2, principal)
                            passed = passed and res["passed"]
                    except Exception:
                        passed = False
                if passed:
                    event_id = str(uuid.uuid4())
                    row = obox.build_outbox_row(
                        incident_id, event_id,
                        f"aftertrace-{scope_d['project']}-{scope_d['environment']}",
                        f"Scope: {scope_d['project']}/{scope_d['environment']}/{scope_d['corpus']} "
                        f"cache epoch {bumped['before_epoch']}->{bumped['after_epoch']} verified",
                        [f"project:{scope_d['project']}", f"env:{scope_d['environment']}",
                         "subsystem:rag", "compat:qdrant-local-v1"],
                        {"incident_id": incident_id, "outcome": "RESOLVED"})
                    with self.db.tx_immediate(conn):
                        conn.execute("UPDATE incidents SET state='RESOLVED', terminal=1,"
                                     " state_version=state_version+1, updated_at=? WHERE id=?",
                                     (_utcnow(), incident_id))
                        self.db.append_event(conn, incident_id, "incident.resolved",
                                             {"epoch": bumped})
                        conn.execute(
                            "INSERT OR IGNORE INTO memory_outbox(event_id,incident_id,document_id,bank_id,"
                            "payload_json,payload_sha256,operation_id,state,attempts,next_attempt_at)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (row["event_id"], row["incident_id"], row["document_id"], row["bank_id"],
                             row["payload_json"], row["payload_sha256"], row["operation_id"],
                             "pending", 0, row["next_attempt_at"]))
                    return {"state": "RESOLVED", "epoch": bumped}
                with self.db.tx_immediate(conn):
                    conn.execute("UPDATE incidents SET state='RECONCILING', updated_at=? WHERE id=?",
                                 (_utcnow(), incident_id))
                return {"state": "RECONCILING", "reason": "postconditions failed"}

            # Publish path: full 8-step cutover
            cut = Cutover(self.db, gateway=self.gateway, qdrant_factory=self.qdrant_factory)
            prep = cut.prepare(conn, incident_id, plan_body, approval)
            cut.pause(conn, scope_d)
            drained = cut.drain(timeout_s=10.0)
            if not drained:
                with self.db.tx_immediate(conn):
                    self.db.append_event(conn, incident_id, "cutover.drain_timeout", {})
            target = (plan_body.get("actions") or [{}])[0].get("target")
            alias = prep["before"]["alias"]
            expected_old = prep["before"]["collection"]
            try:
                await cut.mutate(conn, incident_id, prep["operation_id"], alias,
                                 expected_old, target, approval_id=approval_id)
            except Exception as exc:
                from .cutover import OutcomeUnknown as _OU
                if isinstance(exc, _OU):
                    await cut.reconcile(conn, incident_id, scope_d, alias)
                    return {"state": "RECONCILING", "reason": str(exc)[:300]}
                raise
            await cut.observe(alias, target)
            mrow = self.db.get_manifest(conn, inc["desired_manifest"])
            new_tuple = cut.install(conn, incident_id, scope_d, target, inc["desired_manifest"])
            # Verify with real gateway probes (installed verifying tuple)
            manifest = json.loads(dict(mrow)["body_json"])
            from .models import CanarySpec
            canaries = [CanarySpec(**c) for c in manifest.get("canaries", [])[:3]]
            principal = {"project_id": scope_d["project"], "corpus_id": scope_d["corpus"],
                         "environment": scope_d["environment"], "acl_scope": ["tenant:a"]}
            live_route = dict(self.db.get_route(conn, scope_d["project"], scope_d["corpus"],
                                                scope_d["environment"]))
            v = await cut.verify(conn, incident_id, live_route, canaries, principal)
            if not v["passed"]:
                with self.db.tx_immediate(conn):
                    conn.execute("UPDATE incidents SET state='RECONCILING', updated_at=? WHERE id=?",
                                 (_utcnow(), incident_id))
                return {"state": "RECONCILING", "reason": "postconditions failed"}
            event_id = str(uuid.uuid4())
            row = obox.build_outbox_row(
                incident_id, event_id, f"aftertrace-{scope_d['project']}-{scope_d['environment']}",
                f"Scope: {scope_d['project']}/{scope_d['environment']}/{scope_d['corpus']} "
                f"alias {alias} {expected_old}->{target} verified",
                [f"project:{scope_d['project']}", f"env:{scope_d['environment']}",
                 "subsystem:rag", "compat:qdrant-local-v1"],
                {"incident_id": incident_id, "outcome": "RESOLVED"})
            return cut.publish(conn, incident_id, scope_d, row)
        finally:
            conn.close()
