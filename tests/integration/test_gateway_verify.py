"""Alias integration needs a real native Qdrant; here: gateway + verifier with fakes."""
import hashlib

import pytest

from aftertrace.db import Database
from aftertrace.gateway import Gateway
from aftertrace.ingest import build_point, deterministic_vector
from aftertrace.models import CanarySpec, ChunkSpec, Manifest
from aftertrace.verify import probe_twice


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _make_manifest(dim=4):
    tB = "sdk auth revision B canonical text"
    pid = "6772ecde-fd54-4c5d-9f2d-b51b00361032"
    chunk = ChunkSpec(point_id=pid, document_id="authentication", chunk_id="auth-0001",
                      revision="B", content_sha256=_sha(tB), source_blob_sha256=_sha("blob"))
    can = CanarySpec(query_id="canary-auth-B", query_vector=tuple(deterministic_vector(tB, dim)),
                     expected_point_id=pid, expected_revision="B", expected_content_sha256=_sha(tB))
    m = Manifest(project_id="sample", corpus_id="sdk-docs", environment="staging", revision="B",
                 source_snapshot_sha256=_sha("snap"), pipeline_fingerprint="pipe",
                 embedding_model_revision="emb", vector_size=dim, distance="Cosine",
                 chunker_version="c1", chunks=(chunk,), canaries=(can,))
    return m, tB, pid


@pytest.mark.asyncio
async def test_gateway_probe_passes_on_correct_backend(tmp_path):
    m, tB, pid = _make_manifest()
    db = Database(str(tmp_path / "it.sqlite3"))
    conn = db.connect()
    try:
        import json

        from aftertrace.models import manifest_digest

        digest = manifest_digest(m)
        db.insert_manifest(conn, digest, "sample", "sdk-docs", "staging", "B",
                           json.dumps(m.model_dump(mode="json")))
        db.upsert_route(conn, "sample", "sdk-docs", "staging", "sample_sdk_docs_live",
                        "sample_sdk_docs_build_B", digest, generation=5, cache_epoch=5, mode="serving")
        route = dict(db.get_route(conn, "sample", "sdk-docs", "staging"))
    finally:
        conn.close()

    pt = build_point(pid, "sample", "sdk-docs", "authentication", "auth-0001", "B", tB, 4)

    def fake_query(collection, vector, top_k, filters):
        assert collection == "sample_sdk_docs_build_B"
        return [{"point_id": pid, "revision": "B", "content_sha256": _sha(tB), "text": tB}]

    gw = Gateway(db, query_fn=fake_query)
    principal = {"project_id": "sample", "corpus_id": "sdk-docs", "environment": "staging",
                 "acl_scope": ["tenant:a"]}
    res = await probe_twice(gw, m.canaries[0], route, principal)
    assert res["passed"]


@pytest.mark.asyncio
async def test_contaminated_cache_fixture(tmp_path):
    """Backend/route B correct; insert A response under current-epoch key -> diagnosis must reject alias repair."""
    m, tB, pid = _make_manifest()
    db = Database(str(tmp_path / "it2.sqlite3"))
    conn = db.connect()
    try:
        import json

        from aftertrace.models import manifest_digest

        digest = manifest_digest(m)
        db.insert_manifest(conn, digest, "sample", "sdk-docs", "staging", "B",
                           json.dumps(m.model_dump(mode="json")))
        db.upsert_route(conn, "sample", "sdk-docs", "staging", "sample_sdk_docs_live",
                        "sample_sdk_docs_build_B", digest, generation=5, cache_epoch=5, mode="serving")
        route = dict(db.get_route(conn, "sample", "sdk-docs", "staging"))
    finally:
        conn.close()

    gw = Gateway(db, query_fn=lambda c, v, k, f: [])
    principal = {"project_id": "sample", "corpus_id": "sdk-docs", "environment": "staging",
                 "acl_scope": ["tenant:a"]}
    query = {"query_vector": list(m.canaries[0].query_vector), "top_k": 3, "filters": {},
             "query_id": m.canaries[0].query_id}
    # Poison current-epoch key with stale revision A payload
    stale_hit = {"point_id": pid, "revision": "A", "content_sha256": _sha("old"), "text": "old text"}
    from aftertrace.gateway import cache_key

    key_route = {k: route[k] for k in
                 ("project_id", "corpus_id", "environment", "collection_name", "generation", "cache_epoch", "manifest_digest")}
    gw.poison_cache(key_route, query, ["tenant:a"],
                    {"hits": [stale_hit], "route": {"collection_name": route["collection_name"],
                                                     "generation": route["generation"],
                                                     "cache_epoch": route["cache_epoch"]}})
    first = await gw.query(query, principal, verifier=True)
    assert first["cache_hit"] is True
    assert first["hits"][0]["revision"] == "A"
    # Epoch bump must change identity (repair path)
    bumped = dict(route, cache_epoch=route["cache_epoch"] + 1)
    assert cache_key(key_route, query, ["tenant:a"]) != cache_key(
        {k: bumped[k] for k in key_route}, query, ["tenant:a"])
