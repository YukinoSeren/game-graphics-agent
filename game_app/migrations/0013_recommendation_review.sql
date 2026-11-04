-- REC-T03: one durable consumption per (user scope, impact notice).
--
-- The notice and the review events remain in ``objects``; this table makes a
-- re-delivery return the original ResourceResult without a second event.
-- ``review_state`` is deliberately absent: the review state is derived from
-- ``RecommendationReviewEvent``, never stored as a projection.

CREATE TABLE IF NOT EXISTS impact_consumptions (
    user_scope_id        TEXT NOT NULL,
    notice_id            TEXT NOT NULL,
    outcome              TEXT NOT NULL,
    recommendation_ids   TEXT NOT NULL DEFAULT '[]',
    review_event_ids     TEXT NOT NULL DEFAULT '[]',
    result_refs          TEXT NOT NULL,
    reason               TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    PRIMARY KEY (user_scope_id, notice_id)
);

CREATE INDEX IF NOT EXISTS ix_impact_consumptions_notice
    ON impact_consumptions(notice_id);
