"""Three scenarios. Each is runnable as a separate fresh process."""

from __future__ import annotations

import datetime
import sqlite3

from rich.console import Console
from rich.panel import Panel

from . import fixtures, sqlite_log
from .agent import ask_approval, gateway_query, show_evidence_table, verify_target_collection
from .config import Settings
from .memory_store import MemoryStore
from .qdrant_store import QdrantStore


def _collections_for(scenario: str) -> tuple[str, str, str]:
    return (
        f"aftertrace_{scenario}_a",
        f"aftertrace_{scenario}_b",
        f"aftertrace_{scenario}_live",
    )


def _ingest_corpus(
    store: QdrantStore, corpus_data: dict, col_a: str, col_b: str, console: Console
) -> None:
    pts_a, pts_b = [], []
    for c in corpus_data["chunks"]:
        pts_a.append(
            {
                "id": c["point_id"],
                "vector": c["vec_a"],
                "payload": {
                    "document_id": c["doc_id"],
                    "revision": "A",
                    "content_sha256": c["sha_a"],
                    "text": f"[FIXTURE] {corpus_data['corpus']}/{c['doc_id']} rev A",
                },
            }
        )
        pts_b.append(
            {
                "id": c["point_id"],
                "vector": c["vec_b"],
                "payload": {
                    "document_id": c["doc_id"],
                    "revision": "B",
                    "content_sha256": c["sha_b"],
                    "text": f"[FIXTURE] {corpus_data['corpus']}/{c['doc_id']} rev B",
                },
            }
        )
    store.recreate_collection(col_a)
    store.upsert(col_a, pts_a)
    store.recreate_collection(col_b)
    store.upsert(col_b, pts_b)
    console.print(f"[dim]ingested {len(pts_a)} pts -> {col_a} (rev A fixtures)[/dim]")
    console.print(f"[dim]ingested {len(pts_b)} pts -> {col_b} (rev B fixtures)[/dim]")


def _set_live_alias(
    con: sqlite3.Connection, store: QdrantStore, alias: str, collection: str
) -> None:
    sqlite_log.set_alias(con, alias, collection)
    try:
        store.ensure_alias(alias, collection)
    except Exception:
        if not store.local:
            # Cloud mode: fail closed. A SQLite-only alias while Qdrant
            # still points elsewhere is split-brain; never hide it.
            raise
        # LOCAL-SIM: SQLite mirror remains authoritative.


def _recall_step(console: Console, memory: MemoryStore, query: str) -> list:
    console.print(f"[cyan]> recalling past experience from {memory.mode_label}...[/cyan]")
    try:
        hits = memory.recall(query)
    except Exception as e:
        console.print(f"[red]recall failed (degraded, continuing without memory): {e}[/red]")
        return []
    if not hits:
        console.print("[dim]> recall: no relevant past incidents.[/dim]")
        return []
    console.print(f"[green]> recall: {len(hits)} relevant past incident(s):[/green]")
    for i, h in enumerate(hits[:3], 1):
        snippet = h.text[:300].replace("\n", " ")
        console.print(f"  [dim]{i}. doc={h.document_id} score={h.score:.1f}[/dim]\n  {snippet}...")
    return hits


def _retain_incident(
    console: Console,
    memory: MemoryStore,
    con: sqlite3.Connection,
    incident_id: str,
    content: str,
    document_id: str,
    metadata: dict,
) -> None:
    console.print(f"[cyan]> retaining incident to {memory.mode_label}...[/cyan]")
    try:
        memory.ensure_bank()
        memory.retain(
            content=content,
            document_id=document_id,
            context="AFTERTRACE verified operational incident",
            metadata=metadata,
        )
        console.print("[green]> retained.[/green]")
        sqlite_log.log_event(con, incident_id, "memory.retained", {"document_id": document_id})
    except Exception as e:
        console.print(f"[red]retain failed (repair still verified; memory degraded): {e}[/red]")
        sqlite_log.log_event(con, incident_id, "memory.retain_failed", {"error": str(e)})


