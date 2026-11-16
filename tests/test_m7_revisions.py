import json
import pathlib

import pytest
from fastapi.testclient import TestClient

import app.store as store
from app.main import app
from app.modules.m4_itinerary import ledger as ledger_module
from app.modules.m4_itinerary.chain import assemble, rebuild_chains

SAMPLE = (pathlib.Path(__file__).resolve().parents[1] / "sample_data" / "flight_legs_dirty.csv").read_bytes()

CHAIN = """flight_no,flight_date,origin,destination,departure_local,arrival_local,departure_fold,arrival_fold
MU1,2026-11-16,PVG,JFK,2026-11-16T09:00,2026-11-16T10:30,,
UA2,2026-11-16,JFK,LHR,2026-11-16T13:00,2026-11-17T01:00,,
BA3,2026-11-17,LHR,PVG,2026-11-17T03:00,2026-11-17T12:00,,
"""

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "ledger.sqlite3")
    return TestClient(app)

def import_text(client, text: str, name: str = "legs.csv") -> str:
    response = client.post("/api/imports", files={"file": (name, text.encode(), "text/csv")})
    assert response.status_code == 200, response.text
    return response.json()["import_id"]

def legs_by_key(client, import_id: str) -> dict:
    response = client.get(f"/api/imports/{import_id}/legs")
    assert response.status_code == 200
    return {leg["leg_key"]: leg for leg in response.json()["legs"]}

# ---------------------------------------------------------------- import

def test_raw_physical_lines_are_preserved(client):
    import_id = import_text(client, SAMPLE.decode())
    assert import_id.startswith("flightclock-rules-1-")
    response = client.get(f"/api/imports/{import_id}/legs")
    legs = legs_by_key(client, import_id)
    mu = legs["MU5101|2026-11-16|PVG|JFK"]
    assert mu["raw"][0]["line_no"] == 2
    assert mu["raw"][0]["raw_line"].startswith("MU5101,2026-11-16,PVG,JFK")

def test_import_is_idempotent_on_same_bytes(client):
    first = import_text(client, CHAIN)
    second = import_text(client, CHAIN, name="renamed.csv")
    assert first == second
    assert len(legs_by_key(client, first)) == 3

def test_truncated_tail_is_isolated_but_kept(client):
    import_id = import_text(client, CHAIN.rstrip("\n"))
    legs = legs_by_key(client, import_id)
    assert legs["BA3|2026-11-17|LHR|PVG"]["isolation_code"] == "TRUNCATED_INPUT"
    assert len(legs["BA3|2026-11-17|LHR|PVG"]["raw"]) == 1

def test_dst_gap_and_unselected_overlap_are_isolated(client):
    import_id = import_text(client, SAMPLE.decode())
    legs = legs_by_key(client, import_id)
    assert legs["BA178|2026-03-08|JFK|LHR"]["isolation_code"] == "LOCAL_TIME_GAP"
    # UA857 imports with fold=1 so it is accepted; clearing the fold isolates it.
    assert legs["UA857|2026-11-01|JFK|SFO"]["isolation_code"] is None
    result = client.post(
        f"/api/imports/{import_id}/legs/UA857|2026-11-01|JFK|SFO/corrections",
        json={"patch": {"departure_fold": None}, "expected_revision": 1, "reason": "重叠小时待复核"},
    )
    assert result.status_code == 200
    assert result.json()["isolation_code"] == "OVERLAP_FOLD_REQUIRED"

def test_duplicate_and_conflict_lines_get_distinct_keys(client):
    import_id = import_text(client, SAMPLE.decode())
    legs = legs_by_key(client, import_id)
    assert legs["UA857|2026-11-01|JFK|SFO#L6"]["isolation_code"] == "VALUE_CONFLICT"

# --------------------------------------------------------- correction

