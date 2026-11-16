"""Chain assembly and connection recomputation.

Chains are segments of chronological legs that maintain airport continuity.
When a correction moves a leg, every adjacent connection touching it is
recomputed from the corrected UTC timestamps; connections further away keep
their archived result because their endpoints did not change.

Waiting time is a UTC subtraction, so daylight-saving transitions never add or
remove an hour from the layover.
"""

from datetime import datetime

from app.modules.m5_connections.rules import connection_result, required_minutes
from app.modules.m5_connections.windows import waiting_minutes

MAX_WAIT_MINUTES = 720

def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value)

def _terminal(leg: dict, side: str, airport: str) -> str:
    value = leg.get(f"{side}_terminal")
    if value:
        return str(value)
    from app.modules.m2_airports.registry import airport_version
    try:
        return airport_version(airport, str(leg.get(f"{side}_local", "")).split("T")[0] or "2026-11-16")["terminal"]
    except Exception:
        return ""

def connection(previous: dict, following: dict) -> dict:
    airport = previous["destination"]
    wait = waiting_minutes(_utc(previous["arrival_utc"]), _utc(following["departure_utc"]))
    arrival_terminal = _terminal(previous, "arrival", airport)
    departure_terminal = _terminal(following, "departure", airport)
    try:
        required = required_minutes(airport, arrival_terminal, departure_terminal)
        result = connection_result(wait, required, MAX_WAIT_MINUTES)
        status = result["status"]
    except ValueError as exc:
        # An airport without a connection rule cannot be judged; surface the
        # reason instead of fabricating a valid layover.
        required = None
        status = str(exc) or "UNKNOWN_AIRPORT"
    return {
        "airport": airport,
        "from_leg": previous["leg_key"],
        "to_leg": following["leg_key"],
        "arrival_utc": previous["arrival_utc"],
        "departure_utc": following["departure_utc"],
        "arrival_terminal": arrival_terminal,
        "departure_terminal": departure_terminal,
        "wait_minutes": wait,
        "required_minutes": required,
        "status": status,
    }

def assemble(legs: list[dict]) -> list[dict]:
    """Split chronological legs into airport-continuous chains."""
    chains: list[dict] = []
    current: list[dict] = []
    for leg in legs:
        if current and current[-1]["destination"] != leg["origin"]:
            chains.append(_close(current))
            current = []
        current.append(leg)
    if current:
        chains.append(_close(current))
    return chains

def _close(legs: list[dict]) -> dict:
    connections = [connection(previous, following) for previous, following in zip(legs, legs[1:])]
    departures = [leg["departure_utc"] for leg in legs]
    arrivals = [leg["arrival_utc"] for leg in legs]
    return {
        "chain_id": ">".join([legs[0]["origin"]] + [leg["destination"] for leg in legs]),
        "leg_keys": [leg["leg_key"] for leg in legs],
        "departure_utc": min(departures),
        "arrival_utc": max(arrivals),
        "span_minutes": int((_utc(max(arrivals)) - _utc(min(departures))).total_seconds() // 60),
        "connections": connections,
        "status": "VALID" if all(item["status"] == "VALID" for item in connections) else "INVALID",
    }

def _archived_connections(snapshot: dict) -> dict[tuple[str, str], dict]:
    result: dict[tuple[str, str], dict] = {}
    for chain in snapshot.get("chains", []):
        for item in chain.get("connections", []):
            result[(item["from_leg"], item["to_leg"])] = item
    return result

def _by_key(leg_key: str, legs: list[dict]) -> dict:
    return next((leg for leg in legs if leg["leg_key"] == leg_key), {})

def rebuild_chains(archived: dict, current_legs: list[dict]) -> dict:
    """Compare archived chains with the currently published legs.

    Only connections whose endpoints differ from the archive are recalculated.
    A leg that became isolated is reported as a hole and breaks the chain
    silently rather than being guessed back into place.
    """
    archived_connections = _archived_connections(archived)
    archived_legs = {(leg.get("leg_key"), leg.get("revision")): leg for leg in archived.get("legs", [])}
    active = [leg for leg in current_legs if leg["isolation_code"] is None]
    ordered = sorted(active, key=lambda item: (item["departure_utc"], item["flight_no"], item["leg_key"]))
    chains = assemble(ordered)

    recalculated: list[dict] = []
    for chain in chains:
        for item in chain["connections"]:
            previous = archived_connections.get((item["from_leg"], item["to_leg"]))
            endpoints = (_by_key(item["from_leg"], current_legs), _by_key(item["to_leg"], current_legs))
            endpoint_changed = any(
                (leg.get("leg_key"), leg.get("revision")) not in archived_legs for leg in endpoints
            )
            item["recalculated"] = previous is None or endpoint_changed
            item["changed"] = previous is not None and (
                previous.get("status") != item.get("status") or previous.get("wait_minutes") != item.get("wait_minutes")
            )
            item["archived_status"] = previous.get("status") if previous else None
            item["archived_wait_minutes"] = previous.get("wait_minutes") if previous else None
            if item["recalculated"]:
                recalculated.append(item)

    isolated = [
        {
            "leg_key": leg["leg_key"],
            "isolation_code": leg["isolation_code"],
            "isolation_message": leg["isolation_message"],
            "departure_local": leg["departure_local"],
            "arrival_local": leg["arrival_local"],
            "revision": leg["revision"],
            "reason": leg.get("reason"),
        }
        for leg in current_legs if leg["isolation_code"] is not None
    ]
    return {
        "chains": chains,
        "recalculated_connections": recalculated,
        "isolated_legs": isolated,
        "legs": current_legs,
        "counts": {
            "legs": len(current_legs),
            "active": len(active),
            "isolated": len(isolated),
            "chains": len(chains),
            "connections": sum(len(chain["connections"]) for chain in chains),
        },
        "archived": archived,
    }
