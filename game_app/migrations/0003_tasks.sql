-- ENG-T02 durable task execution: registered tasks, immutable inputs, appended
-- events, checkpoints, external-call audit and the single-slot projection.

CREATE TABLE IF NOT EXISTS tasks (
    task_id                   TEXT PRIMARY KEY,
    type                      TEXT NOT NULL,
    user_scope_id             TEXT NOT NULL,
    parent_id                 TEXT,
    revision                  INTEGER NOT NULL,
    state                     TEXT NOT NULL,
    current_input_revision    INTEGER NOT NULL,
    budget                    TEXT NOT NULL,
    calls                     TEXT NOT NULL,
    checkpoint_id             TEXT,
    domain_result             TEXT,
    stop_reason               TEXT,
    result_refs               TEXT NOT NULL DEFAULT '[]',
    cancel_requested          INTEGER NOT NULL DEFAULT 0,
    last_error_code           TEXT,
    active_elapsed_seconds    REAL NOT NULL DEFAULT 0,
    resume_blocked_unknown_call INTEGER NOT NULL DEFAULT 0,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_tasks_state ON tasks(state, created_at);
CREATE INDEX IF NOT EXISTS ix_tasks_parent ON tasks(parent_id);

CREATE TABLE IF NOT EXISTS task_inputs (
    task_id             TEXT NOT NULL REFERENCES tasks(task_id),
    input_revision      INTEGER NOT NULL,
    input               TEXT NOT NULL,
    supersedes_revision INTEGER,
    cause_ref           TEXT,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (task_id, input_revision)
);

CREATE TABLE IF NOT EXISTS task_events (
    task_id               TEXT NOT NULL REFERENCES tasks(task_id),
    sequence              INTEGER NOT NULL,
    input_revision        INTEGER NOT NULL,
    from_state            TEXT,
    to_state              TEXT NOT NULL,
    cause                 TEXT NOT NULL,
    related_refs          TEXT NOT NULL DEFAULT '[]',
    active_elapsed_seconds REAL NOT NULL,
    created_at            TEXT NOT NULL,
    PRIMARY KEY (task_id, sequence)
);

CREATE TABLE IF NOT EXISTS task_checkpoints (
    checkpoint_id  TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL REFERENCES tasks(task_id),
    input_revision INTEGER NOT NULL,
    completed_steps TEXT NOT NULL,
    artifact_refs  TEXT NOT NULL,
    call_ids       TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS external_calls (
    call_id             TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL REFERENCES tasks(task_id),
    input_revision      INTEGER NOT NULL,
    kind                TEXT NOT NULL,
    call_state          TEXT NOT NULL,
    provider            TEXT NOT NULL,
    requested_model     TEXT,
    returned_model      TEXT,
    prompt_version      TEXT,
    output_schema_version TEXT,
    input_hash          TEXT NOT NULL,
    intent_saved_at     TEXT NOT NULL,
    sent_at             TEXT,
    finished_at         TEXT,
    provider_request_id TEXT,
    response_file_id    TEXT,
    usage               TEXT,
    billing_state       TEXT NOT NULL,
    error_code          TEXT
);

CREATE INDEX IF NOT EXISTS ix_external_calls_task ON external_calls(task_id);
