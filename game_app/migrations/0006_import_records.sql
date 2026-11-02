-- IMP-T02 capture-scoped record identity.  The four-part key
-- (import_snapshot_id, capture_scope_id, record_type, local_record_id) is
-- unique among *usable* records.  An isolated record keeps its row and its
-- diagnostic envelope but does not claim the usable slot, so a duplicate or a
-- broken dependency can never overwrite the record that does.

CREATE TABLE IF NOT EXISTS imported_record_keys (
    record_snapshot_id TEXT PRIMARY KEY,
    import_snapshot_id TEXT NOT NULL,
    capture_scope_id   TEXT NOT NULL,
    record_type        TEXT NOT NULL,
    local_record_id    TEXT NOT NULL,
    usable             INTEGER NOT NULL CHECK (usable IN (0, 1))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_imported_record_usable
    ON imported_record_keys(
        import_snapshot_id, capture_scope_id, record_type, local_record_id
    )
    WHERE usable = 1;

CREATE INDEX IF NOT EXISTS ix_imported_record_keys_snapshot
    ON imported_record_keys(import_snapshot_id);
