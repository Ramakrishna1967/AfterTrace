"""Shared incident/session operations over the SQLite log.

Used by both the argparse CLI (`sessions`, `export`) and the TUI
(`/sessions`, `/export`, `/undo`, `/redo`, `/history`). Read-only except
where noted. Exports are secret-redacted.
"""
from __future__ import annotations

import json
import os

from . import file_ref, sqlite_log


def list_incidents(settings) -> list[dict]:
    """Newest-last incident rows. Empty list when no log yet (never raises)."""
    try:
        con = sqlite_log.connect(settings.sqlite_path)
    except Exception:
        return []
    try:
        rows = con.execute(
            "SELECT id, scenario, symptom, state, created_at FROM incidents ORDER BY created_at"
        ).fetchall()
        return [
            {"id": r[0], "scenario": r[1], "symptom": r[2], "state": r[3], "created_at": r[4]}
            for r in rows
        ]
    except Exception:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def get_events(settings, incident_id: str, limit: int = 200) -> list[dict]:
    try:
        con = sqlite_log.connect(settings.sqlite_path)
    except Exception:
        return []
    try:
        rows = con.execute(
            "SELECT kind, data_json, created_at FROM events "
            "WHERE incident_id=? ORDER BY seq LIMIT ?",
            (incident_id, limit),
        ).fetchall()
        return [{"kind": r[0], "data": r[1], "created_at": r[2]} for r in rows]
    except Exception:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def find_last_mutation(settings) -> dict | None:
    """Latest alias.switched / alias.reverted / cache.invalidated event, or None."""
    try:
        con = sqlite_log.connect(settings.sqlite_path)
    except Exception:
        return None
    try:
        row = con.execute(
            "SELECT incident_id, kind, data_json FROM events "
            "WHERE kind IN ('alias.switched','alias.reverted',"
            "'alias.reapplied','cache.invalidated') "
            "ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row[2])
        except ValueError:
            data = {}
        return {"incident_id": row[0], "kind": row[1], "data": data}
    except Exception:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


def alias_for_collection(settings, collection: str) -> str | None:
    """SQLite mirror lookup: which alias currently targets this collection."""
    try:
        con = sqlite_log.connect(settings.sqlite_path)
    except Exception:
        return None
    try:
        row = con.execute(
            "SELECT alias_name FROM aliases WHERE collection_name=?", (collection,)
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


def _redact_value(value):
    """Redact secrets inside parsed structure without breaking JSON.

    Never redact the serialized JSON string: regex replacement can eat a
    closing quote/brace and emit invalid JSON (the exact failure the export
    test guards). Parse first, redact values, then dump.
    """
    if isinstance(value, str):
        # If the string itself is an embedded JSON document (data_json),
        # redact its parsed form so inner structure survives too.
        stripped = value.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                inner = json.loads(value)
                return json.dumps(_redact_value(inner), sort_keys=True, default=str)
            except ValueError:
                pass
        return file_ref.redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def export_incident(settings, incident_id: str, dest_dir: str | None = None) -> dict:
    """Write a redacted JSON export. Returns {path, incidents, events} or {error}."""
    items = list_incidents(settings)
    match = next(
        (i for i in items if i["id"] == incident_id or i["id"].startswith(incident_id)),
        None,
    )
    if match is None:
        return {"error": f"unknown incident '{incident_id}'"}
    payload = {"incident": match, "events": get_events(settings, match["id"])}
    clean_payload = _redact_value(payload)
    raw = json.dumps(clean_payload, indent=2, sort_keys=True, default=str)
    clean = raw
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    outdir = dest_dir or os.path.join(here, ".data")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f"export_{match['id'][:8]}.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(clean)
    return {"path": path, "incidents": 1, "events": len(payload["events"])}
