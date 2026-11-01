-- ENG-T01 directory projections: local-setting heads/slots, legacy ambiguity
-- buckets, declared-game slots, amendment successor links and metric rules.
-- Heads and slots are rebuildable projections; definitions stay in objects.

CREATE TABLE IF NOT EXISTS local_setting_heads (
    series_id            TEXT PRIMARY KEY,
    local_setting_id     TEXT NOT NULL UNIQUE,
    installation_id      TEXT NOT NULL,
    namespace            TEXT NOT NULL,
    raw_name             TEXT NOT NULL,
    menu_path            TEXT NOT NULL,          -- canonical JSON array
    located              INTEGER NOT NULL CHECK (located IN (0, 1)),
    version              INTEGER NOT NULL,
    parent_definition_id TEXT,
    created_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_local_setting_name
    ON local_setting_heads(installation_id, namespace, raw_name);

CREATE TABLE IF NOT EXISTS local_setting_slots (
    installation_id TEXT NOT NULL,
    namespace       TEXT NOT NULL,
    raw_name        TEXT NOT NULL,
    menu_path       TEXT NOT NULL,
    series_id       TEXT NOT NULL REFERENCES local_setting_heads(series_id),
    PRIMARY KEY (installation_id, namespace, raw_name, menu_path)
);

CREATE TABLE IF NOT EXISTS local_setting_ambiguities (
    installation_id TEXT NOT NULL,
    namespace       TEXT NOT NULL,
    raw_name        TEXT NOT NULL,
    menu_path       TEXT NOT NULL,
    series_ids      TEXT NOT NULL,               -- canonical JSON array
    PRIMARY KEY (installation_id, namespace, raw_name, menu_path)
);

CREATE TABLE IF NOT EXISTS local_setting_successors (
    parent_id TEXT PRIMARY KEY,
    child_id  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS game_declaration_slots (
    user_scope_id           TEXT NOT NULL,
    normalized_raw_name     TEXT NOT NULL,
    platform_key            TEXT NOT NULL,       -- explicit unknown sentinel
    series_id               TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    PRIMARY KEY (user_scope_id, normalized_raw_name, platform_key)
);

CREATE TABLE IF NOT EXISTS information_amendment_links (
    successor_id      TEXT PRIMARY KEY,
    source_request_id TEXT NOT NULL UNIQUE,
    root_request_id   TEXT NOT NULL,
    root_version      INTEGER NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_definition_rules (
    metric_key       TEXT NOT NULL,
    registry_version TEXT NOT NULL,
    rule_hash        TEXT NOT NULL,
    definition_id    TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (metric_key, registry_version, rule_hash)
);
