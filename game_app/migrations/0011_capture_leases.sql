-- PERF-T04 owned capture leases (04 §4.3, batch-b-handoff §5).
--
-- A lease is the local recovery log of one capture task.  It is registered in
-- the same transaction as the task, before any tool may start, and it is only
-- finalized after the original is published and its ManagedFile row exists.
-- A prepared/owned/unresolved lease therefore blocks a second capture even
-- when the first task has already reached a terminal state.
--
-- Additions to the shape in batch-b-handoff.md §5 (documented there):
-- * `capture_lease_events` stores every state change with the ownership facts
--   it observed; the head projection is rebuildable from those events plus the
--   task's own capture input (which names the session and native session);
-- * `capture_leases.file_id` is the deterministic id `FileService` will use
--   for `capture:<task_id>`; the row is never rewritten with another file.

CREATE TABLE IF NOT EXISTS capture_leases (
    task_id            TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL,
    native_name        TEXT NOT NULL,
    process_pid        INTEGER CHECK (process_pid IS NULL OR process_pid > 0),
    process_started_at TEXT,
    file_id            TEXT NOT NULL,
    state              TEXT NOT NULL CHECK (state IN ('prepared','owned','finalized','unresolved')),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

-- One lease per session may be non-finalized: a prepared/owned/unresolved
-- lease blocks a second capture even when the first task has ended.
CREATE UNIQUE INDEX IF NOT EXISTS ux_capture_lease_active
    ON capture_leases(session_id)
    WHERE state <> 'finalized';

CREATE INDEX IF NOT EXISTS ix_capture_leases_session
    ON capture_leases(session_id, state);

CREATE TABLE IF NOT EXISTS capture_lease_events (
    task_id            TEXT NOT NULL,
    sequence           INTEGER NOT NULL CHECK (sequence >= 1),
    previous_state     TEXT,
    next_state         TEXT NOT NULL CHECK (next_state IN ('prepared','owned','finalized','unresolved')),
    cause              TEXT NOT NULL,
    process_pid        INTEGER CHECK (process_pid IS NULL OR process_pid > 0),
    process_started_at TEXT,
    file_id            TEXT,
    created_at         TEXT NOT NULL,
    PRIMARY KEY (task_id, sequence)
);
