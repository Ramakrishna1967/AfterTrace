"""Retrieval gateway — coherent routing snapshot + scoped cache (spec p9)."""

from __future__ import annotations

import hashlib
import json
import threading
import time


def canonical_bytes(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def cache_key(route: dict, query: dict, principal_scope: list[str] | str) -> str:
    material = {
        "project": route["project_id"],
        "corpus": route["corpus_id"],
        "environment": route["environment"],
        "collection": route["collection_name"],
        "generation": route["generation"],
        "epoch": route["cache_epoch"],
        "manifest": route["manifest_digest"],
        "acl_scope": principal_scope,
        "query": query,  # vector/text, filters, top_k, model revision
    }
    return hashlib.sha256(canonical_bytes(material)).hexdigest()


class Gateway:
    """In-process gateway with admission lock, in-flight registry and epoch cache.

    Query path mirrors spec p9:
      1. derive scope server-side (caller passes principal)
      2. admit only in serving (verifier probes allowed with verifier=True)
      3. cache hit returns copy + trace metadata
      4. miss queries physical collection via injected query_fn
      5. finally release in-flight registration
    """

    def __init__(self, db, query_fn=None):
        self._db = db
        self._query_fn = query_fn or (lambda collection, vector, top_k, filters: [])
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()
        self._cache: dict[str, dict] = {}
        self._route_cache: dict[tuple, dict] = {}

    def set_query_fn(self, fn):
        self._query_fn = fn

    def _load_route(self, project_id: str, corpus_id: str, environment: str) -> dict:
        conn = self._db.connect()
        try:
            row = self._db.get_route(conn, project_id, corpus_id, environment)
        finally:
            conn.close()
        if row is None:
            raise ValueError("no serving route")
        return dict(row)

    async def query(self, query: dict, principal: dict, verifier: bool = False) -> dict:
        project_id = principal["project_id"]
        corpus_id = principal["corpus_id"]
        environment = principal.get("environment", "staging")
        acl_scope = principal.get("acl_scope", [principal.get("tenant", "tenant:a")])

        with self._lock:
            route = self._load_route(project_id, corpus_id, environment)
            mode = route["mode"]
            if mode != "serving" and not verifier:
                return {
                    "error": "gateway gated",
                    "status": 503,
                    "route": {
                        k: route[k] for k in ("collection_name", "generation", "cache_epoch")
                    },
                }
            # Capture immutable tuple
            captured = {
                "project_id": route["project_id"],
                "corpus_id": route["corpus_id"],
                "environment": route["environment"],
                "collection_name": route["collection_name"],
                "generation": route["generation"],
                "cache_epoch": route["cache_epoch"],
                "manifest_digest": route["manifest_digest"],
            }
            request_id = f"{time.time_ns()}-{len(self._in_flight)}"
            self._in_flight.add(request_id)

        try:
            key = cache_key(captured, query, acl_scope)
            if key in self._cache:
                stored = self._cache[key]
                return {
                    **{k: v for k, v in stored.items()},
                    "cache_hit": True,
                    "query_id": query.get("query_id", ""),
                    "request_id": request_id,
                    "route": {
                        "collection_name": captured["collection_name"],
                        "generation": captured["generation"],
                        "cache_epoch": captured["cache_epoch"],
                    },
                }
            # Miss: query physical collection captured above
            vector = query.get("query_vector", [])
            top_k = int(query.get("top_k", 3))
            filters = query.get("filters", {})
            hits = self._query_fn(captured["collection_name"], vector, top_k, filters)
            # ACL enforcement: filter hits to principal scope (tenant isolation canary)
            # Hits carry payload tenant when present; drop forbidden ones.
            allowed = []
            scope_set = set(acl_scope if isinstance(acl_scope, list) else [acl_scope])
            for h in hits:
                tenant = h.get("tenant")
                if tenant is not None and tenant not in scope_set:
                    continue
                allowed.append(h)
            response = {
                "hits": allowed,
                "cache_hit": False,
                "query_id": query.get("query_id", ""),
                "request_id": request_id,
                "route": {
                    "collection_name": captured["collection_name"],
                    "generation": captured["generation"],
                    "cache_epoch": captured["cache_epoch"],
                },
            }
            self._cache[key] = {k: v for k, v in response.items() if k not in ("request_id",)}
            return response
        finally:
            with self._lock:
                self._in_flight.discard(request_id)

    # Test helpers
    def poison_cache(self, route: dict, query: dict, scope, payload: dict):
        """Insert a contaminated entry under the current key (fault fixture)."""
        self._cache[cache_key(route, query, scope)] = payload

    def clear(self):
        with self._lock:
            self._cache.clear()
