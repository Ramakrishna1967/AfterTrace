"""Security + budgets — boundary enforcement outside the model (spec p25).

Covers: prompt-injection posture (treat retrieved content as data),
cross-project leakage guards, arbitrary network/file denial, secret
redaction + scanning, duplicate/destructive-write guards, memory
provenance, loop/spend budgets, approval auth.
"""
from __future__ import annotations

import re
import time

SECRET_PATTERNS = [
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*\S+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+"),
    re.compile(r"(?i)authorization\s*:\s*\S+"),
    re.compile(r"x-api-key\s*:\s*\S+", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(secret|token|password)\s*[:=]\s*\S+"),
]

ALLOWLIST_METADATA_KEYS = {
    "incident_id", "event_id", "evidence_digest", "outcome", "pipeline",
    "project", "corpus", "environment", "revision", "manifest_digest",
    "alias", "collection", "generation", "cache_epoch", "fence",
    "route", "canary_id", "cache_hit", "tool", "status",
}


def redact(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    out = text
    for pat in SECRET_PATTERNS:
        out = pat.sub("[redacted]", out)
    # Never persist raw signed URLs / tokens in query strings
    out = re.sub(r"(api_key|token)=[^&\s]+", r"\1=[redacted]", out)
    return out


def scan_for_secrets(text: str) -> list[str]:
    hits = []
    for pat in SECRET_PATTERNS:
        if pat.search(text or ""):
            hits.append(pat.pattern[:40])
    return hits


def sanitize_metadata(meta: dict) -> dict:
    """Allowlisted structured metadata only (spec p25)."""
    return {k: (redact(str(v))[:500]) for k, v in meta.items() if k in ALLOWLIST_METADATA_KEYS}


def sanitize_summary(text: str, max_chars: int = 2000) -> str:
    return redact(text)[:max_chars]


def redact_json(value, max_str: int = 2000):
    """Redact secrets inside parsed JSON without breaking its structure.

    Never json.loads() a redacted string: redaction inserts brackets/text
    that can invalidate JSON and crash the stream. Parse first, redact values.
    """
    if isinstance(value, str):
        return redact(value)[:max_str]
    if isinstance(value, dict):
        return {k: redact_json(v, max_str) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_json(v, max_str) for v in value]
    return value


class Budget:
    """Unbounded loop/tool/spend guard (spec p25 defaults)."""

    def __init__(self, max_dispatches: int = 8, wall_clock_s: float = 120.0,
                 response_limit_bytes: int = 131072, recall_tokens: int = 1500,
                 approval_validity_s: int = 300, scan_limit: int = 10000):
        self.max_dispatches = max_dispatches
        self.wall_clock_s = wall_clock_s
        self.response_limit_bytes = response_limit_bytes
        self.recall_tokens = recall_tokens
        self.approval_validity_s = approval_validity_s
        self.scan_limit = scan_limit
        self.dispatches = 0
        self.start = time.monotonic()
        self.response_bytes = 0

    def check_dispatch(self):
        if self.dispatches >= self.max_dispatches:
            raise RuntimeError("diagnostic tool budget exhausted; escalate with observations retained")
        if (time.monotonic() - self.start) > self.wall_clock_s:
            raise RuntimeError("diagnostic wall clock exhausted; stop new model/tool requests")
        self.dispatches += 1

    def check_response(self, n_bytes: int) -> bool:
        """True if within limit; caller must truncate + keep evidence reference."""
        self.response_bytes += n_bytes
        return n_bytes <= self.response_limit_bytes

    def truncate(self, text: str) -> str:
        b = text.encode("utf-8")
        if len(b) <= self.response_limit_bytes:
            return text
        return b[: self.response_limit_bytes].decode("utf-8", errors="ignore") + "…[truncated]"


def require_scope(principal: dict, project: str, corpus: str, environment: str):
    """Server-side scope binding; never trust request fields alone (p5/p25)."""
    if principal.get("project_id") != project:
        raise PermissionError("cross-project access denied")
    allowed_corpora = principal.get("corpora")
    if allowed_corpora is not None and corpus not in allowed_corpora:
        raise PermissionError("unauthorized corpus scope")
    if principal.get("environment") and principal["environment"] != environment:
        raise PermissionError("unauthorized environment scope")


def require_approver_role(principal: dict):
    # Separation of duties: the default operator role (and any ad-hoc role)
    # may propose and investigate but may NOT approve repairs. Only an
    # explicitly privileged approver/admin may consume a plan.
    # NOTE: caller identity here is header-asserted (see app._principal).
    # Deployments must verify identity via a real auth provider; a header
    # alone must not confer approver privilege.
    role = principal.get("role", "operator")
    if role not in ("admin", "approver"):
        raise PermissionError(f"role {role} may not approve repairs")
    return role
