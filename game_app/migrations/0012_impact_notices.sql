-- KNW-T04 impact notices: a rebuildable de-duplication projection.
--
-- The notice itself is an ``ImpactNotice`` object in ``objects``; this table
-- only makes the four-part de-duplication tuple unique without scanning JSON
-- payloads.  ``affected_hash`` is the canonical hash of the sorted affected
-- record ids plus the rule version.

CREATE TABLE IF NOT EXISTS impact_notices (
    notice_id           TEXT PRIMARY KEY,
    old_package_id      TEXT NOT NULL,
    new_package_id      TEXT NOT NULL,
    affected_hash       TEXT NOT NULL,
    rule_version        TEXT NOT NULL,
    affected_record_ids TEXT NOT NULL,
    reason              TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_impact_notice_dedupe
    ON impact_notices(old_package_id, new_package_id, affected_hash, rule_version);

CREATE INDEX IF NOT EXISTS ix_impact_notices_new_package
    ON impact_notices(new_package_id, created_at);