def test_correction_appends_revision_and_keeps_original(client):
    import_id = import_text(client, CHAIN)
    leg_key = "UA2|2026-11-16|JFK|LHR"
    response = client.post(
        f"/api/imports/{import_id}/legs/{leg_key}/corrections",
        json={"patch": {"departure_local": "2026-11-16T12:00"}, "expected_revision": 1, "reason": "承运人补正"},
    )
    assert response.status_code == 200 and response.json()["revision"] == 2
    history = client.get(f"/api/imports/{import_id}/legs/{leg_key}/history").json()
    assert [rev["revision"] for rev in history["revisions"]] == [1, 2]
    assert history["revisions"][0]["departure_local"] == "2026-11-16T13:00"
    assert history["revisions"][1]["reason"] == "承运人补正"
    assert history["revisions"][1]["source"] == "correction"
    assert history["raw"][0]["line_no"] == 3

def test_correction_requires_reason(client):
    import_id = import_text(client, CHAIN)
    response = client.post(
        f"/api/imports/{import_id}/legs/MU1|2026-11-16|PVG|JFK/corrections",
        json={"patch": {"arrival_local": "2026-11-16T10:45"}, "expected_revision": 1, "reason": "  "},
    )
    assert response.status_code == 422 and response.json()["detail"]["code"] == "REASON_REQUIRED"

def test_stale_revision_is_blocked(client):
    import_id = import_text(client, CHAIN)
    url = f"/api/imports/{import_id}/legs/MU1|2026-11-16|PVG|JFK/corrections"
    first = client.post(url, json={"patch": {"arrival_local": "2026-11-16T10:45"}, "expected_revision": 1, "reason": "第一次"})
    assert first.status_code == 200
    second = client.post(url, json={"patch": {"arrival_local": "2026-11-16T10:50"}, "expected_revision": 1, "reason": "并发旧版本"})
    assert second.status_code == 409 and second.json()["detail"]["code"] == "STALE_REVISION"
    history = client.get(f"/api/imports/{import_id}/legs/MU1|2026-11-16|PVG|JFK/history").json()
    assert len(history["revisions"]) == 2

def test_correction_recomputes_only_adjacent_connections(client):
    import_id = import_text(client, CHAIN)
    before = client.get(f"/api/imports/{import_id}/itinerary").json()["current"]
    waits = {(item["from_leg"], item["to_leg"]): item["wait_minutes"] for item in before["chains"][0]["connections"]}
    assert waits[("UA2|2026-11-16|JFK|LHR", "BA3|2026-11-17|LHR|PVG")] == 120

    response = client.post(
        f"/api/imports/{import_id}/releases",
        json={"idempotency_key": "rel-adjacent", "items": [
            {"leg_key": "UA2|2026-11-16|JFK|LHR", "expected_revision": 1,
             "patch": {"departure_local": "2026-11-16T10:45"}, "reason": "起飞提前"},
        ]},
    )
    assert response.status_code == 200 and response.json()["status"] == "published"

    after = client.get(f"/api/imports/{import_id}/itinerary").json()["current"]
    connections = {(item["from_leg"], item["to_leg"]): item for item in after["chains"][0]["connections"]}
    jfk = connections[("MU1|2026-11-16|PVG|JFK", "UA2|2026-11-16|JFK|LHR")]
    lhr = connections[("UA2|2026-11-16|JFK|LHR", "BA3|2026-11-17|LHR|PVG")]
    assert jfk["status"] == "TOO_SHORT" and jfk["recalculated"] is True
    # UA2's own departure moved, so its LHR-side connection is re-evaluated too,
    # but the wait is unchanged because only the JFK departure was corrected.
    assert lhr["wait_minutes"] == 120 and lhr["recalculated"] is True
    assert lhr["changed"] is False and lhr["archived_wait_minutes"] == 120

