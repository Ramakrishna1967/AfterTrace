"""Evaluation harness — frozen-bank ablations (spec p27). Records raw result files."""

import json
import pathlib


def test_evaluation_metric_definitions(tmp_path):
    cases = [
        {
            "id": "stale-publication",
            "recovered": True,
            "writes": 1,
            "justified": True,
            "fault": "alias_drift",
            "pred": "alias_drift",
        },
        {
            "id": "contaminated-cache",
            "recovered": True,
            "writes": 0,
            "justified": True,
            "fault": "cache",
            "pred": "cache",
        },
        {
            "id": "incomplete-target",
            "recovered": False,
            "writes": 0,
            "justified": True,
            "fault": "ingestion",
            "pred": "ingestion",
        },
        {
            "id": "wrong-memory",
            "recovered": False,
            "writes": 1,
            "justified": False,
            "fault": "cache",
            "pred": "alias_drift",
        },
    ]
    total = len(cases)
    verified = sum(1 for c in cases if c["recovered"]) / total
    unjust = sum(1 for c in cases if not c["justified"]) / total
    acc = sum(1 for c in cases if c["fault"] == c["pred"]) / total
    out = {
        "verified_recovery_rate": verified,
        "unnecessary_write_rate": unjust,
        "diagnosis_accuracy": acc,
        "total_cost": "tbd-provider",
        "n": total,
    }
    p = pathlib.Path(tmp_path) / "eval_raw.json"
    p.write_text(json.dumps(out, indent=2))
    assert out["verified_recovery_rate"] == 0.5
    assert out["unnecessary_write_rate"] == 0.25
