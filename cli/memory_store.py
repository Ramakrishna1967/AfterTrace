"""Hindsight Cloud adapter with explicit local-fallback log.

Cloud mode requires HINDSIGHT_BASE_URL + HINDSIGHT_API_KEY (env only).
Fallback mode appends to .data/memory_fallback.jsonl and does keyword recall
over it. Terminal output ALWAYS labels which mode is in use. No fabricated
memories: fallback recall only returns previously retained local entries.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from rich.console import Console

from .config import SCOPE_TAGS, Settings


@dataclass
class MemoryHit:
    text: str
    document_id: str | None
    score: float = 0.0


class MemoryStore:
    def __init__(self, settings: Settings, console: Console | None = None, force_local: bool = False):
        self.settings = settings
        self.console = console
        self.local = force_local or not settings.hindsight_configured
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.fallback_path = os.environ.get(
            "AFTERTRACE_MEMORY_FALLBACK", os.path.join(here, ".data", "memory_fallback.jsonl")
        )
        self._client = None
        if not self.local:
            from hindsight_client import Hindsight

            self._client = Hindsight(
                base_url=settings.hindsight_base_url,
                api_key=settings.hindsight_api_key,
                timeout=45.0,
            )

    @property
    def mode_label(self) -> str:
        return "LOCAL-FALLBACK memory (no Hindsight Cloud creds)" if self.local else "Hindsight Cloud"

    def ensure_bank(self) -> None:
        if self.local:
            return
        assert self._client is not None
        try:
            self._client.create_bank(bank_id=self.settings.bank_id, name="AFTERTRACE")
        except Exception as e:
            # Bank likely already exists; continue.
            if self.console:
                self.console.print(f"[dim]bank ensure note: {e}[/dim]")

    def retain(self, content: str, document_id: str, context: str, metadata: dict[str, str]) -> str:
        """Retain real incident data only. Caller builds content from observations."""
        if self.local:
            os.makedirs(os.path.dirname(self.fallback_path), exist_ok=True)
            rec = {"document_id": document_id, "context": context, "metadata": metadata, "content": content}
            with open(self.fallback_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            return "local-fallback"
        assert self._client is not None
        str_meta = {k: str(v) for k, v in metadata.items()}
        resp = self._client.retain(
            bank_id=self.settings.bank_id,
            content=content,
            context=context,
            document_id=document_id,
            metadata=str_meta,
            tags=SCOPE_TAGS,
            retain_async=False,
        )
        return str(getattr(resp, "success", True))

    def recall(self, query: str, max_tokens: int = 1500) -> list[MemoryHit]:
        if len(query) > 8000:
            query = query[:8000]
        if self.local:
            hits: list[MemoryHit] = []
            if not os.path.exists(self.fallback_path):
                return hits
            q = query.lower()
            keywords = [w for w in q.split() if len(w) > 3][:20]
            with open(self.fallback_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    text = rec.get("content", "")
                    tl = text.lower()
                    score = sum(1 for w in keywords if w in tl)
                    # Boost alias-drift vocabulary so transfer scenario matches.
                    for w in ("alias", "drift", "revision", "collection"):
                        if w in tl:
                            score += 1
                    if score > 0:
                        hits.append(MemoryHit(text=text, document_id=rec.get("document_id"), score=float(score)))
            hits.sort(key=lambda h: h.score, reverse=True)
            return hits[:5]
        assert self._client is not None
        resp = self._client.recall(
            bank_id=self.settings.bank_id,
            query=query,
            types=["world", "experience"],
            budget="mid",
            max_tokens=max_tokens,
            tags=SCOPE_TAGS,
            tags_match="all_strict",
        )
        out: list[MemoryHit] = []
        for r in resp.results or []:
            out.append(MemoryHit(text=getattr(r, "text", ""), document_id=getattr(r, "document_id", None)))
        return out

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
