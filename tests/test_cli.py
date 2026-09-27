"""Regression tests: fixtures deterministic, verification exact, rejection gate logic.

Run: python -m pytest tests/test_cli.py -q
"""
import math
import sqlite3

from cli import fixtures, sqlite_log
from cli.agent import gateway_query, verify_target_collection
from cli.config import Settings
from cli.qdrant_store import QdrantStore
from cli.vectors import content_sha256, fixture_vector


def _local_settings(tmp_path) -> Settings:
    return Settings(
        qdrant_url=None,
        qdrant_api_key=None,
        hindsight_base_url=None,
        hindsight_api_key=None,
        bank_id="test-bank",
        sqlite_path=str(tmp_path / "t.sqlite3"),
    )


def test_fixture_vectors_deterministic_and_unit():
    a = fixture_vector("hello")
    b = fixture_vector("hello")
    c = fixture_vector("different")
    assert a == b
    assert a != c
    assert abs(math.sqrt(sum(x * x for x in a)) - 1.0) < 1e-9
    assert all(math.isfinite(x) for x in a)
    assert len(content_sha256("x")) == 64


def test_verify_target_exact(tmp_path):
    s = _local_settings(tmp_path)
    store = QdrantStore(s, force_local=True)
    corpus = fixtures.corpus_s1()
    pts = [
        {"id": c["point_id"], "vector": c["vec_b"],
         "payload": {"document_id": c["doc_id"], "revision": "B",
                     "content_sha256": c["sha_b"], "text": "x"}}
        for c in corpus["chunks"]
    ]
    store.recreate_collection("col_b")
    store.upsert("col_b", pts)
    ok, errs = verify_target_collection(store, "col_b", corpus)
    assert ok, errs


def test_verify_target_rejects_wrong_revision(tmp_path):
    s = _local_settings(tmp_path)
    store = QdrantStore(s, force_local=True)
    corpus = fixtures.corpus_s1()
    pts = [
        {"id": c["point_id"], "vector": c["vec_a"],
         "payload": {"document_id": c["doc_id"], "revision": "A",
                     "content_sha256": c["sha_a"], "text": "x"}}
        for c in corpus["chunks"]
    ]
    store.recreate_collection("col_wrong")
    store.upsert("col_wrong", pts)
    ok, errs = verify_target_collection(store, "col_wrong", corpus)
    assert not ok
    assert errs


def test_alias_switch_requires_expected_old(tmp_path):
    s = _local_settings(tmp_path)
    store = QdrantStore(s, force_local=True)
    store.ensure_alias("live", "col_a")
    try:
        store.switch_alias("live", "col_WRONG", "col_b")
    except ValueError:
        pass
    else:
        raise AssertionError("switch must fail when expected_old mismatches (no blind overwrite)")
    assert store.get_alias_target("live") == "col_a"


def test_stale_cache_masks_correct_alias(tmp_path):
    """S3 core: alias correct but query returns stale A; rejection must trigger."""
    s = _local_settings(tmp_path)
    store = QdrantStore(s, force_local=True)
    corpus = fixtures.corpus_s3()
    pts_a = [{"id": c["point_id"], "vector": c["vec_a"],
              "payload": {"document_id": c["doc_id"], "revision": "A",
                          "content_sha256": c["sha_a"], "text": "x"}} for c in corpus["chunks"]]
    pts_b = [{"id": c["point_id"], "vector": c["vec_b"],
              "payload": {"document_id": c["doc_id"], "revision": "B",
                          "content_sha256": c["sha_b"], "text": "x"}} for c in corpus["chunks"]]
    store.recreate_collection("a3")
    store.upsert("a3", pts_a)
    store.recreate_collection("b3")
    store.upsert("b3", pts_b)
    con = sqlite_log.connect(s.sqlite_path)
    sqlite_log.set_alias(con, "live3", "b3")  # CORRECT
    store.ensure_alias("live3", "b3")
    canary = fixtures.canary_for(corpus, corpus["chunks"][0]["doc_id"])
    sqlite_log.set_cache(con, "k1", "A", canary["expected_point_id"], stale=1)
    got = gateway_query(con, store, "live3", "k1", list(canary["query_vector"]))
    assert got["revision"] == "A" and got["via"] == "stale-cache"
    # After invalidation, live B must show through with alias untouched.
    sqlite_log.clear_cache(con, "k1")
    got2 = gateway_query(con, store, "live3", "k1", list(canary["query_vector"]))
    assert got2["revision"] == "B"
    assert sqlite_log.get_alias(con, "live3") == "b3"
    con.close()
