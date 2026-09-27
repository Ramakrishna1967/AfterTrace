"""Qdrant Cloud adapter with explicit local-simulation fallback.

Cloud mode requires QDRANT_URL + QDRANT_API_KEY (read from env, never hardcoded).
Local-sim mode is an in-memory dict used ONLY when Cloud creds are absent or
--local is passed, and is always labeled as simulation in terminal output.
"""

from __future__ import annotations

from rich.console import Console

from .config import VECTOR_DIM, Settings
from .vectors import cosine


class QdrantStore:
    def __init__(
        self, settings: Settings, console: Console | None = None, force_local: bool = False
    ):
        self.settings = settings
        self.console = console
        self.local = force_local or not settings.qdrant_configured
        self._mem: dict[str, dict[str, dict]] = {}
        self._alias_mem: dict[str, str] = {}
        self._client = None
        if not self.local:
            from qdrant_client import QdrantClient

            self._client = QdrantClient(
                url=settings.qdrant_url, api_key=settings.qdrant_api_key, timeout=30
            )

    @property
    def mode_label(self) -> str:
        return "LOCAL-SIM (no Qdrant Cloud creds)" if self.local else "Qdrant Cloud"

    def _note(self, msg: str) -> None:
        if self.console:
            self.console.print(f"[dim]{msg}[/dim]")

    # ---- collections ----
    def recreate_collection(self, name: str, dim: int = VECTOR_DIM) -> None:
        if self.local:
            self._mem[name] = {}
            return
        assert self._client is not None
        from qdrant_client.http.models import Distance, VectorParams

        try:
            self._client.delete_collection(name)
        except Exception:
            pass
        self._client.create_collection(
            name, vectors_config=VectorParams(size=dim, distance=Distance.COSINE)
        )

    def upsert(self, name: str, points: list[dict]) -> None:
        if self.local:
            col = self._mem.setdefault(name, {})
            for p in points:
                col[str(p["id"])] = {"vector": p["vector"], "payload": p["payload"]}
            return
        assert self._client is not None
        from qdrant_client.http.models import PointStruct

        structs = [
            PointStruct(id=p["id"], vector=p["vector"], payload=p["payload"]) for p in points
        ]
        self._client.upsert(collection_name=name, points=structs, wait=True)

    def count(self, name: str) -> int:
        if self.local:
            return len(self._mem.get(name, {}))
        assert self._client is not None
        return self._client.count(name).count

    def scroll_all(self, name: str) -> dict[str, dict]:
        if self.local:
            return dict(self._mem.get(name, {}))
        assert self._client is not None
        out: dict[str, dict] = {}
        offset = None
        while True:
            recs, offset = self._client.scroll(
                name, limit=100, offset=offset, with_payload=True, with_vectors=True
            )
            for r in recs:
                out[str(r.id)] = {"vector": list(r.vector), "payload": dict(r.payload or {})}
            if offset is None:
                break
        return out

    def query_top1(self, collection: str, query_vector: list[float]) -> dict:
        if self.local:
            col = self._mem.get(collection, {})
            if not col:
                raise ValueError(f"collection {collection} empty/missing")
            best_id, best_score = None, -2.0
            for pid, pt in col.items():
                s = cosine(query_vector, pt["vector"])
                if s > best_score:
                    best_id, best_score = pid, s
            best = col[best_id]
            return {
                "point_id": best_id,
                "revision": best["payload"].get("revision"),
                "sha": best["payload"].get("content_sha256"),
                "score": best_score,
            }
        assert self._client is not None
        # qdrant-client 1.19: query_points preferred, search as fallback
        try:
            res = self._client.query_points(
                collection, query=query_vector, limit=1, with_payload=True
            )
            pts = res.points if hasattr(res, "points") else res
            if not pts:
                raise ValueError("no hits")
            p = pts[0]
            payload = dict(p.payload or {})
            return {
                "point_id": str(p.id),
                "revision": payload.get("revision"),
                "sha": payload.get("content_sha256"),
                "score": float(getattr(p, "score", 0.0)),
            }
        except Exception as err:
            hits = self._client.search(
                collection, query_vector=query_vector, limit=1, with_payload=True
            )
            if not hits:
                raise ValueError("no hits") from err
            h = hits[0]
            payload = dict(h.payload or {})
            return {
                "point_id": str(h.id),
                "revision": payload.get("revision"),
                "sha": payload.get("content_sha256"),
                "score": float(h.score),
            }

    # ---- aliases (real Qdrant alias best-effort + caller mirrors to SQLite) ----
    def get_alias_target(self, alias: str) -> str | None:
        if self.local:
            return self._alias_mem.get(alias)
        assert self._client is not None
        try:
            resp = self._client.get_aliases()
            for a in resp.aliases:
                if a.alias_name == alias:
                    return a.collection_name
            return None
        except Exception as e:
            self._note(f"[qdrant alias read failed, using SQLite mirror: {e}]")
            return None

    def create_alias(self, alias: str, collection: str) -> None:
        if self.local:
            self._alias_mem[alias] = collection
            return
        assert self._client is not None
        from qdrant_client.http.models import CreateAliasOperation

        self._client.update_collection_aliases(
            [
                CreateAliasOperation(
                    create_alias={"collection_name": collection, "alias_name": alias}
                )
            ]
        )

    def switch_alias(self, alias: str, expected_old: str, new_collection: str) -> dict:
        """Atomic delete+create. Verifies expected_old first; no hidden retries."""
        if self.local:
            actual = self._alias_mem.get(alias)
            if actual != expected_old:
                raise ValueError(
                    f"alias changed before dispatch: expected {expected_old}, got {actual}"
                )
            self._alias_mem[alias] = new_collection
            return {"before": actual, "after": new_collection}
        assert self._client is not None
        from qdrant_client.http.models import CreateAliasOperation, DeleteAliasOperation

        actual = self.get_alias_target(alias)
        if actual != expected_old:
            raise ValueError(
                f"alias changed before dispatch: expected {expected_old}, got {actual}"
            )
        self._client.update_collection_aliases(
            [
                DeleteAliasOperation(delete_alias={"alias_name": alias}),
                CreateAliasOperation(
                    create_alias={"collection_name": new_collection, "alias_name": alias}
                ),
            ]
        )
        observed = self.get_alias_target(alias)
        if observed != new_collection:
            raise RuntimeError(f"alias switch not observed: {observed!r} != {new_collection!r}")
        return {"before": actual, "after": observed}

    def ensure_alias(self, alias: str, collection: str) -> None:
        if self.local:
            self._alias_mem[alias] = collection
            return
        assert self._client is not None
        try:
            current = self.get_alias_target(alias)
        except Exception:
            current = None
        if current == collection:
            return
        from qdrant_client.http.models import CreateAliasOperation, DeleteAliasOperation

        ops = []
        if current is not None:
            ops.append(DeleteAliasOperation(delete_alias={"alias_name": alias}))
        ops.append(
            CreateAliasOperation(create_alias={"collection_name": collection, "alias_name": alias})
        )
        self._client.update_collection_aliases(ops)
