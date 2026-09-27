"""SQLite authority — migrations, transactions, repositories (spec p7-8)."""
from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str):
        self.path = path
        p = pathlib.Path(path)
        if str(p.parent) not in ("", "."):
            p.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Ensure schema on first connect
        conn = self.connect()
        self.migrate(conn)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def migrate(self, conn: sqlite3.Connection) -> None:
        sql_path = pathlib.Path(__file__).parent.parent.parent / "migrations" / "001.sql"
        alt = pathlib.Path("migrations/001.sql")
        target = sql_path if sql_path.exists() else alt
        sql = target.read_text(encoding="utf-8")
        conn.executescript(sql)

    @contextmanager
    def tx_immediate(self, conn: sqlite3.Connection):
        """BEGIN IMMEDIATE transaction context."""
        conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise

    # -- manifests --
    def insert_manifest(self, conn, digest, project_id, corpus_id, environment, revision, body_json):
        now = _utcnow()
        conn.execute(
            "INSERT OR IGNORE INTO manifests(digest,project_id,corpus_id,environment,revision,body_json,source_verified_at,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (digest, project_id, corpus_id, environment, revision, body_json, now, now),
        )

    def get_manifest(self, conn, digest: str):
        cur = conn.execute("SELECT * FROM manifests WHERE digest=?", (digest,))
        return cur.fetchone()

    # -- routes --
    def get_route(self, conn, project_id, corpus_id, environment):
        cur = conn.execute(
            "SELECT * FROM routes WHERE project_id=? AND corpus_id=? AND environment=?",
            (project_id, corpus_id, environment),
        )
        return cur.fetchone()

    def upsert_route(self, conn, project_id, corpus_id, environment, alias_name,
                     collection_name, manifest_digest, generation=1, cache_epoch=1,
                     mode="serving", fence=1):
        conn.execute(
            "INSERT INTO routes(project_id,corpus_id,environment,alias_name,collection_name,manifest_digest,generation,cache_epoch,mode,fence)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(project_id,corpus_id,environment) DO UPDATE SET"
            " alias_name=excluded.alias_name, collection_name=excluded.collection_name,"
            " manifest_digest=excluded.manifest_digest, generation=excluded.generation,"
            " cache_epoch=excluded.cache_epoch, mode=excluded.mode, fence=excluded.fence",
            (project_id, corpus_id, environment, alias_name, collection_name,
             manifest_digest, generation, cache_epoch, mode, fence),
        )

    # -- incidents --
    def transition_incident(self, conn, incident_id: str, expected_state: str,
                            expected_version: int, new_state: str, terminal: int = 0) -> bool:
        """CAS state transition: requires one changed row + event appended by caller."""
        cur = conn.execute(
            "UPDATE incidents SET state=?, state_version=state_version+1, terminal=?, updated_at=?"
            " WHERE id=? AND state=? AND state_version=?",
            (new_state, terminal, _utcnow(), incident_id, expected_state, expected_version),
        )
        return cur.rowcount == 1

    def append_event(self, conn, incident_id: str, kind: str, body: dict) -> int:
        cur = conn.execute(
            "INSERT INTO events(incident_id,kind,body_json,created_at) VALUES(?,?,?,?)",
            (incident_id, kind, json.dumps(body, sort_keys=True), _utcnow()),
        )
        return int(cur.lastrowid)

    def events_since(self, conn, incident_id: str, since_seq: int = 0, limit: int = 200):
        # Cap replay batches (spec p23); never send secrets — callers redact.
        limit = max(1, min(int(limit), 200))
        cur = conn.execute(
            "SELECT seq, kind, body_json, created_at FROM events WHERE incident_id=? AND seq>? ORDER BY seq ASC LIMIT ?",
            (incident_id, since_seq, limit),
        )
        return list(cur.fetchall())

    # -- plans / approvals / operations (spec p8/p19) --
    def get_plan(self, conn, digest: str):
        cur = conn.execute("SELECT * FROM plans WHERE digest=?", (digest,))
        return cur.fetchone()

    def get_approval(self, conn, approval_id: str):
        cur = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,))
        return cur.fetchone()

    def active_approval_for_plan(self, conn, plan_digest: str):
        cur = conn.execute(
            "SELECT * FROM approvals WHERE plan_digest=? AND state='active' ORDER BY created_at DESC LIMIT 1",
            (plan_digest,),
        )
        return cur.fetchone()

    def consume_approval(self, conn, approval_id: str) -> bool:
        cur = conn.execute(
            "UPDATE approvals SET state='consumed' WHERE id=? AND state='active'",
            (approval_id,),
        )
        return cur.rowcount == 1

    def revoke_approval(self, conn, approval_id: str) -> bool:
        cur = conn.execute(
            "UPDATE approvals SET state='revoked' WHERE id=? AND state='active'",
            (approval_id,),
        )
        return cur.rowcount == 1

    def expire_approvals(self, conn, now_iso: str) -> int:
        cur = conn.execute(
            "UPDATE approvals SET state='expired' WHERE state='active' AND expires_at <= ?",
            (now_iso,),
        )
        return cur.rowcount

    def list_operations(self, conn, incident_id: str):
        cur = conn.execute(
            "SELECT * FROM operations WHERE incident_id=? ORDER BY ordinal ASC, updated_at ASC",
            (incident_id,),
        )
        return list(cur.fetchall())

    def unresolved_operations(self, conn, incident_id: str):
        cur = conn.execute(
            "SELECT * FROM operations WHERE incident_id=? AND status IN ('prepared','dispatched','unknown')",
            (incident_id,),
        )
        return list(cur.fetchall())

    def set_route_mode(self, conn, project_id: str, corpus_id: str, environment: str, mode: str):
        if mode not in ("serving", "paused", "verifying", "reconcile"):
            raise ValueError(f"invalid route mode {mode}")
        conn.execute(
            "UPDATE routes SET mode=? WHERE project_id=? AND corpus_id=? AND environment=?",
            (mode, project_id, corpus_id, environment),
        )

    def bump_cache_epoch(self, conn, project_id: str, corpus_id: str, environment: str) -> dict:
        """Cache-only repair: epoch increment in one tx (spec p9/p20)."""
        with self.tx_immediate(conn):
            row = self.get_route(conn, project_id, corpus_id, environment)
            if row is None:
                raise ValueError("no route for epoch bump")
            r = dict(row)
            conn.execute(
                "UPDATE routes SET cache_epoch=cache_epoch+1 WHERE project_id=? AND corpus_id=? AND environment=?",
                (project_id, corpus_id, environment),
            )
            updated = dict(self.get_route(conn, project_id, corpus_id, environment))
        return {"before_epoch": r["cache_epoch"], "after_epoch": updated["cache_epoch"]}

    # -- outbox (spec p22) --
    def outbox_due(self, conn, now_iso: str, limit: int = 20):
        cur = conn.execute(
            "SELECT * FROM memory_outbox WHERE state IN ('pending','retry','submitted')"
            " AND next_attempt_at <= ? ORDER BY next_attempt_at ASC LIMIT ?",
            (now_iso, limit),
        )
        return list(cur.fetchall())

    def outbox_update(self, conn, event_id: str, state: str, attempts: int,
                      next_attempt_at: str, last_error: str | None = None):
        if state not in ("pending", "submitted", "completed", "retry", "dead"):
            raise ValueError(f"invalid outbox state {state}")
        conn.execute(
            "UPDATE memory_outbox SET state=?, attempts=?, next_attempt_at=?, last_error=? WHERE event_id=?",
            (state, attempts, next_attempt_at, last_error, event_id),
        )

    def nonterminal_incidents(self, conn):
        cur = conn.execute("SELECT * FROM incidents WHERE terminal=0 ORDER BY created_at ASC")
        return list(cur.fetchall())
