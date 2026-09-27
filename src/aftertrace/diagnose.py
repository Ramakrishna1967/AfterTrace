"""Deterministic diagnostic predicates (spec p12).

The agent orders checks; these predicates decide admissibility.
Each observation carries timestamp, scope, route generation, source tool, digest.
"""
from __future__ import annotations


ALLOWED_ACTIONS = (
    "inspect_alias",
    "inspect_route",
    "verify_target",
    "probe_gateway",
    "inspect_cache",
    "verify_source",
    "propose_repair",
    "escalate",
)


def classify(observations: dict) -> dict:
    """Map observed state -> candidate cause + required next check.

    observations keys (booleans / strings supplied by worker tools):
      target_passes, alias_points_to_target, route_matches_target,
      backend_probe_passes, gateway_returns_expected, source_verified,
      generations_mixed, alias_route_disagree, semantic_only_failure
    """
    o = observations
    if o.get("generations_mixed") or o.get("alias_route_disagree") or o.get("outstanding_mutation"):
        return {
            "cause": "control_plane_uncertainty",
            "repair_eligible": False,
            "next": "Enter reconciliation. No repair based on a mixed snapshot.",
        }
    if o.get("source_verified") is False:
        return {
            "cause": "unverified_desired_state",
            "repair_eligible": False,
            "next": "Stop. A memory of an older source cannot establish intended contents.",
        }
    if o.get("target_passes") is False:
        return {
            "cause": "incomplete_or_corrupt_ingestion",
            "repair_eligible": False,
            "next": "Build a new staged collection; do not publish or patch the failed target.",
        }
    if o.get("target_passes") and not o.get("alias_points_to_target", True):
        return {
            "cause": "publication_alias_drift",
            "repair_eligible": True,
            "next": "Read alias and route under serialized authority. Prepare promotion plan.",
        }
    if (
        o.get("target_passes")
        and o.get("alias_points_to_target", True)
        and o.get("backend_probe_passes")
        and not o.get("gateway_returns_expected", True)
    ):
        return {
            "cause": "query_cache_contamination",
            "repair_eligible": True,
            "next": "Epoch-only repair eligible only after backend+routing confirmed correct.",
        }
    if o.get("semantic_only_failure"):
        return {
            "cause": "outside_current_fault_model",
            "repair_eligible": False,
            "next": "Escalate to retrieval/reranking/generation diagnostics.",
        }
    return {"cause": "unknown", "repair_eligible": False, "next": "inspect state or escalate"}


def repeated_call(history: list[tuple[str, str, int]]) -> bool:
    """Hash (tool, normalized args, generation) to detect no-progress loops."""
    seen: set[tuple[str, str, int]] = set()
    for item in history:
        if item in seen:
            return True
        seen.add(item)
    return False
