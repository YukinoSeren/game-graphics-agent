-- Unique references required by 07 §4: an owner cannot register the same
-- relation to the same target twice.  `locator` is nullable, and SQLite treats
-- NULLs as distinct, so the index normalises it to '' instead.
CREATE UNIQUE INDEX IF NOT EXISTS ux_object_references
    ON object_references(owner_id, relation, target_id, COALESCE(locator, ''));
