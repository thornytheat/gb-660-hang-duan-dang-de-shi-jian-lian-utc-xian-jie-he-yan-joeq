"""Append-only leg ledger and the correction release process.

Guarantees this module is responsible for:

* raw physical rows are written once per import and never mutated;
* every interpretation (initial import or correction) is a new revision row,
  carrying its own reason, so the previous explanation stays auditable;
* the chain archived at import time is captured in ``imports.archived_chain``
  and is never recomputed from later revisions;
* corrections are guarded by an expected revision (optimistic concurrency);
* a release is created once per idempotency key and can be resumed after an
  interruption without appending duplicate revisions.
"""

import json
import sqlite3
from datetime import datetime, timezone
from hashlib import sha256

from app.config import RULE_VERSION
from app.errors import DomainError
from app.reasons import ISOLATION_REASONS
from app.store import connect, init_db

from app.modules.m1_legs.ingest import parse_csv

from .interpret import interpret

PATCHABLE = (
    "flight_no",
    "flight_date",
    "origin",
    "destination",
    "departure_local",
    "arrival_local",
    "departure_fold",
    "arrival_fold",
    "departure_terminal",
    "arrival_terminal",
)

LEG_FIELDS = (
    "leg_key",
    "revision",
    "flight_no",
    "flight_date",
    "origin",
    "destination",
    "departure_local",
    "arrival_local",
    "departure_fold",
    "arrival_fold",
    "departure_terminal",
    "arrival_terminal",
    "departure_utc",
    "arrival_utc",
    "duration_minutes",
    "origin_zone",
    "destination_zone",
    "origin_offset",
    "destination_offset",
    "isolation_code",
    "isolation_message",
    "reason_code",
    "reason",
    "source",
    "release_id",
    "created_at",
)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def import_id_for(payload: bytes) -> str:
    return f"{RULE_VERSION}-{sha256(payload).hexdigest()[:12]}"

def _business_key(values: dict) -> str | None:
    parts = [str(values.get(name, "")).strip().upper() for name in ("flight_no", "flight_date", "origin", "destination")]
    if not all(parts):
        return None
    return "|".join(parts)

def _row_key(values: dict, line_no: int, seen: dict[str, dict]) -> tuple[str, str | None, int | None]:
    """Return ``(leg_key, isolation_code, first_line)`` for one physical row."""
    base = _business_key(values)
    if base is None:
        return f"LINE:{line_no}", None, None
    if base not in seen:
        seen[base] = (values, line_no)
        return base, None, None
    prior_values, prior_line = seen[base]
    code = "DUPLICATE_KEY" if prior_values == values else "VALUE_CONFLICT"
    return f"{base}#L{line_no}", code, prior_line

def _blank_leg() -> dict:
    return {
        "flight_no": "",
        "flight_date": "",
        "origin": "",
        "destination": "",
        "departure_local": "",
        "arrival_local": "",
        "departure_fold": None,
        "arrival_fold": None,
        "departure_terminal": None,
        "arrival_terminal": None,
    }

def _leg_from_values(values: dict) -> dict:
    leg = _blank_leg()
    for name in PATCHABLE:
        if name in values and values[name] is not None:
            leg[name] = str(values[name]).strip() if values[name] != "" else ""
    for name in ("departure_fold", "arrival_fold"):
        token = str(leg[name]).strip() if leg[name] is not None else ""
        leg[name] = int(token) if token in ("0", "1") else (None if token == "" else token)
    return leg

def _interpreted_row(leg: dict, fallback_code: str | None, fallback_detail: str) -> dict:
    isolated = {**leg, "departure_utc": None, "arrival_utc": None, "duration_minutes": None,
                "origin_zone": None, "destination_zone": None, "origin_offset": None,
                "destination_offset": None}
    if fallback_code:
        message = ISOLATION_REASONS.get(fallback_code, fallback_detail)
        if fallback_detail and fallback_code in ("DUPLICATE_KEY", "VALUE_CONFLICT", "TRUNCATED_INPUT", "BAD_FORMAT"):
            message = f"{message}（{fallback_detail}）"
        return {**isolated, "isolation_code": fallback_code, "isolation_message": message}
    result = interpret(leg)
    if not result["ok"]:
        code = result["isolation_code"]
        detail = result.get("isolation_detail") or ""
        message = ISOLATION_REASONS.get(code, detail)
        if detail:
            message = f"{message}（{detail}）"
        return {**isolated, "isolation_code": code, "isolation_message": message}
    merged = {**leg, **{key: value for key, value in result.items() if key != "ok"}}
    merged["isolation_code"] = None
    merged["isolation_message"] = None
    return merged

