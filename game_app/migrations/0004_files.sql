-- ENG-T03 managed-file operations: staged files, publish phases and the
-- original response.  ManagedFile objects themselves live in `objects`.

CREATE TABLE IF NOT EXISTS file_operations (
    operation_id           TEXT PRIMARY KEY,
    kind                   TEXT NOT NULL,
    user_scope_id          TEXT NOT NULL,
    method                 TEXT NOT NULL,
    canonical_route        TEXT NOT NULL,
    key                    TEXT NOT NULL,
    request_schema_version TEXT NOT NULL,
    request_hash           TEXT NOT NULL,
    owner_ref              TEXT,
    file_ids               TEXT NOT NULL DEFAULT '[]',
    manifest               TEXT NOT NULL,
    phase                  TEXT NOT NULL,
    last_error_code        TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    UNIQUE (user_scope_id, method, canonical_route, key)
);

CREATE INDEX IF NOT EXISTS ix_file_operations_phase ON file_operations(phase);
