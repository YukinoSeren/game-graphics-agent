-- IMP-T03 import snapshot identity and its declaration file.
--
-- The projection makes the three-part import identity
-- (import_fingerprint, mapping_version, mapping_context_hash) unique without
-- scanning JSON payloads.  declaration_file_id is stored here because the
-- publication writer runs before FileService inserts its ManagedFile rows, so
-- a reference row could not be created in the same statement order.

CREATE TABLE IF NOT EXISTS import_snapshots (
    import_snapshot_id     TEXT PRIMARY KEY,
    import_fingerprint     TEXT NOT NULL,
    mapping_version        TEXT NOT NULL,
    mapping_context_hash   TEXT NOT NULL,
    import_status          TEXT NOT NULL,
    declaration_file_id    TEXT NOT NULL,
    parent_import_snapshot_id TEXT,
    mapped_game_id         TEXT,
    created_by_task_id     TEXT,
    created_at             TEXT NOT NULL,
    UNIQUE (import_fingerprint, mapping_version, mapping_context_hash)
);

CREATE INDEX IF NOT EXISTS ix_import_snapshots_fingerprint
    ON import_snapshots(import_fingerprint);
