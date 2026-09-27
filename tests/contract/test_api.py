"""Contract tests — HTTP boundary shapes (spec p23)."""
import pytest
from fastapi.testclient import TestClient

from aftertrace.app import build_app
from aftertrace.db import Database
from aftertrace.settings import Settings


@pytest.fixture()
def client(tmp_path):
    settings = Settings(database_path=str(tmp_path / "c.sqlite3"))
    db = Database(settings.database_path)
    app = build_app(settings, db)
    with TestClient(app) as c:
        yield c


def _manifest_body():
    import hashlib

    from aftertrace.ingest import deterministic_vector

    t = "contract text vB"
    h = hashlib.sha256(t.encode()).hexdigest()
    pid = "6772ecde-fd54-4c5d-9f2d-b51b00361032"
    vec = deterministic_vector(t, 4)
    return {
        "project_id": "sample", "corpus_id": "sdk-docs", "environment": "staging",
        "revision": "B", "source_snapshot_sha256": h, "pipeline_fingerprint": "p",
        "embedding_model_revision": "e", "vector_size": 4, "distance": "Cosine",
        "chunker_version": "c",
        "chunks": [{"point_id": pid, "document_id": "authentication", "chunk_id": "auth-0001",
                    "revision": "B", "content_sha256": h, "source_blob_sha256": h}],
        "canaries": [{"query_id": "q1", "query_vector": vec, "expected_point_id": pid,
                      "expected_revision": "B", "expected_content_sha256": h, "top_k": 3}],
    }


def test_manifest_and_incident_idempotency(client):
    m = _manifest_body()
    r = client.post("/v1/manifests", json=m, headers={"x-project": "sample"})
    assert r.status_code == 201, r.text
    digest = r.json()["digest"]
    h = {"x-project": "sample", "Idempotency-Key": "k-1"}
    r1 = client.post("/v1/incidents", json={"corpus_id": "sdk-docs", "desired_manifest_digest": digest}, headers=h)
    assert r1.status_code == 202, r1.text
    r2 = client.post("/v1/incidents", json={"corpus_id": "sdk-docs", "desired_manifest_digest": digest}, headers=h)
    assert r2.status_code == 202
    assert r1.json()["incident_id"] == r2.json()["incident_id"]
    # reuse key with different digest -> 409
    r3 = client.post("/v1/incidents", json={"corpus_id": "sdk-docs", "desired_manifest_digest": "other"}, headers=h)
    assert r3.status_code == 409


def test_diagnostics_rejects_unsupported_action(client):
    m = _manifest_body()
    d = client.post("/v1/manifests", json=m, headers={"x-project": "sample"}).json()["digest"]
    inc = client.post("/v1/incidents", json={"corpus_id": "sdk-docs", "desired_manifest_digest": d},
                      headers={"x-project": "sample", "Idempotency-Key": "k-9"}).json()["incident_id"]
    r = client.post(f"/v1/incidents/{inc}/diagnostics", json={"action": "rm -rf"})
    assert r.status_code == 422
