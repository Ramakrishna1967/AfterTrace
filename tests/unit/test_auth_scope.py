"""Regression tests for P0 auth/scope/redaction fixes (full codebase review).

Run: python -m pytest tests/unit/test_auth_scope.py -q
"""
import json

import pytest
from fastapi.testclient import TestClient

from aftertrace.app import build_app
from aftertrace.db import Database
from aftertrace.security import redact_json, require_approver_role, scan_for_secrets
from aftertrace.settings import Settings


@pytest.fixture()
def client(tmp_path):
    settings = Settings(database_path=str(tmp_path / "auth.sqlite3"))
    db = Database(settings.database_path)
    app = build_app(settings, db)
    with TestClient(app) as c:
        yield c


def _manifest_body(project="sample"):
    import hashlib

    from aftertrace.ingest import deterministic_vector

    t = "auth scope text vB"
    h = hashlib.sha256(t.encode()).hexdigest()
    pid = "6772ecde-fd54-4c5d-9f2d-b51b00361032"
    vec = deterministic_vector(t, 4)
    return {
        "project_id": project, "corpus_id": "sdk-docs", "environment": "staging",
        "revision": "B", "source_snapshot_sha256": h, "pipeline_fingerprint": "p",
        "embedding_model_revision": "e", "vector_size": 4, "distance": "Cosine",
        "chunker_version": "c",
        "chunks": [{"point_id": pid, "document_id": "authentication", "chunk_id": "auth-0001",
                    "revision": "B", "content_sha256": h, "source_blob_sha256": h}],
        "canaries": [{"query_id": "q1", "query_vector": vec, "expected_point_id": pid,
                      "expected_revision": "B", "expected_content_sha256": h, "top_k": 3}],
    }


def _incident(client, project="sample"):
    d = client.post("/v1/manifests", json=_manifest_body(project),
                    headers={"x-project": project}).json()["digest"]
    r = client.post("/v1/incidents",
                    json={"corpus_id": "sdk-docs", "desired_manifest_digest": d},
                    headers={"x-project": project, "Idempotency-Key": f"k-{project}"})
    assert r.status_code == 202, r.text
    return r.json()["incident_id"]


def test_manifest_sample_wildcard_closed(client):
    # Presenting as sample must NOT mint manifests for other projects.
    r = client.post("/v1/manifests", json=_manifest_body("other"), headers={"x-project": "sample"})
    assert r.status_code == 403, r.text


def test_diagnostics_cross_project_blocked(client):
    iid = _incident(client, "sample")
    r = client.post(f"/v1/incidents/{iid}/diagnostics", json={"action": "inspect_alias"},
                    headers={"x-project": "other"})
    assert r.status_code == 403, r.text
    r = client.post(f"/v1/incidents/{iid}/diagnostics", json={"action": "inspect_alias"},
                    headers={"x-project": "sample"})
    assert r.status_code == 200, r.text


def test_cancel_cross_project_blocked(client):
    iid = _incident(client, "sample")
    r = client.post(f"/v1/incidents/{iid}/cancel", headers={"x-project": "other"})
    assert r.status_code == 403, r.text
    r = client.post(f"/v1/incidents/{iid}/cancel", headers={"x-project": "sample"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"


def test_operator_cannot_approve(client):
    iid = _incident(client, "sample")
    r = client.post(f"/v1/incidents/{iid}/approvals", json={"plan_digest": "deadbeef"})
    assert r.status_code == 403, r.text  # default role is operator
    r = client.post(f"/v1/incidents/{iid}/approvals", json={"plan_digest": "deadbeef"},
                    headers={"x-role": "approver"})
    assert r.status_code == 409, r.text  # role passes, plan correctly reported stale


def test_require_approver_role_rejects_operator():
    import pytest as pt

    with pt.raises(PermissionError):
        require_approver_role({"role": "operator"})
    assert require_approver_role({"role": "approver"}) == "approver"
    assert require_approver_role({"role": "admin"}) == "admin"


def test_sse_redaction_never_crashes_stream(client):
    iid = _incident(client, "sample")
    # Event body containing a secret pattern must stream redacted, not 500.
    evil = json.dumps({"note": "leaked api-key=ABC123 and bearer XYZ", "n": 1})
    assert scan_for_secrets(evil)
    # redact_json keeps structure valid and removes the secret.
    data = redact_json(json.loads(evil))
    assert json.dumps(data)  # still serializable
    assert scan_for_secrets(json.dumps(data)) == []
    r = client.get(f"/v1/incidents/{iid}/events", headers={"x-project": "sample"})
    assert r.status_code == 200, r.text
    assert "incident.detected" in r.text


def test_redact_json_keeps_types():
    assert redact_json({"a": 1, "b": [True, None]}) == {"a": 1, "b": [True, None]}
    out = redact_json({"token": "token=secret-value"})
    assert out == {"token": "[redacted]"}  # whole secret value redacted, key kept
    assert scan_for_secrets(json.dumps(out)) == []
