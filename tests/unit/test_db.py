"""DB CAS transition + outbox atomicity (spec p7-8, p22)."""

from aftertrace.db import Database


def test_cas_transition(tmp_path):
    db = Database(str(tmp_path / "t.sqlite3"))
    conn = db.connect()
    try:
        db.insert_manifest(conn, "d1", "p", "c", "staging", "B", "{}")
        conn.execute(
            "INSERT INTO incidents(id,project_id,corpus_id,environment,"
            "desired_manifest,state,terminal,state_version,"
            "request_key,created_at,updated_at)"
            " VALUES('i1','p','c','staging','d1','DIAGNOSING',0,0,'k1','t','t')"
        )
        ok = db.transition_incident(conn, "i1", "DIAGNOSING", 0, "PLAN_READY")
        assert ok
        # stale version must fail
        ok2 = db.transition_incident(conn, "i1", "DIAGNOSING", 0, "PLAN_READY")
        assert not ok2
    finally:
        conn.close()


def test_outbox_row_unique(tmp_path):
    db = Database(str(tmp_path / "t2.sqlite3"))
    conn = db.connect()
    try:
        db.insert_manifest(conn, "d1", "p", "c", "staging", "B", "{}")
        conn.execute(
            "INSERT INTO incidents(id,project_id,corpus_id,environment,"
            "desired_manifest,state,terminal,state_version,"
            "request_key,created_at,updated_at)"
            " VALUES('i1','p','c','staging','d1','VERIFYING',0,0,'k1','t','t')"
        )
        conn.execute(
            "INSERT INTO memory_outbox(event_id,incident_id,document_id,bank_id,"
            "payload_json,payload_sha256,operation_id,state,attempts,next_attempt_at)"
            " VALUES('e1','i1','doc-1','bank','{}','abc','op-1','pending',0,'t')"
        )
        import sqlite3

        try:
            conn.execute(
                "INSERT INTO memory_outbox(event_id,incident_id,document_id,bank_id,"
                "payload_json,payload_sha256,operation_id,state,attempts,next_attempt_at)"
                " VALUES('e2','i1','doc-1','bank','{}','abc','op-2','pending',0,'t')"
            )
            raise AssertionError("duplicate document_id should fail")
        except sqlite3.IntegrityError:
            pass
    finally:
        conn.close()
