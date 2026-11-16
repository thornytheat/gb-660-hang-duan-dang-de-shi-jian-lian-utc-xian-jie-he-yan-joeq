import os
import sqlite3
from app.config import DATABASE_PATH

CONNECT_TIMEOUT_SECONDS = 5.0

def connect(path=None):
    target = path or DATABASE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(target, timeout=CONNECT_TIMEOUT_SECONDS, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    return db

def init_db():
    from app.schema import SCHEMA
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, accepted INTEGER NOT NULL, quarantined INTEGER NOT NULL)")
        db.executescript(SCHEMA)

def record_run(manifest: dict) -> bool:
    init_db()
    with connect() as db:
        before = db.total_changes
        db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?, ?)", (manifest["run_id"], manifest["input_sha256"], manifest["accepted"], manifest["quarantined"]))
        return db.total_changes > before

def run_count() -> int:
    init_db()
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
