-- FBK-T01: the current target-acceptance projection and its event log.
--
-- Acceptances themselves are immutable ``TargetAcceptance`` objects.  This
-- pair exists only to compare-and-set ``(session_id, proposal_id)`` inside the
-- write transaction, so two concurrent declarations cannot silently overwrite
-- each other; the acceptance history stays in the event rows.

CREATE TABLE IF NOT EXISTS target_acceptance_current (
    session_id     TEXT NOT NULL,
    proposal_id    TEXT NOT NULL,
    acceptance_id  TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (session_id, proposal_id)
);

CREATE TABLE IF NOT EXISTS target_acceptance_events (
    session_id              TEXT NOT NULL,
    proposal_id             TEXT NOT NULL,
    pointer_revision        INTEGER NOT NULL,
    previous_acceptance_id  TEXT,
    next_acceptance_id      TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    PRIMARY KEY (session_id, proposal_id, pointer_revision)
);
