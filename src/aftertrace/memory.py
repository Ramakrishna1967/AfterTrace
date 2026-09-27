"""Hindsight adapter — retain/recall with provenance (spec p14-15)."""

from __future__ import annotations

import os


class IncidentMemory:
    def __init__(self, bank_id: str, scope_tags: list[str]):
        if not bank_id or not scope_tags:
            raise ValueError("application-authorized scope required")
        self.bank_id, self.tags = bank_id, scope_tags
        try:
            from hindsight_client import Hindsight  # type: ignore

            self.client = Hindsight(
                base_url=os.environ.get("HINDSIGHT_BASE_URL", ""),
                api_key=os.environ.get("HINDSIGHT_API_KEY", ""),
                timeout=45.0,
            )
            self.available = True
        except Exception:
            self.client = None
            self.available = False

    @staticmethod
    def bank_for(project_id: str, environment: str) -> str:
        # Application-mapped bank per project+environment; callers cannot supply arbitrary banks.
        safe_p = "".join(c if c.isalnum() or c in "-_" else "_" for c in project_id)
        safe_e = "".join(c if c.isalnum() or c in "-_" else "_" for c in environment)
        return f"aftertrace-{safe_p}-{safe_e}"

    @staticmethod
    def scope_tags(project_id: str, environment: str) -> list[str]:
        return [
            f"project:{project_id}",
            f"env:{environment}",
            "subsystem:rag",
            "compat:qdrant-local-v1",
        ]

    async def retain_terminal(self, event: dict):
        if not self.available:
            raise RuntimeError("memory unavailable (degraded mode)")
        return await self.client.aretain(
            bank_id=self.bank_id,
            content=event["sanitized_summary"],
            document_id=event["document_id"],
            timestamp=event["occurred_at"],
            context="AFTERTRACE verified operational incident",
            tags=self.tags,
            metadata={
                "incident_id": event["incident_id"],
                "event_id": event["event_id"],
                "evidence_digest": event["evidence_digest"],
                "outcome": event["outcome"],
                "pipeline": event["pipeline_fingerprint"],
            },
            retain_async=False,
        )

    async def recall(self, symptom: str):
        if not self.available:
            raise RuntimeError("memory unavailable (degraded mode)")
        if len(symptom.split()) > 500:
            # Enforce caller-side token-ish bound (spec: 500-token limit)
            symptom = " ".join(symptom.split()[:500])
        result = await self.client.arecall(
            bank_id=self.bank_id,
            query=symptom,
            tags=self.tags,
            tags_match="all_strict",
            types=["world", "experience"],
            budget="mid",
            max_tokens=1500,
        )
        return result.results

    async def close(self):
        if self.client is not None:
            try:
                await self.client.aclose()
            except Exception:
                pass


def sanitize_summary(text: str, max_chars: int = 2000) -> str:
    """Bound retained text; secrets redacted via shared security patterns."""
    from .security import redact

    return redact(text)[:max_chars]
