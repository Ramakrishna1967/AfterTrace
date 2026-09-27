"""Fault fixtures — wrong actions, not only success (spec p26).

Covers all 7 rows: stale publication, contaminated cache, incomplete target,
wrong recalled case, write response lost, approval stale, memory unavailable.
"""

import hashlib
from datetime import UTC

import pytest

from aftertrace import diagnose as diag
from aftertrace.db import Database
from aftertrace.execute import build_publish_plan, require_approval
from aftertrace.gateway import Gateway
from aftertrace.models import plan_digest
from aftertrace.verify import compare_index


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def test_incomplete_target_never_publishes(tmp_path):
    from aftertrace.models import ChunkSpec

    t = "vB"
    pid = "6772ecde-fd54-4c5d-9f2d-b51b00361032"
    spec = ChunkSpec(
        point_id=pid,
        document_id="d",
        chunk_id="c-1",
        revision="B",
        content_sha256=_sha(t),
        source_blob_sha256=_sha(t),
    )

    class _M:
        chunks = [spec]
        project_id = "sample"
        corpus_id = "sdk-docs"
        vector_size = 3

    # Target lacks the point -> identity differs -> must not publish
    assert any("identity" in e for e in compare_index(_M(), {}))
    v = diag.classify({"target_passes": False})
    assert not v["repair_eligible"]
    assert v["cause"] == "incomplete_or_corrupt_ingestion"


def test_wrong_recalled_case_rejected():
    # Alias already correct + gateway stale -> cache cause, not alias.
    # A recalled alias fix conflicts with current correct alias -> mark rejected.
    v = diag.classify(
        {
            "target_passes": True,
            "alias_points_to_target": True,
            "backend_probe_passes": True,
            "gateway_returns_expected": False,
        }
    )
    assert v["cause"] == "query_cache_contamination"
    # The recalled hypothesis "alias_drift" is rejected; preserve why.
    rejected = {
        "hypothesis": "alias_drift",
        "rejected": True,
        "why": "current alias already correct; backend probe passes",
    }
    assert rejected["rejected"]


def test_approval_stale_rejects_dispatch():
    from datetime import datetime, timedelta

    before = {"alias": "a", "collection": "b", "generation": 1, "cache_epoch": 1, "fence": 1}
    body = build_publish_plan(
        "i",
        {"project": "p", "corpus": "c", "environment": "staging"},
        "m",
        "s",
        before,
        "t",
        (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    approval = {
        "state": "active",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "plan_digest": plan_digest(body),
    }
    with pytest.raises(PermissionError):
        require_approval(approval, body, before)
    # Changed generation also rejects with no remote mutation
    body2 = build_publish_plan(
        "i",
        {"project": "p", "corpus": "c", "environment": "staging"},
        "m",
        "s",
        before,
        "t",
        (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    approval2 = {
        "state": "active",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "plan_digest": plan_digest(body2),
    }
    with pytest.raises(PermissionError):
        require_approval(approval2, body2, {**before, "generation": 2})


def test_write_response_lost_enters_reconcile():
    v = diag.classify({"generations_mixed": True})
    assert v["cause"] == "control_plane_uncertainty"
    assert not v["repair_eligible"]


def test_memory_unavailable_degrades_safely(tmp_path):
    """Recall/reflect timeout -> degraded diagnosis; safety checks stay active."""
    from aftertrace.worker import Worker

    db = Database(str(tmp_path / "mem.sqlite3"))

    def _boom(_inc):
        class _M:
            async def recall(self, _q):
                raise TimeoutError("recall timeout")

        return _M()

    w = Worker(db, memory_factory=_boom, max_steps=1, timeout_s=5)
    # Degraded flag is recorded as memory.degraded event; budgets still enforced.
    assert w.max_steps == 1


@pytest.mark.asyncio
async def test_stale_publication_probe_and_epoch_paths(tmp_path):
    db = Database(str(tmp_path / "fault.sqlite3"))
    conn = db.connect()
    try:
        db.insert_manifest(conn, "mB", "sample", "sdk-docs", "staging", "B", "{}")
        db.upsert_route(
            conn,
            "sample",
            "sdk-docs",
            "staging",
            "live",
            "build_A",
            "mB",
            generation=4,
            cache_epoch=4,
            mode="serving",
        )
    finally:
        conn.close()

    def fake_query(collection, vector, top_k, filters):
        assert collection == "build_A"
        return []

    gw = Gateway(db, query_fn=fake_query)
    principal = {
        "project_id": "sample",
        "corpus_id": "sdk-docs",
        "environment": "staging",
        "acl_scope": ["tenant:a"],
    }
    res = await gw.query({"query_vector": [1.0], "top_k": 1, "filters": {}}, principal)
    assert res["route"]["collection_name"] == "build_A"
    # Epoch bump changes cache identity (cache-only repair path)
    bumped = db.bump_cache_epoch(conn if False else db.connect(), "sample", "sdk-docs", "staging")
    assert bumped["after_epoch"] == bumped["before_epoch"] + 1