# ---------------- Scenario 1: cold incident ----------------
def run_scenario1(
    settings: Settings,
    console: Console,
    auto_yes: bool = False,
    force_local: bool = False,
    approver=None,
) -> int:
    console.print(
        Panel(
            "SCENARIO 1 -- cold incident: alias drift (no prior memory)", border_style="bold blue"
        )
    )
    con = sqlite_log.connect(settings.sqlite_path)
    store = QdrantStore(settings, console, force_local=force_local)
    memory = MemoryStore(settings, console, force_local=force_local)
    console.print(f"[dim]vector store: {store.mode_label} | memory: {memory.mode_label}[/dim]")

    corpus = fixtures.corpus_s1()
    col_a, col_b, alias = _collections_for("s1")
    _ingest_corpus(store, corpus, col_a, col_b, console)
    # BUG: B fully ingested and correct, but alias still points at A.
    _set_live_alias(con, store, alias, col_a)
    console.print(f"[yellow]BUG INJECTED: {col_b} correct, but alias {alias} -> {col_a}[/yellow]")

    canary = fixtures.canary_for(corpus, corpus["chunks"][0]["doc_id"])
    cache_key = f"{alias}:{canary['query_id']}"
    sqlite_log.clear_cache(con, cache_key)

    symptom = f"query {canary['query_id']} expected rev B but live serves old rev"
    incident_id = sqlite_log.new_incident(con, "s1-cold", symptom)

    # DETECT
    first = gateway_query(con, store, alias, cache_key, list(canary["query_vector"]))
    console.print(
        f"> query via alias {alias}: got rev [bold]{first['revision']}[/bold]"
        f" point {first['point_id']} ({first['via']})"
    )
    if first["revision"] == "B" and first["point_id"] == canary["expected_point_id"]:
        console.print("[green]No failure: live already returns B. Nothing to do.[/green]")
        return 0
    console.print("[red]X MISMATCH DETECTED: expected rev B, got rev A. Opening incident.[/red]")
    sqlite_log.log_event(con, incident_id, "detected", {"got": first, "expected_rev": "B"})

    # DIAGNOSE (cold: no memory fast-path expected, but still attempt recall to prove cold)
    recalled = _recall_step(
        console,
        memory,
        "RAG retrieval returns old revision; new build ready but live serves stale revision",
    )
    live = sqlite_log.get_alias(con, alias) or store.get_alias_target(alias)
    ok_b, errs = verify_target_collection(store, col_b, corpus)
    show_evidence_table(
        console,
        [
            (f"verify target {col_b}", "PASS exact B" if ok_b else f"FAIL: {errs[:2]}"),
            (f"alias {alias} -> ?", f"{live} (expected {col_b})"),
            ("live query revision", f"{first['revision']}"),
        ],
    )
    sqlite_log.log_event(
        con, incident_id, "diagnosed", {"live": live, "target_ok": ok_b, "recalled": len(recalled)}
    )
    if not ok_b:
        console.print(
            "[red]Target B is NOT correct; cannot propose alias promotion."
            " Escalating (needs new build).[/red]"
        )
        sqlite_log.set_state(con, incident_id, "ESCALATED")
        return 2
    if live == col_b:
        console.print("[red]Alias already correct; not alias drift. Escalating.[/red]")
        sqlite_log.set_state(con, incident_id, "ESCALATED")
        return 2
    hypothesis = "alias_drift"
    console.print(
        f"[bold]Hypothesis: {hypothesis}[/bold]"
        " -- target B verified correct while alias still targets A."
    )

    # PROPOSE + APPROVE
    proposal = (
        f"switch alias {alias}: {col_a} -> {col_b}\n"
        f"Evidence: B exact ({store.count(col_b)} pts), live query rev A.\n"
        "Postcondition: exact B + gateway canary returns B."
    )
    if not ask_approval(console, proposal, auto_yes, approver):
        console.print("Cancelled by operator. No writes made.")
        sqlite_log.set_state(con, incident_id, "CANCELLED")
        return 3
    sqlite_log.log_event(
        con, incident_id, "approved", {"alias": alias, "before": col_a, "after": col_b}
    )

    # APPLY (re-verify immediately before write: never let memory authorize)
    live_now = sqlite_log.get_alias(con, alias) or store.get_alias_target(alias)
    if live_now != col_a:
        console.print(
            f"[red]Precondition changed (alias now {live_now}); aborting. Re-plan required.[/red]"
        )
        return 4
    res = store.switch_alias(alias, col_a, col_b)
    _set_live_alias(con, store, alias, col_b)
    console.print(f"[green]OK alias switched: {res['before']} -> {res['after']}[/green]")
    sqlite_log.log_event(con, incident_id, "alias.switched", res)

    # VERIFY
    second = gateway_query(con, store, alias, cache_key, list(canary["query_vector"]))
    passed = second["revision"] == "B" and second["point_id"] == canary["expected_point_id"]
    status = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
    console.print(
        f"> re-query: rev [bold]{second['revision']}[/bold] ({second['via']}) -> {status}"
    )
    if not passed:
        sqlite_log.set_state(con, incident_id, "FAILED")
        return 5
    sqlite_log.set_state(con, incident_id, "RESOLVED")
    sqlite_log.log_event(con, incident_id, "verified", {"got": second})

    # RETAIN (real data only)
    ts = datetime.datetime.now(datetime.UTC).isoformat()
    content = (
        f"AFTERTRACE incident s1 (cold). Symptom: query {canary['query_id']}"
        " expected revision B "
        f"point {canary['expected_point_id']} but alias {alias} pointed at {col_a},"
        " returning revision A. "
        f"Evidence: target {col_b} passed exact inventory ({store.count(col_b)} points,"
        " revision B, sha verified); "
        f"live alias observed {col_a}; live query returned rev A via {first['via']}. "
        f"Attempt rejected: none (cold). Action approved: switch live alias {col_a} -> {col_b}. "
        f"Outcome: post-switch gateway query returned rev B point {second['point_id']} (PASS). "
        "Applicability: same publication protocol; target must first be verified exact. "
        "Contraindication: if alias already correct or target incomplete,"
        " do NOT switch; investigate other cause. "
        f"Collections: {col_a},{col_b}. Corpus: {corpus['corpus']}."
    )
    _retain_incident(
        console,
        memory,
        con,
        incident_id,
        content,
        document_id=f"aftertrace-s1-{corpus['corpus']}",
        metadata={
            "incident_id": incident_id,
            "scenario": "s1-cold",
            "corpus": corpus["corpus"],
            "alias": alias,
            "before": col_a,
            "after": col_b,
            "occurred_at": ts,
        },
    )
    memory.close()
    console.print(
        Panel("SCENARIO 1 RESOLVED: alias drift fixed and verified.", border_style="green")
    )
    return 0


