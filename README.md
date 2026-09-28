# AFTERTRACE — memory-guided incident recovery

**Memory proposes, live evidence disposes.** Two ways in:

- **CLI/TUI** (`cli/`) — incident-recovery flows against Qdrant + Hindsight Cloud,
  fully cloud with zero local services except a single SQLite log file.
- **Control-plane service** (`src/aftertrace/` + `web/`) — FastAPI + SQLite authority
  implementing the full specification below; needs a running server (see Service).

Spec background: `SystemArchitecture.pdf` (Rev 1.1). This implements the
detect -> diagnose -> propose -> approve -> fix -> verify -> retain loop plus
the critical **reject-wrong-recalled-fix** gate.

## Stack

- Qdrant Cloud (via `QDRANT_URL` + `QDRANT_API_KEY`) — real vector store.
- Hindsight Cloud (via `HINDSIGHT_BASE_URL` + `HINDSIGHT_API_KEY`, `hindsight-client` SDK) — real memory.
- SQLite single file `.data/aftertrace_cli.sqlite3` — minimal incident/event/alias/cache log.
- Python 3.11+, `qdrant-client`, `hindsight-client`, `rich`, `textual`.
  No Docker; the CLI needs no local servers.
- Fake vectors are deterministic hash fixtures (`cli/vectors.py`), clearly labeled `[FIXTURE]`, not embeddings.

Credentials are read from the environment only. Never hardcoded, never printed.

## Setup

```bash
pip install "qdrant-client>=1.12" "hindsight-client>=0.10" rich textual
# Cloud mode:
set QDRANT_URL=https://xxx.qdrant.cloud
set QDRANT_API_KEY=...
set HINDSIGHT_BASE_URL=https://...
set HINDSIGHT_API_KEY=...
# optional: set AFTERTRACE_BANK_ID=aftertrace
python -m cli doctor
```

Without those env vars the tool runs in clearly-labeled
`LOCAL-SIM` / `LOCAL-FALLBACK` modes so the logic is still demonstrable.
Pass `--local` to force simulation even with creds set.

## Run

One command runs the full story in order:

```bash
python -m cli demo --local --yes
python -m cli tui --local       # interactive full-screen terminal UI (same flows, modal approvals)
```

In the TUI just describe what you want in plain words ("fix the alias drift",
"show past runs", "what do you remember") or use slash commands:
`/scenario1 /scenario2 /scenario3 /demo /history /memory /rerun`
(`tab` completes, `ctrl+p` opens the palette, `ctrl+r` re-runs the last flow).

Power-user input: `@path` injects a project file (fuzzy match, capped,
redacted); `!command` runs a shell command and shows the output as a result.
More flows: `/new` fresh context, `/sessions` incident list, `/export`
redacted JSON, `/undo` + `/redo` alias-switch revert (approval-gated),
`/compact` collapse transcript, `/models` backend readiness, `/connect`
session-only Cloud keys.

Or step by step (each a fresh process):

```bash
python -m cli reset
python -m cli scenario1 --yes   # cold alias-drift: detect, fix A->B, verify, retain
python -m cli scenario2 --yes   # NEW corpus, recall s1, memory-ordered alias check, fix, verify
python -m cli scenario3 --yes   # same symptom, alias already correct -> REJECT recalled alias fix, fix stale cache instead
python -m cli sessions          # list recorded incidents
python -m cli export            # save latest incident as redacted JSON
```

Omit `--yes` for an interactive `y/n` approval prompt (required narrative:
recalled memory never auto-authorizes; human approves, live preconditions re-checked).

## Service (control plane)

`src/aftertrace/` is the full FastAPI service from the spec: manifests,
incidents, approvals, journaled alias cutover, deterministic verifier,
durable SSE, memory outbox, and the `web/` trace UI. It needs a running
server plus Qdrant and Hindsight reachability:

```bash
pip install -e ".[dev]"
$env:PYTHONPATH = "src"
$env:DATABASE_PATH = ".data/aftertrace.sqlite3"
python -m uvicorn aftertrace.app:app --host 127.0.0.1 --port 8000 --workers 1
```

Health: `GET /health /ready /metrics`, UI at `/`, API under `/v1/*`
(manifests, incidents, diagnostics, approvals, query, SSE events).
Ops units and native Qdrant config live in `ops/`.

## What each scenario proves

- **s1 cold**: B ingested correct, alias still A. Query expecting B returns A.
  Agent verifies B exact, observes alias=A, proposes switch, asks approval,
  switches, re-queries B PASS, retains real incident (no fabrication).
- **s2 transfer**: new corpus/collections, same bug. Fresh process calls
  `recall`, shows past s1 experience, prioritizes alias check first,
  still verifies live state before writing. Re-query PASS.
- **s3 rejection (most important)**: alias already correct (->B), stale cache
  serves A. Recall returns old alias fix, agent re-checks live alias+target,
  prints `REJECTED recalled alias fix`, investigates different cause,
  invalidates cache only (alias untouched). No inappropriate write.

## Files

- `cli/config.py` env + constants
- `cli/vectors.py`, `cli/fixtures.py` deterministic fixture corpus
- `cli/qdrant_store.py` Cloud adapter + labeled local-sim fallback
- `cli/memory_store.py` Hindsight adapter + labeled JSONL fallback
- `cli/sqlite_log.py` incident/event/alias/cache tables
- `cli/agent.py` gateway query, exact verification, approval
- `cli/scenarios.py` the three runs
- `cli/__main__.py` `python -m cli ...` entry
- `cli/tui.py` interactive full-screen terminal UI (`python -m cli tui`)