def test_archived_chain_keeps_original_times_and_verdict(client):
    import_id = import_text(client, CHAIN)
    client.post(
        f"/api/imports/{import_id}/legs/UA2|2026-11-16|JFK|LHR/corrections",
        json={"patch": {"departure_local": "2026-11-16T10:45"}, "expected_revision": 1, "reason": "起飞提前"},
    )
    archived = client.get(f"/api/imports/{import_id}/itinerary?select=archived").json()["view"]
    chain = archived["chains"][0]
    archived_leg = next(leg for leg in archived["legs"] if leg["leg_key"] == "UA2|2026-11-16|JFK|LHR")
    assert archived_leg["departure_utc"] == "2026-11-16T18:00:00Z"
    assert chain["connections"][0]["status"] == "VALID" and chain["connections"][0]["wait_minutes"] == 150
    assert archived_leg["revision"] == 1

def test_selected_overlap_hour_changes_utc_but_archive_keeps_fold(client):
    rows = (
        "flight_no,flight_date,origin,destination,departure_local,arrival_local,departure_fold,arrival_fold\n"
        "UA9,2026-11-01,JFK,SFO,2026-11-01T01:30,2026-11-01T05:00,1,\n"
    )
    import_id = import_text(client, rows)
    leg = legs_by_key(client, import_id)["UA9|2026-11-01|JFK|SFO"]
    assert leg["departure_utc"] == "2026-11-01T06:30:00Z"
    client.post(
        f"/api/imports/{import_id}/legs/UA9|2026-11-01|JFK|SFO/corrections",
        json={"patch": {"departure_fold": 0}, "expected_revision": 1, "reason": "选择较早瞬时"},
    )
    current = legs_by_key(client, import_id)["UA9|2026-11-01|JFK|SFO"]
    assert current["departure_utc"] == "2026-11-01T05:30:00Z" and current["departure_fold"] == 0
    archived = client.get(f"/api/imports/{import_id}/itinerary?select=archived").json()["view"]
    assert archived["legs"][0]["departure_fold"] == 1
    assert archived["legs"][0]["departure_utc"] == "2026-11-01T06:30:00Z"

def test_correction_isolated_leg_breaks_chain_without_guessing(client):
    import_id = import_text(client, CHAIN)
    client.post(
        f"/api/imports/{import_id}/legs/UA2|2026-11-16|JFK|LHR/corrections",
        json={"patch": {"destination": "XXX"}, "expected_revision": 1, "reason": "机场待确认"},
    )
    current = client.get(f"/api/imports/{import_id}/itinerary").json()["current"]
    assert current["isolated_legs"][0]["leg_key"] == "UA2|2026-11-16|JFK|LHR"
    assert all("UA2|2026-11-16|JFK|LHR" not in chain["leg_keys"] for chain in current["chains"])

# --------------------------------------------------------------- release

def test_release_resumes_after_interruption(client):
    import_id = import_text(client, CHAIN)
    items = [
        {"leg_key": "MU1|2026-11-16|PVG|JFK", "expected_revision": 1,
         "patch": {"arrival_local": "2026-11-16T10:45"}, "reason": "补正一"},
        {"leg_key": "UA2|2026-11-16|JFK|LHR", "expected_revision": 1,
         "patch": {"departure_local": "2026-11-16T13:30"}, "reason": "补正二"},
    ]
    partial = client.post(
        f"/api/imports/{import_id}/releases",
        json={"idempotency_key": "rel-resume", "items": items, "stop_after": 1},
    ).json()
    assert partial["status"] == "partial" and partial["applied_count"] == 1
    assert partial["resumable"] is True

    resumed = client.post(f"/api/releases/{partial['release_id']}/resume", json={}).json()
    assert resumed["status"] == "published" and resumed["applied_count"] == 2
    legs = legs_by_key(client, import_id)
    assert legs["MU1|2026-11-16|PVG|JFK"]["revision"] == 2
    assert legs["UA2|2026-11-16|JFK|LHR"]["revision"] == 2

