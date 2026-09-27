"""Minimal SQLite incident/event log + alias table + cache-sim table. Single local file."""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import uuid


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS incidents(
          id TEXT PRIMARY KEY,
          scenario TEXT NOT NULL,
          symptom TEXT NOT NULL,
          state TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          incident_id TEXT NOT NULL REFERENCES incidents(id),
          kind TEXT NOT NULL,
          data_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS aliases(
          alias_name TEXT PRIMARY KEY,
          collection_name TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cache_sim(
          cache_key TEXT PRIMARY KEY,
          response_revision TEXT NOT NULL,
          response_point_id TEXT NOT NULL,
          stale INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL
        );
        """
    )
    con.commit()
    return con


def new_incident(con: sqlite3.Connection, scenario: str, symptom: str) -> str:
    iid = str(uuid.uuid4())
    con.execute(
        "INSERT INTO incidents(id,scenario,symptom,state,created_at) VALUES(?,?,?,?,?)",
        (iid, scenario, symptom, "OPEN", _now()),
    )
    con.commit()
    return iid


def log_event(con: sqlite3.Connection, incident_id: str, kind: str, data: dict) -> None:
    con.execute(
        "INSERT INTO events(incident_id,kind,data_json,created_at) VALUES(?,?,?,?)",
        (incident_id, kind, json.dumps(data, sort_keys=True, default=str), _now()),
    )
    con.commit()


def set_state(con: sqlite3.Connection, incident_id: str, state: str) -> None:
    con.execute("UPDATE incidents SET state=? WHERE id=?", (state, incident_id))
    con.commit()


def set_alias(con: sqlite3.Connection, alias: str, collection: str) -> None:
    con.execute(
        "INSERT INTO aliases(alias_name,collection_name,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(alias_name) DO UPDATE SET collection_name=excluded.collection_name,"
        " updated_at=excluded.updated_at",
        (alias, collection, _now()),
    )
    con.commit()


def get_alias(con: sqlite3.Connection, alias: str) -> str | None:
    row = con.execute("SELECT collection_name FROM aliases WHERE alias_name=?", (alias,)).fetchone()
    return row[0] if row else None


def set_cache(con: sqlite3.Connection, key: str, revision: str, point_id: str, stale: int) -> None:
    con.execute(
        "INSERT INTO cache_sim(cache_key,response_revision,"
        "response_point_id,stale,updated_at)"
        " VALUES(?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET"
        " response_revision=excluded.response_revision,"
        " response_point_id=excluded.response_point_id,"
        " stale=excluded.stale, updated_at=excluded.updated_at",
        (key, revision, point_id, stale, _now()),
    )
    con.commit()


def get_cache(con: sqlite3.Connection, key: str) -> dict | None:
    row = con.execute(
        "SELECT response_revision,response_point_id,stale FROM cache_sim WHERE cache_key=?", (key,)
    ).fetchone()
    if not row:
        return None
    return {"revision": row[0], "point_id": row[1], "stale": bool(row[2])}


def clear_cache(con: sqlite3.Connection, key: str) -> None:
    con.execute("DELETE FROM cache_sim WHERE cache_key=?", (key,))
    con.commit()
