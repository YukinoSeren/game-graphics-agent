-- DEV-T03 current adopted configuration: one pointer per (user, installation)
-- and one append-only event per pointer revision.  A historical record never
-- writes either table.

CREATE TABLE IF NOT EXISTS current_applied (
    user_scope_id  TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    applied_id     TEXT,
    revision       INTEGER NOT NULL CHECK (revision >= 1),
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (user_scope_id, installation_id)
);

CREATE TABLE IF NOT EXISTS current_applied_events (
    user_scope_id       TEXT NOT NULL,
    installation_id     TEXT NOT NULL,
    pointer_revision    INTEGER NOT NULL CHECK (pointer_revision >= 1),
    previous_applied_id TEXT,
    next_applied_id     TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (user_scope_id, installation_id, pointer_revision)
);