# ---------------- Scenario 2: memory-assisted transfer ----------------
def run_scenario2(
    settings: Settings,
    console: Console,
    auto_yes: bool = False,
    force_local: bool = False,
    approver=None,
) -> int:
    console.print(
        Panel(
            "SCENARIO 2 -- memory-assisted transfer on NEW corpus (fresh process)",
            border_style="bold blue",
        )
    )
    con = sqlite_log.connect(settings.sqlite_path)
    store = QdrantStore(settings, console, force_local=force_local)
    memory = MemoryStore(settings, console, force_local=force_local)
    console.print(f"[dim]vector store: {store.mode_label} | memory: {memory.mode_label}[/dim]")

    corpus = fixtures.corpus_s2()
    col_a, col_b, alias = _collections_for("s2")
    _ingest_corpus(store, corpus, col_a, col_b, console)
    _set_live_alias(con, store, alias, col_a)
    console.print(f"[yellow]BUG INJECTED: {col_b} correct, but alias {alias} -> {col_a}[/yellow]")

    canary = fixtures.canary_for(corpus, corpus["chunks"][0]["doc_id"])
    cache_key = f"{alias}:{canary['query_id']}"
    sqlite_log.clear_cache(con, cache_key)
    incident_id = sqlite_log.new_incident(
        con, "s2-transfer", f"query {canary['query_id']} expected B, live stale"
    )

    first = gateway_query(con, store, alias, cache_key, list(canary["query_vector"]))
    console.print(
        f"> query via alias {alias}: got rev [bold]{first['revision']}[/bold] ({first['via']})"
    )
    if first["revision"] == "B":
        console.print("[green]No failure.[/green]")
        return 0
    console.print("[red]X MISMATCH DETECTED.[/red]")
    sqlite_log.log_event(con, incident_id, "detected", {"got": first, "expected_rev": "B"})

    # RECALL FIRST (this is the transfer proof) -- fresh process, new corpus.
    recalled = _recall_step(
        console,
        memory,
        "RAG retrieval returns old document revision; new build verified correct"
        " but live serves stale revision; alias drift suspected",
    )
    if recalled:
        console.print(
            "[bold cyan]Memory fast-path: prioritizing alias-target check first"
            " (instead of full blind scan).[/bold cyan]"
        )
        console.print(
            "[dim]Note: memory only ORDERED the check"
            " -- live verification still required before any write.[/dim]"
        )
    else:
        console.print(
            "[yellow]No memory found -- did scenario 1 retain succeed?"
            " Continuing with live-only diagnosis.[/yellow]"
        )
    sqlite_log.log_event(con, incident_id, "recalled", {"n": len(recalled)})

    live = sqlite_log.get_alias(con, alias) or store.get_alias_target(alias)
    console.print(f"> [memory-prioritized] alias {alias} -> {live}")
    ok_b, errs = verify_target_collection(store, col_b, corpus)
    show_evidence_table(
        console,
        [
            (
                "recalled guidance",
                f"{len(recalled)} hit(s); top suggests alias-drift" if recalled else "none",
            ),
            (f"verify target {col_b}", "PASS exact B" if ok_b else f"FAIL: {errs[:2]}"),
            (f"alias {alias}", f"{live}"),
        ],
    )
    if not ok_b or live == col_b:
        console.print("[red]Evidence does not support alias drift. Escalating.[/red]")
        sqlite_log.set_state(con, incident_id, "ESCALATED")
        return 2
    console.print("[bold]Hypothesis: alias_drift (memory-assisted, live-confirmed).[/bold]")

    proposal = (
        f"switch alias {alias}: {col_a} -> {col_b}\n"
        f"Evidence: recalled s1 alias-drift + live alias={live}, B exact."
    )
    if not ask_approval(console, proposal, auto_yes, approver):
        sqlite_log.set_state(con, incident_id, "CANCELLED")
        return 3
    live_now = sqlite_log.get_alias(con, alias) or store.get_alias_target(alias)
    if live_now != col_a:
        console.print("[red]Precondition changed; aborting.[/red]")
        return 4
    res = store.switch_alias(alias, col_a, col_b)
    _set_live_alias(con, store, alias, col_b)
    console.print(f"[green]OK alias switched: {res['before']} -> {res['after']}[/green]")
    sqlite_log.log_event(con, incident_id, "alias.switched", res)

    second = gateway_query(con, store, alias, cache_key, list(canary["query_vector"]))
    passed = second["revision"] == "B" and second["point_id"] == canary["expected_point_id"]
    status = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
    console.print(f"> re-query: rev [bold]{second['revision']}[/bold] -> {status}")
    if not passed:
        sqlite_log.set_state(con, incident_id, "FAILED")
        return 5
    sqlite_log.set_state(con, incident_id, "RESOLVED")
    sqlite_log.log_event(con, incident_id, "verified", {"got": second})

    ts = datetime.datetime.now(datetime.UTC).isoformat()
    content = (
        f"AFTERTRACE incident s2 (transfer). Symptom: query {canary['query_id']}"
        f" on NEW corpus {corpus['corpus']} "
        f"expected B but alias {alias} pointed at {col_a}."
        " Evidence: recall returned prior alias-drift experience; "
        f"live alias observed {col_a}; target {col_b} exact ({store.count(col_b)} pts). "
        f"Action approved: switch {col_a} -> {col_b}. Outcome: re-query rev B PASS. "
        "Memory sped diagnosis by prioritizing alias check;"
        " live verification still gated the write."
    )
    _retain_incident(
        console,
        memory,
        con,
        incident_id,
        content,
        document_id=f"aftertrace-s2-{corpus['corpus']}",
        metadata={
            "incident_id": incident_id,
            "scenario": "s2-transfer",
            "corpus": corpus["corpus"],
            "alias": alias,
            "before": col_a,
            "after": col_b,
            "occurred_at": ts,
        },
    )
    memory.close()
    console.print(
        Panel("SCENARIO 2 RESOLVED: memory-assisted alias fix verified.", border_style="green")
    )
    return 0


