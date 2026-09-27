"""Agent core: detect -> recall -> diagnose -> propose -> approve -> fix."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import sqlite_log

if TYPE_CHECKING:
    from .qdrant_store import QdrantStore


def gateway_query(
    con: sqlite3.Connection,
    store: QdrantStore,
    alias: str,
    cache_key: str,
    query_vector: list[float],
) -> dict:
    """Real query path with stale-cache simulation layered on top.

    If cache_sim has a stale entry for cache_key, it is returned (simulating a
    poisoned/stale cache). Otherwise resolves alias -> live collection and
    queries Qdrant, then returns live result.
    """
    cached = sqlite_log.get_cache(con, cache_key)
    if cached is not None and cached["stale"]:
        return {
            "point_id": cached["point_id"],
            "revision": cached["revision"],
            "via": "stale-cache",
            "score": 1.0,
        }
    live_collection = sqlite_log.get_alias(con, alias)
    if live_collection is None:
        # fall back to Qdrant-side alias (cloud mode)
        live_collection = store.get_alias_target(alias)
    if live_collection is None:
        raise ValueError(f"alias {alias!r} has no target")
    hit = store.query_top1(live_collection, query_vector)
    hit["via"] = f"live:{live_collection}"
    hit["live_collection"] = live_collection
    return hit


def verify_target_collection(
    store: QdrantStore, collection: str, corpus_data: dict
) -> tuple[bool, list[str]]:
    """Exact check: identity set + revision + sha match expected B."""
    errors: list[str] = []
    try:
        points = store.scroll_all(collection)
    except Exception as e:
        return False, [f"scroll failed: {e}"]
    expected = {c["point_id"]: c for c in corpus_data["chunks"]}
    if set(expected) != set(points):
        errors.append(f"point identity differs: expected {len(expected)}, got {len(points)}")
    for pid, spec in expected.items():
        if pid not in points:
            errors.append(f"{pid}: missing in {collection}")
            continue
        payload = points[pid].get("payload", {})
        if payload.get("revision") != "B":
            errors.append(f"{pid}: revision {payload.get('revision')!r} != 'B'")
        if payload.get("content_sha256") != spec["sha_b"]:
            errors.append(f"{pid}: sha mismatch")
        if payload.get("document_id") != spec["doc_id"]:
            errors.append(f"{pid}: doc_id mismatch")
    return (len(errors) == 0), errors


def ask_approval(console: Console, proposal: str, auto_yes: bool, approver=None) -> bool:
    """Approval gate. `approver` is an optional callable(proposal)->bool used by
    non-stdio frontends (e.g. the Textual TUI modal). Defaults preserve CLI behavior."""
    console.print(
        Panel(proposal, title="Proposed repair (requires approval)", border_style="yellow")
    )
    if auto_yes:
        console.print("[yellow]--yes supplied: auto-approving.[/yellow]")
        return True
    if approver is not None:
        try:
            return bool(approver(proposal))
        except Exception:
            return False
    try:
        ans = console.input("[bold yellow]Approve and apply? [y/N]: [/bold yellow]").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


def show_evidence_table(console: Console, rows: list[tuple[str, str]]) -> None:
    t = Table(title="Evidence (live observations, not memory)")
    t.add_column("Check")
    t.add_column("Result")
    for k, v in rows:
        t.add_row(k, v)
    console.print(t)
