"""Authoritative contracts — Pydantic manifest models (spec p5)."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ChunkSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    point_id: str  # UUID string used by Qdrant
    document_id: str
    chunk_id: str
    revision: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_blob_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CanarySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    query_id: str
    query_vector: tuple[float, ...]
    expected_point_id: str
    expected_revision: str
    expected_content_sha256: str
    top_k: int = Field(default=3, ge=1, le=20)

    @field_validator("query_vector")
    @classmethod
    def _finite_vector(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        if len(v) == 0:
            raise ValueError("query_vector must be non-empty")
        for x in v:
            if not isinstance(x, (int, float)) or not math.isfinite(x):
                raise ValueError("query_vector must contain only finite numbers")
        return v


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    project_id: str
    corpus_id: str
    environment: Literal["local", "staging", "production"]
    revision: str
    source_snapshot_sha256: str
    pipeline_fingerprint: str
    embedding_model_revision: str
    vector_size: int = Field(ge=1)
    distance: Literal["Cosine"] = "Cosine"
    chunker_version: str
    chunks: tuple[ChunkSpec, ...]
    canaries: tuple[CanarySpec, ...]

    @model_validator(mode="after")
    def _admission_checks(self) -> Manifest:
        # Reject duplicate point IDs and chunk IDs
        point_ids = [c.point_id for c in self.chunks]
        if len(set(point_ids)) != len(point_ids):
            raise ValueError("duplicate point_id in manifest")
        chunk_ids = [c.chunk_id for c in self.chunks]
        if len(set(chunk_ids)) != len(chunk_ids):
            raise ValueError("duplicate chunk_id in manifest")
        # Canary dimension + reference checks
        by_id = {c.point_id: c for c in self.chunks}
        for can in self.canaries:
            if len(can.query_vector) != self.vector_size:
                raise ValueError(
                    f"canary {can.query_id}: vector dim"
                    f" {len(can.query_vector)} != {self.vector_size}"
                )
            if not all(math.isfinite(x) for x in can.query_vector):
                raise ValueError(f"canary {can.query_id}: non-finite vector")
            target = by_id.get(can.expected_point_id)
            if target is None:
                raise ValueError(f"canary {can.query_id}: references undeclared point")
            if target.revision != can.expected_revision:
                raise ValueError(f"canary {can.query_id}: revision mismatch")
            if target.content_sha256 != can.expected_content_sha256:
                raise ValueError(f"canary {can.query_id}: content hash mismatch")
        return self


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project: str
    corpus: str
    environment: str


class PlanAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["publish_sealed_index", "epoch_bump_only", "build_staging_index"]
    target: str | None = None


class RepairPlan(BaseModel):
    """Approval-bound repair plan (spec p19)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    incident_id: str
    scope: Scope
    desired_manifest_digest: str
    source_snapshot_digest: str
    expected_before: dict
    actions: tuple[PlanAction, ...]
    postconditions: tuple[str, ...] = ("exact_manifest", "gateway_canaries")
    rollback: dict = Field(default_factory=lambda: {"allowed": False})
    not_after: str  # UTC timestamp ISO8601


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    tool: str
    status: Literal["ok", "unavailable", "unauthorized", "invalid_response", "timeout"]
    timestamp: str
    scope: str = ""
    route_generation: int = 0
    source_tool: str = ""
    content_digest: str = ""
    data: dict = Field(default_factory=dict)


def canonical_bytes(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def manifest_digest(manifest: Manifest) -> str:
    return hashlib.sha256(canonical_bytes(manifest.model_dump(mode="json"))).hexdigest()


def plan_digest(plan_dict: dict) -> str:
    return hashlib.sha256(canonical_bytes(plan_dict)).hexdigest()
