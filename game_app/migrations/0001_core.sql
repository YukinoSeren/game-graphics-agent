-- ENG-T01 core storage: immutable objects, aggregate snapshots, references and
-- idempotent responses.  Payloads are stored as the exact bytes the owner wrote.

CREATE TABLE IF NOT EXISTS objects (
    id             TEXT PRIMARY KEY,
    entity_type    TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    payload        BLOB NOT NULL,
    payload_encoding TEXT NOT NULL DEFAULT 'json',
    series_id      TEXT,
    version        INTEGER,
    created_at     TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_objects_version
    ON objects(entity_type, series_id, version)
    WHERE version IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_objects_type ON objects(entity_type);

CREATE TABLE IF NOT EXISTS aggregate_snapshots (
    entity_type TEXT NOT NULL,
    object_id   TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    payload     BLOB NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (entity_type, object_id, revision)
);

CREATE TABLE IF NOT EXISTS aggregate_heads (
    entity_type      TEXT NOT NULL,
    object_id        TEXT NOT NULL,
    current_revision INTEGER NOT NULL,
    PRIMARY KEY (entity_type, object_id)
);

CREATE TABLE IF NOT EXISTS object_references (
    owner_id    TEXT NOT NULL REFERENCES objects(id),
    relation    TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id   TEXT NOT NULL REFERENCES objects(id),
    locator     TEXT
);

CREATE INDEX IF NOT EXISTS ix_references_target
    ON object_references(target_type, target_id);

CREATE TABLE IF NOT EXISTS idempotency (
    user_scope_id          TEXT NOT NULL,
    method                 TEXT NOT NULL,
    canonical_route        TEXT NOT NULL,
    key                    TEXT NOT NULL,
    request_schema_version TEXT NOT NULL,
    request_hash           TEXT NOT NULL,
    task_id                TEXT,
    result_refs            TEXT NOT NULL DEFAULT '[]',
    response_status        INTEGER NOT NULL,
    response_body          BLOB NOT NULL,
    lifecycle              TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    PRIMARY KEY (user_scope_id, method, canonical_route, key)
);

CREATE INDEX IF NOT EXISTS ix_idempotency_task ON idempotency(task_id);
