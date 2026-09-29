# AfterTrace

**Memory-guided incident recovery for retrieval pipelines.** AfterTrace detects
when a RAG application silently serves a stale document revision, diagnoses the
cause from live evidence and past incident experience, proposes a bounded
repair, and verifies the fix — with a human approval gate on every write.

Core invariant: *memory proposes hypotheses; deterministic checks establish
facts; authorization permits writes; verification determines recovery.*

🎬 **Live demo:** https://youtu.be/DqRyujWagHg?si=FOxySEdE96FlqQDE

## Why it exists

Silent retrieval failures are the worst kind: ingestion jobs report success,
health checks stay green, and users receive outdated answers with no error
anywhere. AfterTrace closes that gap with an independent verification loop:

```
detect → diagnose → propose → approve → fix → verify → retain
```

Past incidents are retained as experience. Future diagnoses recall them to
move faster — but a recalled fix is never applied blindly: live preconditions
are re-verified first, and a mismatched memory is explicitly rejected.

## Features

- **Stale-revision detection** — canary queries assert the served revision against the intended one
- **Evidence-based diagnosis** — exact index verification plus alias, route, and cache inspection
- **Approval-gated repair** — interactive prompt, modal dialog, or `--yes` for automation; expired or changed plans are rejected
- **Memory-guided transfer** — Hindsight Cloud recall prioritizes checks based on prior incidents
- **Wrong-fix rejection** — recalled repairs that don't match live state are refused with a reason, never applied
- **Full audit trail** — every incident, observation, approval, and mutation is journaled in SQLite
- **Secret-safe by design** — credentials live in the environment only; logs, exports, and retained memory are redacted

## Quickstart

Requirements: Python 3.11+. Install from PyPI (includes the `aftertrace` CLI):

```powershell
pip install AfterTrace
aftertrace doctor     # environment, dependencies, and storage check
aftertrace demo --local --yes   # full end-to-end demonstration
```

From source instead:

```powershell
git clone https://github.com/Ramakrishna1967/AfterTrace
Set-Location AfterTrace
pip install -e .
```

For live backends instead of simulation, set the Cloud credentials first:

```powershell
$env:QDRANT_URL = "https://xxx.qdrant.cloud"
$env:QDRANT_API_KEY = "..."
$env:HINDSIGHT_BASE_URL = "https://..."
$env:HINDSIGHT_API_KEY = "..."
```

Without credentials the tool runs in clearly labeled `LOCAL-SIM` /
`LOCAL-FALLBACK` modes. Pass `--local` to force simulation when credentials
are present. Omit `--yes` for an interactive approval prompt.

## Usage

### One-command demo

```bash
aftertrace demo --local --yes
```

Runs the complete narrative in order: a cold alias-drift fix, a
memory-assisted transfer on a new corpus, and the rejection of a wrong
recalled fix — each resolved and verified.

### Individual commands

```bash
aftertrace scenario1 --yes   # cold alias drift: detect, verify, repair, retain
aftertrace scenario2 --yes   # same fault class, new corpus, memory-assisted
aftertrace scenario3 --yes   # same symptom, different cause: reject stale memory
aftertrace sessions          # list recorded incidents
aftertrace export            # save the latest incident as redacted JSON
aftertrace reset             # clear local state for a fresh run
```

### Interactive terminal UI

```bash
aftertrace tui --local
```

A full-screen console with a prompt box, slash-command palette (`ctrl+p`),
tab completion, and modal approvals. Plain-English input is routed to the
matching flow ("fix the alias drift", "show past runs"). Power inputs:
`@path` injects a project file, `!command` runs a shell command.
All flows: `/scenario1 /scenario2 /scenario3 /demo /history /memory /rerun`
(`ctrl+r`), `/new`, `/sessions`, `/export`, `/undo`, `/redo`, `/compact`,
`/models`, `/connect`, `/themes`, `/share`, `/editor`, `/details`, `/check`,
`/reset`, `/quit`.

## Architecture

| Layer | Location | Role |
| --- | --- | --- |
| CLI / TUI | `cli/` | Operator flows, adapters, SQLite log |
| Control plane | `src/aftertrace/` | FastAPI service: manifests, incidents, approvals, journaled cutover, verifier, SSE, outbox |
| Trace UI | `web/` | Browser projection of incident state |
| Fixtures | `fixtures/` | Manifests, sources, fault scenarios |
| Operations | `ops/` | Service units, native Qdrant config |

The vector store is Qdrant (Cloud or local); long-term memory is Hindsight
Cloud with a local JSONL fallback. Test and demo corpora use deterministic
hash-based fixture vectors, clearly labeled `[FIXTURE]` — never presented as
real embeddings.

### Control-plane service

```bash
pip install -e ".[dev]"
$env:PYTHONPATH = "src"
$env:DATABASE_PATH = ".data/aftertrace.sqlite3"
python -m uvicorn aftertrace.app:app --host 127.0.0.1 --port 8000 --workers 1
```

Health and metrics: `GET /health /ready /metrics`. UI at `/`, API under
`/v1/*` (manifests, incidents, diagnostics, approvals, query, SSE events).
Ops units and native Qdrant config live in `ops/`.

## Safety model

- Recalled memory is advisory only — every repair requires fresh live evidence.
- Approvals bind to an exact plan digest, scope, and expiry; reuse, scope
  mismatch, or precondition drift aborts the write.
- Alias mutations verify expected-before state and confirm observed-after
  state; ambiguous outcomes enter reconciliation, never silent retry.
- Redaction (`redact`, `redact_json`, metadata allowlist) applies to logs,
  event streams, and retained memory.

## Testing

```powershell
python -m pytest tests/ -q   # full suite: unit, integration, contract, evaluation
python -m ruff check cli src tests
python -m ruff format --check cli src tests
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `aftertrace` not recognized | Reinstall (`pip install -e .`) and restart the terminal so `Scripts/` is on `PATH` |
| Demo fails partway | Run `aftertrace doctor` first; then `aftertrace reset` and re-run the demo |
| Stale results after a run | `aftertrace reset` clears the local log and cached memory |
| `Unknown command` in the TUI | Check spelling — it suggests the closest match; `tab` completes, `ctrl+p` lists all |
| Approval modal never appears | Don't pass `--yes` if you want the prompt; in the TUI it always appears |

## Project structure

```
cli/            Operator CLI + interactive TUI
src/aftertrace/ Control-plane service
web/            Trace UI (served by the control plane)
fixtures/       Manifests, sources, fault scenarios
migrations/     SQLite authority schema
ops/            Service units, native Qdrant config
tests/          Unit, integration, contract, evaluation suites
```
