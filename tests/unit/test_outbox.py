"""Outbox lifecycle: pending->submitted->completed/retry/dead (spec p22)."""

import pytest

from aftertrace.db import Database
from aftertrace.outbox import backoff, build_outbox_row, deliver_once, is_retryable


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class _ClientOK:
    def __init__(self, op_status="completed"):
        self.op_status = op_status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _Resp({"operation_id": "op-1"})

    async def get(self, *a, **k):
        return _Resp({"status": self.op_status})


class _ClientAuthFail:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        raise RuntimeError("401 unauthorized")

    async def get(self, *a, **k):
        raise AssertionError("must not poll after auth fail")


def _seed(tmp_path):
    db = Database(str(tmp_path / "ob.sqlite3"))
    conn = db.connect()
    try:
        db.insert_manifest(conn, "m1", "p", "c", "staging", "B", "{}")
        conn.execute(
            "INSERT INTO incidents(id,project_id,corpus_id,environment,desired_manifest,state,"
            "terminal,state_version,request_key,created_at,updated_at)"
            " VALUES('i1','p','c','staging','m1','RESOLVED',1,0,'k','t','t')"
        )
    finally:
        conn.close()
    return db


def _insert(db, row):
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO memory_outbox(event_id,incident_id,document_id,bank_id,payload_json,"
            "payload_sha256,operation_id,state,attempts,next_attempt_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                row["event_id"],
                row["incident_id"],
                row["document_id"],
                row["bank_id"],
                row["payload_json"],
                row["payload_sha256"],
                row["operation_id"],
                "pending",
                0,
                row["next_attempt_at"],
            ),
        )
    finally:
        conn.close()


def test_backoff_caps_and_retryable():
    assert not is_retryable(RuntimeError("401 unauthorized"))
    assert is_retryable(RuntimeError("timeout"))
    assert "T" in backoff(99)


@pytest.mark.asyncio
async def test_deliver_completed(tmp_path):
    db = _seed(tmp_path)
    row = build_outbox_row("i1", "e1", "bank", "summary ok", ["project:p"], {"incident_id": "i1"})
    _insert(db, row)
    state = await deliver_once(db, _ClientOK("completed"), row)
    assert state == "completed"
    conn = db.connect()
    try:
        cur = conn.execute("SELECT state FROM memory_outbox WHERE event_id='e1'")
        assert dict(cur.fetchone())["state"] == "completed"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_auth_failure_dead_letters_with_same_uuid(tmp_path):
    db = _seed(tmp_path)
    row = build_outbox_row("i1", "e2", "bank", "summary", ["project:p"], {"incident_id": "i1"})
    op_before = row["operation_id"]
    _insert(db, row)
    state = await deliver_once(db, _ClientAuthFail(), row)
    assert state == "dead"
    conn = db.connect()
    try:
        cur = conn.execute("SELECT state, operation_id FROM memory_outbox WHERE event_id='e2'")
        r = dict(cur.fetchone())
        assert r["state"] == "dead"
        assert r["operation_id"] == op_before  # never silently generate a new UUID
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_terminal_operation_dead_letters(tmp_path):
    db = _seed(tmp_path)
    row = build_outbox_row("i1", "e3", "bank", "summary", ["project:p"], {"incident_id": "i1"})
    _insert(db, row)
    state = await deliver_once(db, _ClientOK("failed"), row)
    assert state == "dead"