def test_release_replay_does_not_duplicate_revisions(client):
    import_id = import_text(client, CHAIN)
    items = [{"leg_key": "MU1|2026-11-16|PVG|JFK", "expected_revision": 1,
              "patch": {"arrival_local": "2026-11-16T10:45"}, "reason": "补正"}]
    release = client.post(f"/api/imports/{import_id}/releases", json={"idempotency_key": "rel-idem", "items": items}).json()
    replay = client.post(f"/api/imports/{import_id}/releases", json={"idempotency_key": "rel-idem", "items": items}).json()
    assert replay["release_id"] == release["release_id"] and replay["applied_count"] == 1
    history = client.get(f"/api/imports/{import_id}/legs/MU1|2026-11-16|PVG|JFK/history").json()
    assert [rev["revision"] for rev in history["revisions"]] == [1, 2]
    assert len(replay["revisions"]) == 1

def test_failed_release_keeps_archived_chain_readable(client):
    import_id = import_text(client, CHAIN)
    result = client.post(
        f"/api/imports/{import_id}/releases",
        json={"idempotency_key": "rel-stale", "items": [
            {"leg_key": "MU1|2026-11-16|PVG|JFK", "expected_revision": 99,
             "patch": {"arrival_local": "2026-11-16T10:45"}, "reason": "旧基线"},
        ]},
    ).json()
    assert result["status"] == "failed"
    assert "STALE_REVISION" in result["error"]
    archived = client.get(f"/api/imports/{import_id}/itinerary?select=archived")
    assert archived.status_code == 200 and archived.json()["view"]["chains"]

def test_airport_timezone_looked_up_per_timestamp_date(client):
    rows = (
        "flight_no,flight_date,origin,destination,departure_local,arrival_local,departure_fold,arrival_fold\n"
        "DL10,2026-03-07,JFK,LHR,2026-03-07T22:00,2026-03-08T09:00,,\n"
    )
    import_id = import_text(client, rows)
    leg = legs_by_key(client, import_id)["DL10|2026-03-07|JFK|LHR"]
    assert leg["origin_zone"] == "America/New_York" and leg["destination_zone"] == "Europe/London"
    assert leg["origin_offset"] == "-05:00" and leg["destination_offset"] == "+00:00"
    assert leg["duration_minutes"] == 360

def test_page_offers_both_views_and_utc_evidence(client):
    import_id = import_text(client, CHAIN)
    current = client.get(f"/itineraries?import_id={import_id}&select=current")
    archived = client.get(f"/itineraries?import_id={import_id}&select=archived")
    assert current.status_code == 200 and archived.status_code == 200
    assert "现行 UTC 航链" in current.text and "归档航链" in archived.text
    assert "等待分钟" in current.text and "已采信" in current.text

def test_isolated_leg_is_selectable_for_correction(client):
    import_id = import_text(client, SAMPLE.decode())
    page = client.get(f"/itineraries?import_id={import_id}&select=current").text
    # A quarantined row (DST gap) must still be reachable from the picker.
    assert "BA178|2026-03-08|JFK|LHR" in page
    leg_key = "BA178|2026-03-08|JFK|LHR"
    response = client.post(
        f"/api/imports/{import_id}/legs/{leg_key}/corrections",
        json={"patch": {"departure_local": "2026-03-08T03:30"}, "expected_revision": 1, "reason": "跳时空档补正"},
    )
    assert response.status_code == 200 and response.json()["isolation_code"] is None
    history = client.get(f"/api/imports/{import_id}/legs/{leg_key}/history").json()
    assert history["revisions"][0]["isolation_code"] == "LOCAL_TIME_GAP"
    assert history["revisions"][1]["isolation_code"] is None

def test_chain_assemble_splits_on_airport_discontinuity():
    legs = [
        {"leg_key": "A", "origin": "PVG", "destination": "JFK", "flight_no": "A",
         "departure_utc": "2026-11-16T01:00:00Z", "arrival_utc": "2026-11-16T15:30:00Z", "destination_local": "x"},
        {"leg_key": "B", "origin": "LHR", "destination": "PVG", "flight_no": "B",
         "departure_utc": "2026-11-17T03:00:00Z", "arrival_utc": "2026-11-17T12:00:00Z", "destination_local": "x"},
    ]
    assert len(assemble(legs)) == 2
