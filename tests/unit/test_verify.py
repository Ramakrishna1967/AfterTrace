"""Exact-ID comparison, payload/vector checks (spec p10)."""

import hashlib

from aftertrace.ingest import deterministic_vector
from aftertrace.models import ChunkSpec
from aftertrace.verify import check_canary, compare_index


class _M:
    def __init__(self, chunks, dim=3):
        self.chunks = chunks
        self.project_id = "sample"
        self.corpus_id = "sdk-docs"
        self.vector_size = dim


def _point(pid, doc, chk, rev, text, dim=3):
    return {
        "id": pid,
        "vector": deterministic_vector(text, dim),
        "payload": {
            "project_id": "sample",
            "corpus_id": "sdk-docs",
            "document_id": doc,
            "chunk_id": chk,
            "revision": rev,
            "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "text": text,
        },
    }


def _spec(pid, doc, chk, rev, text):
    h = hashlib.sha256(text.encode()).hexdigest()
    return ChunkSpec(
        point_id=pid,
        document_id=doc,
        chunk_id=chk,
        revision=rev,
        content_sha256=h,
        source_blob_sha256=h,
    )


def test_compare_clean():
    t = "exact bytes vB"
    pid = "6772ecde-fd54-4c5d-9f2d-b51b00361032"
    m = _M([_spec(pid, "authentication", "auth-0001", "B", t)])
    assert compare_index(m, {pid: _point(pid, "authentication", "auth-0001", "B", t)}) == []


def test_compare_detects_missing_and_corrupt():
    t = "vB"
    pid = "6772ecde-fd54-4c5d-9f2d-b51b00361032"
    m = _M([_spec(pid, "d", "c-1", "B", t)])
    assert any("identity" in e for e in compare_index(m, {}))
    bad = _point(pid, "d", "c-1", "A", t)  # wrong revision
    assert any("payload" in e for e in compare_index(m, {pid: bad}))


def test_canary_requires_route_and_revision():
    import hashlib as _h

    t = "canary text"
    h = _h.sha256(t.encode()).hexdigest()
    pid = "p1"

    class _C:
        expected_point_id = pid
        expected_revision = "B"
        expected_content_sha256 = h

    route = {"collection_name": "build_B", "generation": 5, "cache_epoch": 5}
    resp = {
        "route": dict(route),
        "hits": [{"point_id": pid, "revision": "B", "content_sha256": h, "text": t}],
    }
    ok, _ = check_canary(resp, _C(), route)
    assert ok
    bad_route = dict(route, generation=4)
    ok2, msg = check_canary({"route": bad_route, "hits": resp["hits"]}, _C(), route)
    assert not ok2 and "routing" in msg
