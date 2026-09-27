"""Evaluation protocol — contribution of memory (spec p27).

Frozen-bank ablations with identical model/tools/permissions/verifier/budget.
The deterministic verifier is the outcome oracle; an LLM judging its own
repair is never evidence. Raw result files are written for reproducibility.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from datetime import UTC, datetime


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


CONDITIONS = ("stateless", "static_runbook", "raw_log", "hindsight")


def score_cases(cases: list[dict]) -> dict:
    total = max(1, len(cases))
    verified = sum(1 for c in cases if c.get("recovered")) / total
    unjust = (
        sum(1 for c in cases if c.get("writes", 0) > 0 and not c.get("justified", True)) / total
    )
    acc = sum(1 for c in cases if c.get("fault") == c.get("pred")) / total
    tool_calls = sum(int(c.get("tool_calls", 0)) for c in cases)
    return {
        "verified_recovery_rate": verified,
        "unnecessary_write_rate": unjust,
        "diagnosis_accuracy": acc,
        "tool_calls": tool_calls,
        "n": len(cases),
        "computed_at": _utcnow(),
    }


def run_frozen_bank(cases: list[dict], bank_id: str, out_path: str | None = None) -> dict:
    """Score one frozen condition; write raw JSON (never fabricated gains).

    cases: [{id, fault, pred, recovered, writes, justified, tool_calls, latency_s, cost}]
    """
    metrics = score_cases(cases)
    result = {
        "bank_id": bank_id,
        "cases": cases,
        "metrics": metrics,
        "oracle": "deterministic_verifier",
        "note": "LLM self-judgment is not evidence of recovery.",
    }
    if out_path:
        p = pathlib.Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def dataset_identity(fixtures_dir: str = "fixtures") -> str:
    """Record fixture seed/manifests/python lockfile versions for reproducibility."""
    h = hashlib.sha256()
    base = pathlib.Path(fixtures_dir)
    for f in sorted(base.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(base)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()
