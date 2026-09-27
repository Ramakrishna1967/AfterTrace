# AFTERTRACE — memory-guided incident recovery

**Memory proposes, live evidence disposes.** Fully cloud, zero local services
(except a single SQLite log file). CLI only.

Spec background: `SystemArchitecture.pdf` (Rev 1.1). This implements the
detect -> diagnose -> propose -> approve -> fix -> verify -> retain loop plus
the critical **reject-wrong-recalled-fix** gate.

## Stack

- Qdrant Cloud (via `QDRANT_URL` + `QDRANT_API_KEY`) — real vector store.
- Hindsight Cloud (via `HINDSIGHT_BASE_URL` + `HINDSIGHT_API_KEY`, `hindsight-client` SDK) — real memory.
- SQLite single file `.data/aftertrace_cli.sqlite3` — minimal incident/event/alias/cache log.
- Python 3.11+, `qdrant-client`, `hindsight-client`, `rich`. No Docker, no servers, no FastAPI/MCP/SSE.
- Fake vectors are deterministic hash fixtures (`cli/vectors.py`), clearly labeled `[FIXTURE]`, not embeddings.

Credentials are read from the environment only. Never hardcoded, never printed.

## Setup

```bash
pip install "qdrant-client>=1.12" "hindsight-client>=0.10" rich
# Cloud mode (recommended for judging):
set QDRANT_URL=https://xxx.qdrant.cloud
set QDRANT_API_KEY=...
set HINDSIGHT_BASE_URL=https://...
set HINDSIGHT_API_KEY=...
# optional: set AFTERTRACE_BANK_ID=aftertrace
python -m cli check
```

Without those env vars the tool runs in clearly-labeled
`LOCAL-SIM` / `LOCAL-FALLBACK` modes so the logic is still demonstrable.
Pass `--local` to force simulation even with creds set.

## Run (fresh process per scenario, in order)

```bash
python -m cli reset
python -m cli scenario1 --yes   # cold alias-drift: detect, fix A->B, verify, retain
python -m cli scenario2 --yes   # NEW corpus, recall s1, memory-ordered alias check, fix, verify
python -m cli scenario3 --yes   # same symptom, alias already correct -> REJECT recalled alias fix, fix stale cache instead
```

Omit `--yes` for an interactive `y/n` approval prompt (required narrative:
recalled memory never auto-authorizes; human approves, live preconditions re-checked).

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
