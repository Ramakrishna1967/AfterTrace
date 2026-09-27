PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS manifests (
    digest TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    corpus_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    revision TEXT NOT NULL,
    body_json TEXT NOT NULL,
    source_verified_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    project_id TEXT NOT NULL,
    corpus_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    alias_name TEXT NOT NULL UNIQUE,
    collection_name TEXT NOT NULL,
    manifest_digest TEXT NOT NULL REFERENCES manifests(digest),
    generation INTEGER NOT NULL DEFAULT 1,
    cache_epoch INTEGER NOT NULL DEFAULT 1,
    mode TEXT NOT NULL CHECK(mode IN ('serving','paused','verifying','reconcile')),
    fence INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(project_id, corpus_id, environment)
);

CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    corpus_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    desired_manifest TEXT NOT NULL REFERENCES manifests(digest),
    state TEXT NOT NULL,
    terminal INTEGER NOT NULL DEFAULT 0 CHECK(terminal IN (0,1)),
    state_version INTEGER NOT NULL DEFAULT 0,
    request_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, request_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_incident
ON incidents(project_id, corpus_id, environment)
WHERE terminal = 0;

CREATE TABLE IF NOT EXISTS plans (
    digest TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    body_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    plan_digest TEXT NOT NULL REFERENCES plans(digest),
    approver_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active','consumed','revoked','expired')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    plan_digest TEXT NOT NULL REFERENCES plans(digest),
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('prepared','dispatched','observed','verified','unknown','failed')),
    request_json TEXT NOT NULL,
    response_json TEXT,
    before_json TEXT NOT NULL,
    after_json TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(plan_digest, ordinal)
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    kind TEXT NOT NULL,
    body_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_incident ON events(incident_id, seq);

CREATE TABLE IF NOT EXISTS memory_outbox (
    event_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    document_id TEXT NOT NULL UNIQUE,
    bank_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('pending','submitted','completed','retry','dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT
);