def _insert_revision(db, import_id: str, leg_key: str, revision: int, leg: dict, *,
                     reason_code: str, reason: str, source: str, created_at: str,
                     release_id: str | None = None) -> int:
    columns = ("import_id", "leg_key", "revision", *LEG_FIELDS[2:], "reason_code", "reason", "source", "release_id", "created_at")
    payload = {
        "import_id": import_id,
        "leg_key": leg_key,
        "revision": revision,
        "reason_code": reason_code,
        "reason": reason,
        "source": source,
        "release_id": release_id,
        "created_at": created_at,
    }
    for name in LEG_FIELDS[2:]:
        payload.setdefault(name, leg.get(name))
    placeholders = ",".join("?" for _ in columns)
    db.execute(f"INSERT INTO leg_revisions ({','.join(columns)}) VALUES ({placeholders})", [payload[name] for name in columns])
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]

def _archived_snapshot(legs: list[dict]) -> dict:
    """Capture the chain exactly as it was interpreted at import time.

    Accepted legs form the archived chains; isolated legs are kept beside them
    so the original isolation evidence can still be reviewed after the leg has
    been corrected in the current view.
    """
    from .chain import assemble

    active = sorted((leg for leg in legs if leg["isolation_code"] is None),
                    key=lambda item: (item["departure_utc"], item["flight_no"], item["leg_key"]))
    isolated = [leg for leg in legs if leg["isolation_code"] is not None]
    return {"archived_at": now_iso(), "legs": active, "chains": assemble(active), "isolated": isolated}

