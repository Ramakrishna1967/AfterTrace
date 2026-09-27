"""Validated environment configuration (spec p27/p29)."""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qdrant_url: str = Field(default="http://127.0.0.1:6333")
    qdrant_api_key: str = Field(default="")
    database_path: str = Field(default=".data/aftertrace.sqlite3")
    source_root: str = Field(default=".data/sources")
    hindsight_base_url: str = Field(default="")
    hindsight_api_key: str = Field(default="")
    control_url: str = Field(default="http://127.0.0.1:8000")
    diagnostic_token: str = Field(default="")
    # Budgets (spec p25)
    max_tool_dispatches: int = 8
    diagnosis_timeout_s: float = 120.0
    tool_response_limit_bytes: int = 131072  # 128 KiB
    manifest_scan_limit: int = 10000
    approval_validity_s: int = 300
    memory_recall_tokens: int = 1500

    @classmethod
    def from_env(cls) -> Settings:
        def env(name: str, default: str = "") -> str:
            return os.environ.get(name, default)

        return cls(
            qdrant_url=env("QDRANT_URL", "http://127.0.0.1:6333"),
            qdrant_api_key=env("QDRANT_API_KEY", ""),
            database_path=env("DATABASE_PATH", ".data/aftertrace.sqlite3"),
            source_root=env("SOURCE_ROOT", ".data/sources"),
            hindsight_base_url=env("HINDSIGHT_BASE_URL", ""),
            hindsight_api_key=env("HINDSIGHT_API_KEY", ""),
            control_url=env("AFTERTRACE_CONTROL_URL", "http://127.0.0.1:8000"),
            diagnostic_token=env("AFTERTRACE_DIAGNOSTIC_TOKEN", ""),
        )
