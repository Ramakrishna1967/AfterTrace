"""Manifest admission validation (spec p5)."""
import hashlib

import pytest

from aftertrace.ingest import deterministic_vector
from aftertrace.models import CanarySpec, ChunkSpec, Manifest


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _chunk(pid, doc, chk, rev, text):
    return ChunkSpec(
        point_id=pid, document_id=doc, chunk_id=chk, revision=rev,
        content_sha256=_sha(text), source_blob_sha256=_sha("blob-" + doc),
    )


def _manifest(chunks, canaries, dim=4):
    return Manifest(
        project_id="sample", corpus_id="sdk-docs", environment="staging",
        revision="B", source_snapshot_sha256=_sha("snap"), pipeline_fingerprint="pipe-1",
        embedding_model_revision="emb-1", vector_size=dim, distance="Cosine",
        chunker_version="c-1", chunks=tuple(chunks), canaries=tuple(canaries),
    )


def test_valid_manifest_and_canary_reference():
    t1 = "auth text vB"
    c1 = _chunk("11111111-1111-4111-8111-111111111111", "authentication", "auth-0001", "B", t1)
    vec = tuple(deterministic_vector(t1, 4))
    can = CanarySpec(query_id="q1", query_vector=vec,
                     expected_point_id=c1.point_id, expected_revision="B",
                     expected_content_sha256=_sha(t1))
    m = _manifest([c1], [can])
    assert m.revision == "B"


def test_duplicate_point_rejected():
    t1 = "hello"
    c1 = _chunk("11111111-1111-4111-8111-111111111111", "d", "c-1", "B", t1)
    c2 = _chunk("11111111-1111-4111-8111-111111111111", "d", "c-2", "B", t1)
    with pytest.raises(Exception):
        _manifest([c1, c2], [])


def test_canary_dim_mismatch_rejected():
    t1 = "hello"
    c1 = _chunk("11111111-1111-4111-8111-111111111111", "d", "c-1", "B", t1)
    can = CanarySpec(query_id="q1", query_vector=(1.0, 2.0),
                     expected_point_id=c1.point_id, expected_revision="B",
                     expected_content_sha256=_sha(t1))
    with pytest.raises(Exception):
        _manifest([c1], [can], dim=4)


def test_nonfinite_vector_rejected():
    with pytest.raises(Exception):
        CanarySpec(query_id="q", query_vector=(float("inf"), 0.0),
                   expected_point_id="x", expected_revision="B",
                   expected_content_sha256=_sha("x"))
