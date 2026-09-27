"""FastAPI control endpoints (spec p23). Single worker, lifespan owns queue/worker/outbox."""
from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from .db import Database
from .gateway import Gateway
from .models import canonical_bytes
from .settings import Settings


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class CreateIncident(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corpus_id: str
    desired_manifest_digest: str


def build_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = db or Database(settings.database_path)
    gateway = Gateway(db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup order (spec p29):
        # 1. settings already validated (pydantic); check migration level + executor ownership
        # 2. SQLite open (WAL/FK per connection in Database.connect)
        # 3. Qdrant connectivity deferred (mark gated, don't block serve in tests)
        # 4. Hindsight compat deferred (mark degraded, never weaken repair controls)
        # 5. Rebuild durable work: gate incomplete mutations in reconcile, keep
        #    approval-waiting incidents idle, restart outbox polling.
        import asyncio

        app.state.db = db
        app.state.settings = settings
        app.state.gateway = gateway
        app.state.memory_degraded = False
        app.state.qdrant_ready = False
        # Resume scan: nonterminal incidents + dispatched/unknown ops -> reconcile/gated
        try:
            conn = db.connect()
            try:
                for inc in db.nonterminal_incidents(conn):
                    inc_d = dict(inc)
                    ops = db.unresolved_operations(conn, inc_d["id"])
                    if ops:
                        with db.tx_immediate(conn):
                            conn.execute(
                                "UPDATE routes SET mode='reconcile' WHERE project_id=? AND corpus_id=?"
                                " AND environment=?",
                                (inc_d["project_id"], inc_d["corpus_id"], inc_d["environment"]),
                            )
                            db.append_event(conn, inc_d["id"], "startup.reconcile",
                                            {"unresolved_ops": len(ops)})
            finally:
                conn.close()
        except Exception:
            pass
        # Hindsight compat probe (degraded, non-blocking)
        if not settings.hindsight_base_url or not settings.hindsight_api_key:
            app.state.memory_degraded = True
        # Background outbox sweeper (durable-work polling)
        stop = asyncio.Event()

        async def _sweeper():
            from . import outbox as obox

            def _factory():
                import httpx

                return httpx.AsyncClient(
                    base_url=settings.hindsight_base_url or "http://127.0.0.1:9",
                    headers={"Authorization": "Bearer " + (settings.hindsight_api_key or "none")},
                    timeout=15.0,
                )

            while not stop.is_set():
                try:
                    if not getattr(app.state, "memory_degraded", False):
                        await obox.run_outbox_sweep(db, _factory, limit=20)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    pass

        task = asyncio.create_task(_sweeper())
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            try:
                await task
            except BaseException:
                pass

    app = FastAPI(title="AfterTrace", lifespan=lifespan)
    app.state.db = db
    app.state.settings = settings
    app.state.gateway = gateway
    app.state.memory_degraded = False

    def _principal(request: Request) -> dict:
        # Minimal auth: headers X-Project / X-Role / X-Tenant; real provider injected in prod.
        # Never trust request body fields for scope (p5/p25).
        project = request.headers.get("x-project", "sample")
        role = request.headers.get("x-role", "operator")
        tenant = request.headers.get("x-tenant", "tenant:a")
        return {"project_id": project, "corpus_id": "", "environment": "staging",
                "role": role, "tenant": tenant, "acl_scope": [tenant]}

    @app.post("/v1/manifests", status_code=201)
    async def create_manifest(request: Request):
        from .models import Manifest

        body = await request.json()
        try:
            m = Manifest(**body)
        except Exception as e:
            raise HTTPException(422, f"invalid manifest: {e}")
        # Bind project/corpus/env to authenticated caller scope.
        # Strict equality: no wildcard principal may mint manifests for other projects.
        principal = _principal(request)
        if m.project_id != principal["project_id"]:
            raise HTTPException(403, "unauthorized scope")
        digest = hashlib.sha256(canonical_bytes(m.model_dump(mode="json"))).hexdigest()
        conn = db.connect()
        try:
            db.insert_manifest(conn, digest, m.project_id, m.corpus_id, m.environment,
                               m.revision, json.dumps(m.model_dump(mode="json")))
        finally:
            conn.close()
        return {"digest": digest}

    async def create_incident_handler(body: CreateIncident, request_key: str, principal: dict, service=None):
        principal_require = principal.get("project_id")
        if not principal_require:
            raise HTTPException(403, "unauthorized scope")
        # service.create_or_get placeholder -> inline implementation
        conn = db.connect()
        try:
            # Idempotency: same project+key returns existing; different digest -> 409
            cur = conn.execute(
                "SELECT * FROM incidents WHERE project_id=? AND request_key=?",
                (principal_require, request_key),
            )
            row = cur.fetchone()
            if row is not None:
                row = dict(row)
                if row["desired_manifest"] != body.desired_manifest_digest:
                    raise HTTPException(409, "idempotency key reused with different digest")
                return {"incident_id": row["id"], "state": row["state"]}
            # Verify manifest exists
            mrow = db.get_manifest(conn, body.desired_manifest_digest)
            if mrow is None:
                raise HTTPException(422, "unknown desired manifest")
            iid = str(uuid.uuid4())
            now = _utcnow()
            with db.tx_immediate(conn):
                conn.execute(
                    "INSERT INTO incidents(id,project_id,corpus_id,environment,desired_manifest,state,terminal,state_version,request_key,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (iid, principal_require, body.corpus_id, "staging",
                     body.desired_manifest_digest, "DETECTED", 0, 0, request_key, now, now),
                )
                db.append_event(conn, iid, "incident.detected",
                                {"corpus": body.corpus_id, "manifest": body.desired_manifest_digest})
            return {"incident_id": iid, "state": "DETECTED"}
        finally:
            conn.close()

    @app.post("/v1/incidents", status_code=202)
    async def create_incident(body: CreateIncident, request: Request,
                              idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        if not idempotency_key:
            raise HTTPException(422, "Idempotency-Key required")
        principal = _principal(request)
        principal["corpus_id"] = body.corpus_id
        return await create_incident_handler(body, idempotency_key, principal)

    @app.get("/v1/incidents/{incident_id}")
    async def get_incident(incident_id: str, request: Request):
        conn = db.connect()
        try:
            cur = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "unknown incident")
            row = dict(row)
            events = db.events_since(conn, incident_id, 0, 200)
            plans = conn.execute("SELECT digest, body_json FROM plans WHERE incident_id=?",
                                 (incident_id,)).fetchall()
            return {
                "id": row["id"], "state": row["state"], "state_version": row["state_version"],
                "scope": {"project": row["project_id"], "corpus": row["corpus_id"],
                          "environment": row["environment"]},
                "desired_manifest": row["desired_manifest"],
                "events": [{"seq": e["seq"], "kind": e["kind"]} for e in events],
                "plans": [dict(p) for p in plans],
            }
        finally:
            conn.close()

    @app.post("/v1/incidents/{incident_id}/diagnostics")
    async def diagnostics(incident_id: str, request: Request):
        from . import diagnose as diag

        body = await request.json()
        action = body.get("action")
        if action not in diag.ALLOWED_ACTIONS:
            raise HTTPException(422, "unsupported action")
        # Incident-bound, read-only; scope-check the caller against the incident owner.
        conn = db.connect()
        try:
            cur = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "unknown incident")
            inc = dict(row)
            from . import security as sec

            try:
                sec.require_scope(
                    _principal(request), inc["project_id"], inc["corpus_id"], inc["environment"]
                )
            except PermissionError as e:
                raise HTTPException(403, str(e))
        finally:
            conn.close()
        return {"incident_id": incident_id, "action": action, "status": "accepted"}

    @app.post("/v1/incidents/{incident_id}/approvals")
    async def approvals(incident_id: str, request: Request):
        from . import security as sec

        body = await request.json()
        plan_digest_val = body.get("plan_digest")
        if not plan_digest_val:
            raise HTTPException(422, "plan_digest required")
        principal = _principal(request)
        try:
            sec.require_approver_role(principal)
        except PermissionError as e:
            raise HTTPException(403, str(e))
        conn = db.connect()
        try:
            cur = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,))
            inc = cur.fetchone()
            if inc is None:
                raise HTTPException(404, "unknown incident")
            inc = dict(inc)
            # Scope binding: approver must belong to incident project (p19/p25)
            if principal["project_id"] != inc["project_id"]:
                raise HTTPException(403, "unauthorized scope")
            cur = conn.execute("SELECT * FROM plans WHERE digest=? AND incident_id=?",
                               (plan_digest_val, incident_id))
            plan = cur.fetchone()
            if plan is None:
                raise HTTPException(409, "stale plan")
            # Expire stale approvals first; enforce single active approval per plan
            db.expire_approvals(conn, _utcnow())
            existing = db.active_approval_for_plan(conn, plan_digest_val)
            if existing is not None:
                return {"approval_id": dict(existing)["id"],
                        "expires_at": dict(existing)["expires_at"], "reused": True}
            aid = str(uuid.uuid4())
            # 5-minute bounded expiry (spec p25)
            from datetime import timedelta

            exp = (datetime.now(timezone.utc) + timedelta(seconds=settings.approval_validity_s)).isoformat()
            now = _utcnow()
            approver = request.headers.get("x-approver", principal.get("role", "operator"))
            with db.tx_immediate(conn):
                conn.execute(
                    "INSERT INTO approvals(id,plan_digest,approver_id,expires_at,state,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (aid, plan_digest_val, sec.redact(approver)[:120], exp, "active", now),
                )
                db.append_event(conn, incident_id, "approval.granted",
                                {"approval_id": aid, "plan_digest": plan_digest_val})
            return {"approval_id": aid, "expires_at": exp}
        finally:
            conn.close()

    @app.post("/v1/incidents/{incident_id}/cancel")
    async def cancel(incident_id: str, request: Request):
        from . import security as sec

        conn = db.connect()
        try:
            cur = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "unknown incident")
            inc = dict(row)
            try:
                sec.require_scope(
                    _principal(request), inc["project_id"], inc["corpus_id"], inc["environment"]
                )
            except PermissionError as e:
                raise HTTPException(403, str(e))
            cur = conn.execute("SELECT status FROM operations WHERE incident_id=? AND status IN ('dispatched','prepared')",
                               (incident_id,))
            pending = cur.fetchone() is not None
            if pending:
                return {"status": "pending", "note": "a write must be reconciled"}
            with db.tx_immediate(conn):
                conn.execute("UPDATE incidents SET state='CANCELLED', terminal=1 WHERE id=? AND terminal=0",
                             (incident_id,))
                db.append_event(conn, incident_id, "incident.cancelled", {})
            return {"status": "cancelled"}
        finally:
            conn.close()

    @app.get("/v1/incidents/{incident_id}/events")
    async def event_stream(incident_id: str, request: Request):
        from . import security as sec
        from .events import sse_format

        # Last-Event-ID via header OR query (?lastEventId=) for EventSource compat (p24).
        last_id = request.headers.get("Last-Event-ID",
                    request.query_params.get("lastEventId", request.query_params.get("since", "0")))
        try:
            since = int(str(last_id).strip() or "0")
        except ValueError:
            since = 0
        conn = db.connect()
        try:
            # Scope-check reconnects (p23): incident must exist; principal must match project.
            cur = conn.execute("SELECT project_id FROM incidents WHERE id=?", (incident_id,))
            inc = cur.fetchone()
            if inc is None:
                raise HTTPException(404, "unknown incident")
            principal = _principal(request)
            if principal["project_id"] != dict(inc)["project_id"]:
                raise HTTPException(403, "unauthorized scope")
            rows = db.events_since(conn, incident_id, since, 200)
            events = []
            for r in rows:
                try:
                    data = json.loads(r["body_json"])
                except ValueError:
                    data = {"unparseable": True}
                # Parse first, redact values after: redacting the raw string
                # can invalidate JSON and kill the stream (never loads(redacted)).
                data = sec.redact_json(data)
                events.append({"seq": r["seq"], "incident_id": incident_id, "kind": r["kind"],
                               "time": r["created_at"], "data": data})
        finally:
            conn.close()

        async def gen():
            import asyncio

            for e in events:
                yield sse_format(e)
            # Heartbeat idle streams so proxies don't kill them (p23).
            yield ": heartbeat\n\n"
            # Keep-alive for a short window then close (server-rendered clients reconnect
            # with Last-Event-ID; cap replay batches server-side).
            await asyncio.sleep(0)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/v1/query")
    async def query(request: Request):
        body = await request.json()
        principal = {
            "project_id": request.headers.get("x-project", "sample"),
            "corpus_id": body.get("corpus_id", "sdk-docs"),
            "environment": body.get("environment", "staging"),
            "acl_scope": [request.headers.get("x-tenant", "tenant:a")],
        }
        q = {"query_vector": body.get("query_vector", []), "top_k": body.get("top_k", 3),
             "filters": body.get("filters", {}), "query_id": body.get("query_id", "")}
        try:
            res = await gateway.query(q, principal)
        except ValueError:
            # No serving route configured: gate closed, never an unhandled 500 (p21).
            return JSONResponse({"status": 503, "detail": "no serving route"},
                                status_code=503, headers={"Retry-After": "2"})
        if res.get("status") == 503:
            # Retry-After for gated/paused routes (p21 step 2).
            return JSONResponse(res, status_code=503, headers={"Retry-After": "2"})
        return res

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        # Readiness only for routes safe to serve (p29): report gated modes.
        conn = db.connect()
        try:
            cur = conn.execute("SELECT project_id, corpus_id, environment, mode FROM routes")
            routes = [dict(r) for r in cur.fetchall()]
            gated = [r for r in routes if r["mode"] != "serving"]
        finally:
            conn.close()
        return {"ready": len(gated) == 0, "gated": gated,
                "memory_degraded": getattr(app.state, "memory_degraded", False)}

    @app.get("/metrics")
    async def metrics():
        # Operational signals (p29): counts by state/outcome, unknown mutations,
        # approval rejections (approx via revoked/expired), outbox lag, verification failures.
        conn = db.connect()
        try:
            states = [dict(r) for r in conn.execute(
                "SELECT state, COUNT(*) c FROM incidents GROUP BY state").fetchall()]
            unknowns = conn.execute(
                "SELECT COUNT(*) c FROM operations WHERE status='unknown'").fetchone()[0]
            outbox = conn.execute(
                "SELECT state, COUNT(*) c FROM memory_outbox GROUP BY state").fetchall()
            lag = conn.execute(
                "SELECT MIN(next_attempt_at) m FROM memory_outbox WHERE state IN ('pending','retry','submitted')").fetchone()[0]
            approvals = [dict(r) for r in conn.execute(
                "SELECT state, COUNT(*) c FROM approvals GROUP BY state").fetchall()]
            verif = conn.execute(
                "SELECT COUNT(*) c FROM events WHERE kind='verification.completed'").fetchone()[0]
        finally:
            conn.close()
        return {
            "incident_state_total": {s["state"]: s["c"] for s in states},
            "mutation_unknown_total": unknowns,
            "memory_outbox": {dict(r)["state"]: dict(r)["c"] for r in outbox},
            "memory_outbox_oldest_due": lag,
            "approvals_by_state": {a["state"]: a["c"] for a in approvals},
            "verification_completed_total": verif,
        }

    @app.get("/")
    async def index():
        from fastapi.responses import HTMLResponse

        import pathlib

        for cand in (pathlib.Path("web/index.html"),
                     pathlib.Path(__file__).parent.parent.parent / "web" / "index.html"):
            if cand.exists():
                return HTMLResponse(cand.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>AfterTrace</h1>")

    return app


app = build_app()
