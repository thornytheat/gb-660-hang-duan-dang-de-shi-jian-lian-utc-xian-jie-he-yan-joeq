"""Interpret one physical leg into UTC, or isolate it with a stable reason.

The interpreter is intentionally free of persistence: both the initial import
and every correction run through the same function, so an uncorrected archived
chain and a freshly published chain always share one rule set.

Airport time zone is resolved per timestamp date, not per flight date: the
departure uses the date of ``departure_local`` at the origin airport and the
arrival uses the date of ``arrival_local`` at the destination airport.  A leg
that crosses a date line therefore picks up the right zone version on each end.
"""

from datetime import datetime, timezone

from app.modules.m1_legs.keys import normalize_flight_no
from app.modules.m2_airports.contracts import normalize_airport
from app.modules.m2_airports.registry import airport_version
from app.modules.m3_localtime.evidence import offset_evidence
from app.modules.m3_localtime.resolve import local_candidates, resolve_local, utc_iso

FOLD_FIELDS = ("departure_fold", "arrival_fold")

def _fold(value) -> int | None:
    if value is None:
        return None
    token = str(value).strip()
    if token == "":
        return None
    if token not in ("0", "1"):
        raise ValueError("BAD_FOLD")
    return int(token)

def _local_date(local_value: str, field: str) -> str:
    try:
        return datetime.fromisoformat(local_value).date().isoformat()
    except ValueError as exc:
        raise ValueError("BAD_FORMAT") from exc

def _zone_at(airport: str, at: str) -> dict:
    try:
        return airport_version(airport, at)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

def isolate(code: str, detail: str = "") -> dict:
    return {"ok": False, "isolation_code": code, "isolation_detail": detail}

def interpret(row: dict) -> dict:
    """Return ``{"ok": True, ...leg}`` or ``{"ok": False, "isolation_code": ...}``."""
    try:
        flight_no = normalize_flight_no(str(row.get("flight_no", "")))
    except ValueError:
        return isolate("BAD_FLIGHT_NO")
    try:
        origin = normalize_airport(str(row.get("origin", "")))
        destination = normalize_airport(str(row.get("destination", "")))
    except ValueError:
        return isolate("BAD_AIRPORT")

    departure_local = str(row.get("departure_local", "")).strip()
    arrival_local = str(row.get("arrival_local", "")).strip()
    flight_date = str(row.get("flight_date", "")).strip()
    if not departure_local or not arrival_local or not flight_date:
        return isolate("EMPTY_VALUE", "航班日期与起降当地时刻必须同时存在")
    try:
        datetime.fromisoformat(flight_date)
    except ValueError:
        return isolate("BAD_FORMAT", "flight_date 不是 ISO 日期")

    try:
        departure_fold = _fold(row.get("departure_fold"))
        arrival_fold = _fold(row.get("arrival_fold"))
    except ValueError:
        return isolate("BAD_FOLD")

    try:
        departure_date = _local_date(departure_local, "departure_local")
        arrival_date = _local_date(arrival_local, "arrival_local")
        departure_zone = _zone_at(origin, departure_date)
        arrival_zone = _zone_at(destination, arrival_date)
    except ValueError as exc:
        code = str(exc)
        if code in ("UNKNOWN_AIRPORT", "TIMEZONE_VERSION_OVERLAP"):
            return isolate(code, f"{origin}->{destination}")
        return isolate("BAD_FORMAT", f"当地时刻无法解析: {code}")

    try:
        departure = _resolve(departure_local, departure_zone["timezone"], departure_fold)
        arrival = _resolve(arrival_local, arrival_zone["timezone"], arrival_fold)
    except ValueError as exc:
        code = str(exc)
        if code == "NONEXISTENT_LOCAL_TIME":
            return isolate("LOCAL_TIME_GAP", f"{origin}/{destination}")
        if code == "AMBIGUOUS_LOCAL_TIME":
            return isolate("OVERLAP_FOLD_REQUIRED", f"{origin}/{destination}")
        return isolate("BAD_FOLD") if code == "BAD_FOLD" else isolate("BAD_FORMAT", code)

    departure_utc = departure.astimezone(timezone.utc)
    arrival_utc = arrival.astimezone(timezone.utc)
    duration = int((arrival_utc - departure_utc).total_seconds() // 60)
    if duration <= 0 or duration > 18 * 60:
        return isolate("INVALID_FLIGHT_DURATION", f"{duration} 分钟")

    departure_evidence = offset_evidence(departure)
    arrival_evidence = offset_evidence(arrival)
    return {
        "ok": True,
        "flight_no": flight_no,
        "flight_date": str(row.get("flight_date", "")).strip(),
        "origin": origin,
        "destination": destination,
        "departure_local": departure_local,
        "arrival_local": arrival_local,
        "departure_fold": departure_fold,
        "arrival_fold": arrival_fold,
        "departure_utc": utc_iso(departure_utc),
        "arrival_utc": utc_iso(arrival_utc),
        "duration_minutes": duration,
        "origin_zone": departure_zone["timezone"],
        "destination_zone": arrival_zone["timezone"],
        "origin_offset": departure_evidence["offset"],
        "destination_offset": arrival_evidence["offset"],
        "departure_candidates": len(local_candidates(departure_local, departure_zone["timezone"])),
        "arrival_candidates": len(local_candidates(arrival_local, arrival_zone["timezone"])),
    }

def _resolve(local_value: str, zone: str, fold: int | None) -> datetime:
    return resolve_local(local_value, zone, fold)