def create_import(payload: bytes, filename: str | None = None) -> dict:
    init_db()
    parsed = parse_csv(payload)
    import_id = import_id_for(payload)
    created = now_iso()
    columns = parsed["columns"]

    with connect() as db:
        existing = db.execute("SELECT import_id FROM imports WHERE import_id = ?", (import_id,)).fetchone()
        if existing:
            return {"import_id": import_id, "reused": True, **summary(db, import_id)}

        db.execute("BEGIN IMMEDIATE")
        try:
            seen: dict[str, dict] = {}
            revisions: list[dict] = []
            for record in parsed["records"]:
                values = record["values"]
                leg_key, conflict_code, first_line = _row_key(values, record["line_no"], seen)
                raw_json = json.dumps(values, ensure_ascii=False, sort_keys=True)
                db.execute(
                    "INSERT INTO leg_raw (import_id, line_no, leg_key, raw_json, raw_line, imported_at) VALUES (?,?,?,?,?,?)",
                    (import_id, record["line_no"], leg_key, raw_json, record["raw_line"], created),
                )
                leg = _leg_from_values(values)
                code = record["parse_error"] or conflict_code
                if record["parse_error"] == "TRUNCATED_INPUT":
                    detail = "输入尾部不完整"
                elif conflict_code:
                    detail = f"与第 {first_line} 行业务键冲突"
                else:
                    detail = ""
                interpreted = _interpreted_row(leg, code, detail)
                interpreted["leg_key"] = leg_key
                interpreted["revision"] = 1
                revisions.append(interpreted)
                _insert_revision(
                    db, import_id, leg_key, 1, interpreted,
                    reason_code=interpreted["isolation_code"] or "IMPORTED",
                    reason="导入采信" if interpreted["isolation_code"] is None else interpreted["isolation_message"],
                    source="import", created_at=created,
                )

            snapshot = _archived_snapshot(revisions)
            accepted = sum(1 for leg in revisions if leg["isolation_code"] is None)
            db.execute(
                "INSERT INTO imports (import_id, input_sha256, source_name, created_at, row_count, accepted, quarantined, archived_chain) VALUES (?,?,?,?,?,?,?,?)",
                (import_id, sha256(payload).hexdigest(), filename, created, len(revisions), accepted,
                 len(revisions) - accepted, json.dumps(snapshot, ensure_ascii=False)),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    with connect() as db:
        result = {"import_id": import_id, "reused": False, **summary(db, import_id), "columns": columns}
    return result

def summary(db_or_path, import_id: str) -> dict:
    if isinstance(db_or_path, sqlite3.Connection):
        row = db_or_path.execute("SELECT * FROM imports WHERE import_id = ?", (import_id,)).fetchone()
    else:
        with db_or_path as db:
            row = db.execute("SELECT * FROM imports WHERE import_id = ?", (import_id,)).fetchone()
    if not row:
        raise DomainError("UNKNOWN_IMPORT", "导入批次不存在", status=404)
    return {
        "input_sha256": row["input_sha256"],
        "source_name": row["source_name"],
        "created_at": row["created_at"],
        "row_count": row["row_count"],
        "accepted": row["accepted"],
        "quarantined": row["quarantined"],
    }

def get_import(import_id: str) -> dict:
    init_db()
    with connect() as db:
        row = db.execute("SELECT * FROM imports WHERE import_id = ?", (import_id,)).fetchone()
        if not row:
            raise DomainError("UNKNOWN_IMPORT", "导入批次不存在", status=404)
        archive = json.loads(row["archived_chain"] or "{}")
    return {
        "import_id": row["import_id"],
        "input_sha256": row["input_sha256"],
        "source_name": row["source_name"],
        "created_at": row["created_at"],
        "row_count": row["row_count"],
        "accepted": row["accepted"],
        "quarantined": row["quarantined"],
        "archived_chain": archive,
    }

def list_imports() -> list[dict]:
    init_db()
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT import_id, source_name, created_at, row_count, accepted, quarantined FROM imports ORDER BY created_at DESC")]

def _row_to_leg(row: sqlite3.Row) -> dict:
    return {name: row[name] for name in LEG_FIELDS}

def current_legs(import_id: str) -> list[dict]:
    init_db()
    with connect() as db:
        rows = db.execute(
            """
            SELECT r.* FROM leg_revisions r
            JOIN (SELECT leg_key, MAX(revision) AS revision FROM leg_revisions
                  WHERE import_id = ? GROUP BY leg_key) latest
              ON latest.leg_key = r.leg_key AND latest.revision = r.revision
            WHERE r.import_id = ?
            ORDER BY r.leg_key
            """,
            (import_id, import_id),
        ).fetchall()
    return [_row_to_leg(row) for row in rows]

def raw_rows(import_id: str) -> list[dict]:
    init_db()
    with connect() as db:
        rows = db.execute("SELECT * FROM leg_raw WHERE import_id = ? ORDER BY line_no", (import_id,)).fetchall()
    return [dict(row) for row in rows]

def leg_history(import_id: str, leg_key: str) -> dict:
    init_db()
    with connect() as db:
        raw = db.execute("SELECT * FROM leg_raw WHERE import_id = ? AND leg_key = ? ORDER BY line_no", (import_id, leg_key)).fetchall()
        revisions = db.execute("SELECT * FROM leg_revisions WHERE import_id = ? AND leg_key = ? ORDER BY revision", (import_id, leg_key)).fetchall()
    if not revisions:
        raise DomainError("LEG_NOT_FOUND", "航段不存在", status=404)
    return {
        "leg_key": leg_key,
        "raw": [dict(row) for row in raw],
        "revisions": [_row_to_leg(row) | {"revision_id": row["revision_id"]} for row in revisions],
    }

def _current_revision(db, import_id: str, leg_key: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM leg_revisions WHERE import_id = ? AND leg_key = ? ORDER BY revision DESC LIMIT 1",
        (import_id, leg_key),
    ).fetchone()

def _apply_patch(base: dict, patch: dict) -> dict:
    if not isinstance(patch, dict) or not patch:
        raise DomainError("BAD_FORMAT", "更正至少包含一个字段")
    merged = {name: base.get(name) for name in PATCHABLE}
    for name, value in patch.items():
        if name not in PATCHABLE:
            raise DomainError("BAD_FORMAT", f"不可更正的字段: {name}")
        merged[name] = "" if value is None else str(value).strip()
    return merged

def correct_leg(import_id: str, leg_key: str, patch: dict, expected_revision: int, reason: str,
                release_id: str | None = None, db=None) -> dict:
    """Append one revision.  Caller may pass an open transaction via ``db``."""
    if not str(reason or "").strip():
        raise DomainError("REASON_REQUIRED", ISOLATION_REASONS["REASON_REQUIRED"])

    def work(conn):
        current = conn.execute(
            "SELECT * FROM leg_revisions WHERE import_id = ? AND leg_key = ? ORDER BY revision DESC LIMIT 1",
            (import_id, leg_key),
        ).fetchone()
        if not current:
            raise DomainError("LEG_NOT_FOUND", ISOLATION_REASONS["LEG_NOT_FOUND"], status=404)
        if int(current["revision"]) != int(expected_revision):
            raise DomainError(
                "STALE_REVISION",
                f"{ISOLATION_REASONS['STALE_REVISION']}（当前 r{current['revision']}，提交 r{expected_revision}）",
                status=409,
            )
        merged_values = _apply_patch(_row_to_leg(current), patch)
        leg = _leg_from_values(merged_values)
        interpreted = _interpreted_row(leg, None, "")

        # A corrected leg must not collide with another current leg's business
        # key: the ledger key stays stable, but two rows claiming to be the same
        # flight would make the chain ambiguous.  Sibling rows that already share
        # this base key (the imported duplicate/conflict pairs) are excluded.
        business = _business_key(interpreted)
        if business:
            base = leg_key.split("#")[0]
            others = conn.execute(
                """
                SELECT r.* FROM leg_revisions r
                JOIN (SELECT leg_key, MAX(revision) AS revision FROM leg_revisions
                      WHERE import_id = ? GROUP BY leg_key) latest
                  ON latest.leg_key = r.leg_key AND latest.revision = r.revision
                WHERE r.import_id = ? AND r.leg_key != ?
                """,
                (import_id, import_id, leg_key),
            ).fetchall()
            if any(
                row["leg_key"].split("#")[0] != base and _business_key(_row_to_leg(row)) == business
                for row in others
            ):
                raise DomainError(
                    "VALUE_CONFLICT",
                    f"更正后的业务键 {business} 与现有航段冲突，未写入",
                    status=409,
                )

        # Keep the ledger identity stable; a correction never re-keys a row.
        interpreted["leg_key"] = leg_key
        revision = int(current["revision"]) + 1
        created = now_iso()
        revision_id = _insert_revision(
            conn, import_id, leg_key, revision, interpreted,
            reason_code=interpreted["isolation_code"] or "CORRECTED",
            reason=str(reason).strip(), source="correction", created_at=created,
            release_id=release_id,
        )
        return {
            "leg_key": leg_key,
            "revision": revision,
            "revision_id": revision_id,
            "previous_revision": int(current["revision"]),
            "isolation_code": interpreted["isolation_code"],
            "departure_utc": interpreted["departure_utc"],
            "arrival_utc": interpreted["arrival_utc"],
        }

    if db is not None:
        return work(db)
    init_db()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = work(conn)
            conn.execute("COMMIT")
            return result
        except Exception:
            conn.execute("ROLLBACK")
            raise

def create_release(import_id: str, idempotency_key: str, items: list[dict], stop_after: int | None = None) -> dict:
    """Create or replay a correction release.

    The first call stores the payload and applies items one by one; each item
    commits independently so an interruption leaves the applied work saved.
    Replaying the same idempotency key never appends new revisions.
    """
    init_db()
    if not str(idempotency_key or "").strip():
        raise DomainError("BAD_FORMAT", "缺少幂等键 idempotency_key")
    if not items:
        raise DomainError("BAD_FORMAT", "发布至少包含一条更正")
    get_import(import_id)

    release_id = f"REL-{sha256(f'{import_id}|{idempotency_key}'.encode()).hexdigest()[:12]}"
    with connect() as db:
        existing = db.execute("SELECT * FROM releases WHERE release_id = ?", (release_id,)).fetchone()
        if existing:
            if existing["status"] != "published":
                apply_release(release_id, stop_after=stop_after)
            return release_detail(release_id)
        db.execute(
            "INSERT INTO releases (release_id, idempotency_key, import_id, status, payload_json, created_at, updated_at, applied_count) VALUES (?,?,?,?,?,?,?,0)",
            (release_id, idempotency_key, import_id, "pending", json.dumps(items, ensure_ascii=False), now_iso(), now_iso()),
        )
        for item in items:
            db.execute(
                "INSERT INTO release_items (release_id, leg_key, revision, applied) VALUES (?,?,?,0)",
                (release_id, item.get("leg_key", ""), int(item.get("expected_revision", 0))),
            )
    apply_release(release_id, stop_after=stop_after)
    return release_detail(release_id)

def resume_release(release_id: str, stop_after: int | None = None) -> dict:
    init_db()
    with connect() as db:
        row = db.execute("SELECT * FROM releases WHERE release_id = ?", (release_id,)).fetchone()
    if not row:
        raise DomainError("UNKNOWN_IMPORT", "发布记录不存在", status=404)
    if row["status"] == "published":
        return release_detail(release_id)
    apply_release(release_id, stop_after=stop_after)
    return release_detail(release_id)

def apply_release(release_id: str, stop_after: int | None = None) -> None:
    applied_now = 0
    with connect() as db:
        release = db.execute("SELECT * FROM releases WHERE release_id = ?", (release_id,)).fetchone()
    if not release:
        return
    payload = json.loads(release["payload_json"])
    import_id = release["import_id"]

    for item in payload:
        if stop_after is not None and applied_now >= stop_after:
            break
        leg_key = item.get("leg_key", "")
        with connect() as db:
            done = db.execute("SELECT applied, error FROM release_items WHERE release_id = ? AND leg_key = ?", (release_id, leg_key)).fetchone()
            if done and done["applied"]:
                continue
            try:
                db.execute("BEGIN IMMEDIATE")
                result = correct_leg(
                    import_id, leg_key, item.get("patch", {}), int(item.get("expected_revision", 0)),
                    item.get("reason", ""), release_id=release_id, db=db,
                )
                db.execute(
                    "UPDATE release_items SET applied = 1, error = NULL WHERE release_id = ? AND leg_key = ?",
                    (release_id, leg_key),
                )
                db.execute("UPDATE releases SET updated_at = ? WHERE release_id = ?", (now_iso(), release_id))
                db.execute("COMMIT")
                applied_now += 1
            except DomainError as exc:
                db.execute("ROLLBACK")
                db.execute(
                    "UPDATE release_items SET error = ? WHERE release_id = ? AND leg_key = ?",
                    (f"{exc.code}: {exc.message}", release_id, leg_key),
                )
                db.execute("UPDATE releases SET updated_at = ? WHERE release_id = ?", (now_iso(), release_id))
            except Exception as exc:  # pragma: no cover - defensive
                db.execute("ROLLBACK")
                db.execute(
                    "UPDATE release_items SET error = ? WHERE release_id = ? AND leg_key = ?",
                    (f"UNEXPECTED: {exc}", release_id, leg_key),
                )
                db.execute("UPDATE releases SET updated_at = ? WHERE release_id = ?", (now_iso(), release_id))

    finish_release(release_id)

def finish_release(release_id: str) -> None:
    with connect() as db:
        items = db.execute("SELECT * FROM release_items WHERE release_id = ?", (release_id,)).fetchall()
        applied = sum(1 for item in items if item["applied"])
        errors = [item["error"] for item in items if item["error"]]
        if items and applied == len(items):
            status = "published"
        elif applied:
            status = "partial"
        elif errors:
            status = "failed"
        else:
            status = "pending"
        db.execute(
            "UPDATE releases SET status = ?, applied_count = ?, updated_at = ?, error = ? WHERE release_id = ?",
            (status, applied, now_iso(), "; ".join(errors) if errors else None, release_id),
        )

def release_detail(release_id: str) -> dict:
    init_db()
    with connect() as db:
        row = db.execute("SELECT * FROM releases WHERE release_id = ?", (release_id,)).fetchone()
        if not row:
            raise DomainError("UNKNOWN_IMPORT", "发布记录不存在", status=404)
        items = db.execute("SELECT * FROM release_items WHERE release_id = ? ORDER BY rowid", (release_id,)).fetchall()
        revisions = db.execute(
            "SELECT leg_key, revision, reason_code, isolation_code, departure_utc, arrival_utc FROM leg_revisions WHERE release_id = ? ORDER BY revision_id",
            (release_id,),
        ).fetchall()
    return {
        "release_id": row["release_id"],
        "import_id": row["import_id"],
        "status": row["status"],
        "idempotency_key": row["idempotency_key"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "applied_count": row["applied_count"],
        "error": row["error"],
        "items": [dict(item) for item in items],
        "revisions": [dict(revision) for revision in revisions],
        "resumable": row["status"] != "published",
    }

def list_releases(import_id: str) -> list[dict]:
    init_db()
    with connect() as db:
        rows = db.execute(
            "SELECT release_id, status, applied_count, error, created_at, updated_at, idempotency_key FROM releases WHERE import_id = ? ORDER BY created_at DESC LIMIT 20",
            (import_id,),
        ).fetchall()
    return [{**dict(row), "resumable": row["status"] != "published"} for row in rows]
