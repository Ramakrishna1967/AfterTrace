"""Cutover protocol + crash matrix (spec p21)."""
import json

import pytest

from aftertrace.cutover import Cutover, rollback_guards
from aftertrace.db import Database
from aftertrace.execute import build_publish_plan, require_approval
from aftertrace.models import plan_digest


def _db(tmp_path):
    return Database(str(tmp_path / "cut.sqlite3"))


def _plan(before):
    from datetime import datetime, timedelta, timezone
    return build_publish_plan("inc-1", {"project": "p", "corpus": "c", "environment": "staging"},
                              "m1", "s1", before, "p_c_build_B",
                              (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())


def test_prepare_rejects_unresolved_prior_write(tmp_path):
    db = _db(tmp_path)
    conn = db.connect()
    try:
        db.insert_manifest(conn, "m1", "p", "c", "staging", "B", "{}")
        db.upsert_route(conn, "p", "c", "staging", "a", "p_c_build_A", "m1")
        conn.execute("INSERT INTO incidents(id,project_id,corpus_id,environment,desired_manifest,state,terminal,state_version,request_key,created_at,updated_at)"
                     " VALUES('inc-1','p','c','staging','m1','DIAGNOSING',0,0,'k','t','t')")
        before = {"alias": "a", "collection": "p_c_build_A", "generation": 1, "cache_epoch": 1, "fence": 1}
        body = _plan(before)
        body["_digest"] = plan_digest(body)
        # operations.plan_digest FK -> plans(digest): seed the plan row first
        with db.tx_immediate(conn):
            conn.execute("INSERT INTO plans(digest,incident_id,body_json,created_at) VALUES(?,?,?,?)",
                         (body["_digest"], "inc-1", json.dumps(body), "t"))
            conn.execute("INSERT INTO operations(id,incident_id,plan_digest,ordinal,kind,status,request_json,before_json,updated_at)"
                         " VALUES('op-old','inc-1',?,0,'publish_sealed_index','dispatched','{}','{}','t')",
                         (body["_digest"],))
        cut = Cutover(db)
        approval = {"state": "active",
                    "expires_at": body["not_after"], "plan_digest": body["_digest"]}
        with pytest.raises(Exception):
            cut.prepare(conn, "inc-1", body, approval)
    finally:
        conn.close()


def test_rollback_guards():
    ok, _ = rollback_guards({"rollback_allowed": False, "after": "B"}, "B", True)
    assert not ok
    ok2, _ = rollback_guards({"rollback_allowed": True, "after": "B"}, "C", True)
    assert not ok2
    ok3, _ = rollback_guards({"rollback_allowed": True, "after": "B"}, "B", True)
    assert ok3


def test_crash_matrix_actions(tmp_path):
    cut = Cutover(_db(tmp_path))
    assert "unknown" in cut.crash_action("after_dispatch_no_ack").lower()
    assert "reconcile" in cut.crash_action("after_alias_before_route").lower()
