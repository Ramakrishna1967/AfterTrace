"""Evaluation harness — metrics + frozen-bank runner (spec p27)."""
from aftertrace.eval import dataset_identity, run_frozen_bank


def test_frozen_bank_metrics_and_raw_file(tmp_path):
    cases = [
        {"id": "stale-publication", "fault": "alias_drift", "pred": "alias_drift",
         "recovered": True, "writes": 1, "justified": True, "tool_calls": 4},
        {"id": "contaminated-cache", "fault": "cache", "pred": "cache",
         "recovered": True, "writes": 0, "justified": True, "tool_calls": 3},
        {"id": "incomplete-target", "fault": "ingestion", "pred": "ingestion",
         "recovered": False, "writes": 0, "justified": True, "tool_calls": 2},
        {"id": "wrong-memory", "fault": "cache", "pred": "alias_drift",
         "recovered": False, "writes": 1, "justified": False, "tool_calls": 5},
    ]
    out = str(tmp_path / "eval_raw.json")
    res = run_frozen_bank(cases, bank_id="eval-bank-frozen", out_path=out)
    assert res["metrics"]["verified_recovery_rate"] == 0.5
    assert res["metrics"]["unnecessary_write_rate"] == 0.25
    assert res["oracle"] == "deterministic_verifier"
    import json
    assert json.loads(open(out).read())["metrics"]["n"] == 4


def test_dataset_identity_stable(tmp_path):
    (tmp_path / "a.json").write_text('{"x":1}')
    h1 = dataset_identity(str(tmp_path))
    h2 = dataset_identity(str(tmp_path))
    assert h1 == h2 and len(h1) == 64
