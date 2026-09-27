"""Config: env-only credentials, constants. Never hardcode secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _get(name: str) -> str | None:
    v = os.environ.get(name)
    if v is not None and v.strip() == "":
        return None
    return v


@dataclass(frozen=True)
class Settings:
    qdrant_url: str | None
    qdrant_api_key: str | None
    hindsight_base_url: str | None
    hindsight_api_key: str | None
    bank_id: str
    sqlite_path: str

    @property
    def qdrant_configured(self) -> bool:
        return bool(self.qdrant_url and self.qdrant_api_key)

    @property
    def hindsight_configured(self) -> bool:
        return bool(self.hindsight_base_url and self.hindsight_api_key)


def load_settings() -> Settings:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_db = os.path.join(here, ".data", "aftertrace_cli.sqlite3")
    return Settings(
        qdrant_url=_get("QDRANT_URL"),
        qdrant_api_key=_get("QDRANT_API_KEY"),
        hindsight_base_url=_get("HINDSIGHT_BASE_URL"),
        hindsight_api_key=_get("HINDSIGHT_API_KEY"),
        bank_id=os.environ.get("AFTERTRACE_BANK_ID", "aftertrace"),
        sqlite_path=os.environ.get("AFTERTRACE_DB", default_db),
    )


SCOPE_TAGS = ["project:aftertrace", "subsystem:rag"]
VECTOR_DIM = 32
