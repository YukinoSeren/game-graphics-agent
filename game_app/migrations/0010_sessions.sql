-- PERF-T02 sessions, segments and immutable configuration bindings.
--
-- Session and segment aggregates keep their immutable revisions in
-- `aggregate_snapshots`; the tables below are rebuildable *head* projections
-- used for lookup, activity sets and revision compare-and-set.  Configuration
-- bindings are append-only: closing a segment adds a new binding revision, it
-- never rewrites the open one.
--
-- Additions to the shape in batch-3-handoff.md §6 (documented there):
-- * configuration_bindings.binding_id carries the object id of the immutable
--   ConfigurationBinding record;
-- * configuration_bindings keeps time_basis/unit so a BindingRange can be
--   rebuilt without the object payload.

CREATE TABLE IF NOT EXISTS session_segments (
    session_id          TEXT NOT NULL,
    segment_id          TEXT NOT NULL,
    revision            INTEGER NOT NULL CHECK (revision >= 1),
    start_s             REAL NOT NULL CHECK (start_s >= 0),
    end_s               REAL,
    time_basis          TEXT NOT NULL,
    lifecycle           TEXT NOT NULL,
    applied_id          TEXT,
    configuration_state TEXT NOT NULL,
    binding_revision    INTEGER NOT NULL CHECK (binding_revision >= 1),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (session_id, segment_id)
);

CREATE INDEX IF NOT EXISTS ix_session_segments_active
    ON session_segments(session_id, lifecycle);

CREATE TABLE IF NOT EXISTS configuration_bindings (
    binding_id          TEXT NOT NULL,
    segment_id          TEXT NOT NULL,
    binding_revision    INTEGER NOT NULL CHECK (binding_revision >= 1),
    applied_id          TEXT,
    configuration_state TEXT NOT NULL,
    start_s             REAL NOT NULL CHECK (start_s >= 0),
    end_s               REAL,
    time_basis          TEXT NOT NULL,
    unit                TEXT NOT NULL CHECK (unit IN ('s', 'ms')),
    supersedes_revision INTEGER,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (segment_id, binding_revision),
    UNIQUE (binding_id)
);
