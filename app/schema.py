"""SQLite schema for the itinerary ledger.

The ledger deliberately keeps three independent layers:

* ``leg_raw``       – the physical imported row, byte-for-byte as received.
* ``leg_revisions`` – append-only interpretations of that row (import plus
                      every correction, each with its own reason).
* ``archived_chain`` – the chain as it was archived at import time.  It is a
                      snapshot, never rebuilt from later revisions.

Nothing in the correction path updates or deletes a previous revision or an
archived snapshot, so historical mis-connection evidence stays available.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    import_id       TEXT PRIMARY KEY,
    input_sha256    TEXT NOT NULL,
    source_name     TEXT,
    created_at      TEXT NOT NULL,
    row_count       INTEGER NOT NULL,
    accepted        INTEGER NOT NULL,
    quarantined     INTEGER NOT NULL,
    archived_chain  TEXT
);

CREATE TABLE IF NOT EXISTS leg_raw (
    raw_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id    TEXT NOT NULL,
    line_no      INTEGER NOT NULL,
    leg_key      TEXT,
    raw_json     TEXT NOT NULL,
    raw_line     TEXT NOT NULL,
    imported_at  TEXT NOT NULL,
    UNIQUE(import_id, line_no)
);

CREATE TABLE IF NOT EXISTS leg_revisions (
    revision_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id          TEXT NOT NULL,
    leg_key            TEXT NOT NULL,
    revision           INTEGER NOT NULL,
    flight_no          TEXT NOT NULL,
    flight_date        TEXT NOT NULL,
    origin             TEXT NOT NULL,
    destination        TEXT NOT NULL,
    departure_local    TEXT NOT NULL,
    arrival_local      TEXT NOT NULL,
    departure_fold     INTEGER,
    arrival_fold       INTEGER,
    departure_terminal TEXT,
    arrival_terminal   TEXT,
    departure_utc      TEXT,
    arrival_utc        TEXT,
    duration_minutes   INTEGER,
    origin_zone        TEXT,
    destination_zone   TEXT,
    origin_offset      TEXT,
    destination_offset TEXT,
    isolation_code     TEXT,
    isolation_message  TEXT,
    reason_code        TEXT NOT NULL,
    reason             TEXT NOT NULL,
    source             TEXT NOT NULL,
    release_id         TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE(import_id, leg_key, revision)
);

CREATE INDEX IF NOT EXISTS ix_leg_revisions_lookup
    ON leg_revisions(import_id, leg_key, revision);

CREATE TABLE IF NOT EXISTS releases (
    release_id       TEXT PRIMARY KEY,
    idempotency_key  TEXT NOT NULL,
    import_id        TEXT NOT NULL,
    status           TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    error            TEXT,
    applied_count    INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_releases_idempotency
    ON releases(import_id, idempotency_key);

CREATE TABLE IF NOT EXISTS release_items (
    release_id    TEXT NOT NULL,
    leg_key       TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    revision_id   INTEGER,
    applied       INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    PRIMARY KEY(release_id, leg_key)
);
"""
