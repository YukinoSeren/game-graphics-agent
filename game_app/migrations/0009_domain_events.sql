-- 2D: append-only domain events and two rebuildable projections.
--
-- domain_events records only states that actually happened: an object born in
-- a state writes previous_state=NULL at sequence 1; no intermediate staging row
-- is invented.  task_responses associates a source-task answer with its task so
-- a rerun can consume it without a cause_ref.  device_series_slots keeps one
-- device series per (user, installation/context/user) scope.

CREATE TABLE IF NOT EXISTS domain_events (
    target_type    TEXT NOT NULL,
    target_id      TEXT NOT NULL,
    dimension      TEXT NOT NULL,
    sequence       INTEGER NOT NULL CHECK (sequence >= 1),
    previous_state TEXT,
    next_state     TEXT NOT NULL,
    cause          TEXT NOT NULL,
    related_refs   TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL,
    PRIMARY KEY (target_type, target_id, dimension, sequence)
);

CREATE TABLE IF NOT EXISTS task_responses (
    task_id     TEXT NOT NULL,
    sequence    INTEGER NOT NULL CHECK (sequence >= 1),
    response_id TEXT NOT NULL UNIQUE,
    request_id  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (task_id, sequence)
);

CREATE TABLE IF NOT EXISTS device_series_slots (
    user_scope_id    TEXT NOT NULL,
    scope_key        TEXT NOT NULL,
    series_id        TEXT NOT NULL,
    device_series_id TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (user_scope_id, scope_key)
);