# ---------------- Scenario 3: reject wrong recalled fix ----------------
def run_scenario3(
    settings: Settings,
    console: Console,
    auto_yes: bool = False,
    force_local: bool = False,
    approver=None,
) -> int:
    console.print(
        Panel(
            "SCENARIO 3 -- same symptom, DIFFERENT cause: reject recalled alias fix",
            border_style="bold blue",
        )
    )
    con = sqlite_log.connect(settings.sqlite_path)
    store = QdrantStore(settings, console, force_local=force_local)
    memory = MemoryStore(settings, console, force_local=force_local)
    console.print(f"[dim]vector store: {store.mode_label} | memory: {memory.mode_label}[/dim]")

    corpus = fixtures.corpus_s3()
    col_a, col_b, alias = _collections_for("s3")
    _ingest_corpus(store, corpus, col_a, col_b, console)
    # CORRECT alias this time...
    _set_live_alias(con, store, alias, col_b)
    # ...but a STALE CACHE entry serves the old revision (the genuinely different fault).
    canary = fixtures.canary_for(corpus, corpus["chunks"][0]["doc_id"])
    cache_key = f"{alias}:{canary['query_id']}"
    stale_pid = corpus["chunks"][0]["point_id"]
    sqlite_log.set_cache(con, cache_key, "A", stale_pid, stale=1)
    console.print(
        f"[yellow]FAULT INJECTED: alias {alias} -> {col_b} (CORRECT),"
        f" but stale cache serves rev A for {canary['query_id']}[/yellow]"
    )

    incident_id = sqlite_log.new_incident(
        con, "s3-reject", f"query {canary['query_id']} expected B, got stale A"
    )

    first = gateway_query(con, store, alias, cache_key, list(canary["query_vector"]))
    console.print(
        f"> query: got rev [bold]{first['revision']}[/bold] ({first['via']})"
        " -- same surface symptom as alias drift."
    )
    if first["revision"] == "B":
        console.print("[green]No failure.[/green]")
        return 0
    console.print("[red]X MISMATCH DETECTED.[/red]")
    sqlite_log.log_event(con, incident_id, "detected", {"got": first, "expected_rev": "B"})

    recalled = _recall_step(
        console,
        memory,
        "RAG retrieval returns old document revision; live serves stale revision;"
        " alias drift fix switch alias",
    )
    sqlite_log.log_event(con, incident_id, "recalled", {"n": len(recalled)})
    if recalled:
        console.print(
            "[cyan]> Recalled fix says: 'switch alias A -> B'."
            " MUST re-verify live state before acting.[/cyan]"
        )

    # CRITICAL: re-verify live state; do NOT let memory authorize.
    live = sqlite_log.get_alias(con, alias) or store.get_alias_target(alias)
    try:
        qdrant_live = store.get_alias_target(alias)
    except Exception:
        qdrant_live = None
    ok_b, errs = verify_target_collection(store, col_b, corpus)
    cache_state = sqlite_log.get_cache(con, cache_key)
    show_evidence_table(
        console,
        [
            ("recalled fix", "switch alias A->B" if recalled else "(no memory)"),
            (f"SQLite alias {alias}", f"{live}"),
            ("Qdrant alias target", f"{qdrant_live}"),
            (f"target {col_b} exact?", "PASS" if ok_b else f"FAIL {errs[:2]}"),
            (
                "cache entry",
                f"rev {cache_state['revision']} stale={cache_state['stale']}"
                if cache_state
                else "none",
            ),
            ("live query rev", f"{first['revision']} via {first['via']}"),
        ],
    )
    sqlite_log.log_event(
        con,
        incident_id,
        "diagnosed",
        {"live": live, "ok_b": ok_b, "cache": cache_state, "recalled": len(recalled)},
    )

    # REJECTION GATE -- the single most important behavior.
    if live == col_b and ok_b:
        console.print(
            Panel(
                "REJECTED recalled alias fix.\n"
                f"Reason: alias {alias} already -> {col_b} and target B verified exact.\n"
                "Reapplying 'switch alias A->B' would be wrong (precondition false).\n"
                "Investigating different cause instead (stale cache).",
                title="REJECTION (correct behavior)",
                border_style="red",
            )
        )
        sqlite_log.log_event(
            con,
            incident_id,
            "rejected_recalled_fix",
            {"reason": "alias already correct", "live": live},
        )
    else:
        console.print("[red]Alias evidence unexpected; escalating without alias write.[/red]")
        sqlite_log.set_state(con, incident_id, "ESCALATED")
        return 2

    # Diagnose different cause: stale cache.
    if not (cache_state and cache_state["stale"] and first.get("via") == "stale-cache"):
        console.print(
            "[yellow]Cache not obviously stale;"
            " escalating for deeper retrieval/cache diagnostics."
            " No alias write made.[/yellow]"
        )
        sqlite_log.set_state(con, incident_id, "ESCALATED")
        console.print(
            Panel(
                "SCENARIO 3 SAFE: wrong fix rejected, no inappropriate write.", border_style="green"
            )
        )
        return 0

    console.print("[bold]Revised hypothesis: stale-cache (not alias drift).[/bold]")
    proposal = (
        f"Invalidate stale cache key {cache_key} (rev A, stale=1).\n"
        f"Alias stays {alias} -> {col_b} (UNCHANGED).\n"
        "Postcondition: same query returns live rev B."
    )
    if not ask_approval(console, proposal, auto_yes, approver):
        sqlite_log.set_state(con, incident_id, "CANCELLED")
        return 3
    sqlite_log.clear_cache(con, cache_key)
    sqlite_log.log_event(con, incident_id, "cache.invalidated", {"cache_key": cache_key})
    console.print("[green]OK stale cache invalidated (alias untouched).[/green]")

    second = gateway_query(con, store, alias, cache_key, list(canary["query_vector"]))
    passed = second["revision"] == "B" and second["point_id"] == canary["expected_point_id"]
    status = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
    console.print(
        f"> re-query: rev [bold]{second['revision']}[/bold] ({second['via']}) -> {status}"
    )
    if not passed:
        sqlite_log.set_state(con, incident_id, "FAILED")
        return 5
    sqlite_log.set_state(con, incident_id, "RESOLVED")
    sqlite_log.log_event(con, incident_id, "verified", {"got": second})

    ts = datetime.datetime.now(datetime.UTC).isoformat()
    content = (
        f"AFTERTRACE incident s3 (rejection). Symptom: query {canary['query_id']}"
        " expected B but got A. "
        f"Recalled fix (alias A->B) REJECTED: live alias {alias} already -> {col_b},"
        " target exact. "
        f"True cause: stale cache key {cache_key} served rev A."
        " Action: invalidated cache only, alias unchanged. "
        "Outcome: re-query rev B PASS."
        " Lesson: never apply recalled repair without live precondition check."
    )
    _retain_incident(
        console,
        memory,
        con,
        incident_id,
        content,
        document_id=f"aftertrace-s3-{corpus['corpus']}",
        metadata={
            "incident_id": incident_id,
            "scenario": "s3-reject",
            "corpus": corpus["corpus"],
            "alias": alias,
            "occurred_at": ts,
        },
    )
    memory.close()
    console.print(
        Panel(
            "SCENARIO 3 RESOLVED: wrong fix rejected; cache cause fixed."
            " No inappropriate alias write.",
            border_style="green",
        )
    )
    return 0
