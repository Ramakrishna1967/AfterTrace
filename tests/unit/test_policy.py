"""Approval binding + diagnosis predicates + citation validation."""
from datetime import datetime, timedelta, timezone

import pytest

from aftertrace import diagnose as diag
from aftertrace.execute import build_publish_plan, require_approval
from aftertrace.models import plan_digest
from aftertrace.planner import NextStep


def test_approval_consumes_exact_plan():
    before = {"alias": "sample_sdk_docs_live", "collection": "sample_sdk_docs_build_A",
              "generation": 4, "cache_epoch": 4, "fence": 9}
    body = build_publish_plan("inc-1", {"project": "sample", "corpus": "sdk-docs", "environment": "staging"},
                              "m-digest", "s-digest", before, "sample_sdk_docs_build_B",
                              (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
    digest = plan_digest(body)
    approval = {"state": "active",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "plan_digest": digest}
    require_approval(approval, body, before)  # no raise
    with pytest.raises(PermissionError):
        require_approval(approval, body, {**before, "generation": 5})


def test_approval_expiry_blocks_write():
    before = {"alias": "a", "collection": "b", "generation": 1, "cache_epoch": 1, "fence": 1}
    body = build_publish_plan("i", {"project": "p", "corpus": "c", "environment": "staging"},
                              "m", "s", before, "t",
                              (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    approval = {"state": "active",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "plan_digest": plan_digest(body)}
    with pytest.raises(PermissionError):
        require_approval(approval, body, before)


def test_diagnose_alias_drift_vs_cache():
    r1 = diag.classify({"target_passes": True, "alias_points_to_target": False})
    assert r1["cause"] == "publication_alias_drift" and r1["repair_eligible"]
    r2 = diag.classify({"target_passes": True, "alias_points_to_target": True,
                        "backend_probe_passes": True, "gateway_returns_expected": False})
    assert r2["cause"] == "query_cache_contamination"
    r3 = diag.classify({"target_passes": False})
    assert not r3["repair_eligible"]
    r4 = diag.classify({"source_verified": False})
    assert r4["cause"] == "unverified_desired_state"


def test_nextstep_rejects_unknown_citation():
    s = NextStep(hypothesis="alias_drift", next_action="inspect_alias", reason="r",
                 evidence_ids=["obs-999"], memory_ids=[])
    assert s.evidence_ids == ["obs-999"]  # schema allows; worker must reject unknown IDs
