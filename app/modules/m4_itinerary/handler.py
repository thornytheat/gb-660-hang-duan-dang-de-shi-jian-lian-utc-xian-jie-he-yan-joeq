from fastapi import APIRouter, Body, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from app.errors import DomainError
from app.web import page

from . import ledger
from .chain import rebuild_chains
from .service import demo

router = APIRouter()

@router.get("/itineraries", response_class=HTMLResponse)
def view(request: Request):
    imports = ledger.list_imports()
    import_id = request.query_params.get("import_id") or (imports[0]["import_id"] if imports else None)
    select = request.query_params.get("select") or "current"
    rows: list[dict] = []
    view_data = None
    if import_id:
        view_data = itinerary_view(import_id, select)
        rows = [
            {
                "key": leg["leg_key"],
                "status": leg.get("isolation_code") or f"r{leg.get('revision', 1)}",
                "value": leg.get("departure_utc") or "隔离",
            }
            for leg in view_data["selectable"][:40]
        ]
    return page(request, "m4.html", "UTC 航链", "/api/itineraries/build", rows, {
        "imports": imports,
        "import_id": import_id,
        "select": select,
        "view": view_data,
    })

@router.post("/api/itineraries/build")
def execute():
    return demo()

# --------------------------------------------------------------------------
# Import and correction ledger
# --------------------------------------------------------------------------

def _error(exc: DomainError) -> JSONResponse:
    return JSONResponse({"detail": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

@router.post("/api/imports")
async def create_import(file: UploadFile = File(None), text: str = Form(None)):
    payload = await file.read() if file is not None else (text or "").encode()
    try:
        result = ledger.create_import(payload, filename=file.filename if file else None)
    except DomainError as exc:
        return _error(exc)
    except ValueError as exc:
        code = getattr(exc, "code", "BAD_FORMAT")
        return JSONResponse({"detail": {"code": code, "message": str(exc)}}, status_code=422)
    return result

@router.get("/api/imports")
def list_imports():
    return {"imports": ledger.list_imports()}

@router.get("/api/imports/{import_id}/legs")
def list_legs(import_id: str):
    try:
        legs = ledger.current_legs(import_id)
        raw = ledger.raw_rows(import_id)
    except DomainError as exc:
        return _error(exc)
    raw_by_key: dict[str, list[dict]] = {}
    for row in raw:
        raw_by_key.setdefault(row["leg_key"], []).append({"line_no": row["line_no"], "raw_line": row["raw_line"], "raw": row["raw_json"]})
    for leg in legs:
        leg["raw"] = raw_by_key.get(leg["leg_key"], [])
    return {"import_id": import_id, "legs": legs}

@router.get("/api/imports/{import_id}/legs/{leg_key:path}/history")
def leg_history(import_id: str, leg_key: str):
    try:
        return ledger.leg_history(import_id, leg_key)
    except DomainError as exc:
        return _error(exc)

@router.post("/api/imports/{import_id}/legs/{leg_key:path}/corrections")
def correct(import_id: str, leg_key: str, body: dict = Body(...)):
    try:
        return ledger.correct_leg(
            import_id,
            leg_key,
            body.get("patch") or {},
            int(body.get("expected_revision")),
            body.get("reason", ""),
        )
    except DomainError as exc:
        return _error(exc)

@router.post("/api/imports/{import_id}/releases")
def release(import_id: str, body: dict = Body(...)):
    try:
        return ledger.create_release(
            import_id,
            body.get("idempotency_key", ""),
            body.get("items") or [],
            stop_after=body.get("stop_after"),
        )
    except DomainError as exc:
        return _error(exc)

@router.get("/api/imports/{import_id}/releases")
def list_releases(import_id: str):
    try:
        ledger.get_import(import_id)
        return {"releases": ledger.list_releases(import_id)}
    except DomainError as exc:
        return _error(exc)

@router.get("/api/releases/{release_id}")
def get_release(release_id: str):
    try:
        return ledger.release_detail(release_id)
    except DomainError as exc:
        return _error(exc)

@router.post("/api/releases/{release_id}/resume")
def resume_release(release_id: str, body: dict = Body(default={})):
    try:
        return ledger.resume_release(release_id, stop_after=body.get("stop_after"))
    except DomainError as exc:
        return _error(exc)

@router.get("/api/imports/{import_id}/itinerary")
def itinerary(import_id: str, select: str = "current"):
    try:
        return itinerary_view(import_id, select)
    except DomainError as exc:
        return _error(exc)

def itinerary_view(import_id: str, select: str = "current") -> dict:
    if select not in ("current", "archived"):
        raise DomainError("BAD_FORMAT", "select 只能是 current 或 archived")
    record = ledger.get_import(import_id)
    archived = record["archived_chain"] or {}
    current = ledger.current_legs(import_id)
    rebuilt = rebuild_chains(archived, current)
    payload = {
        "import_id": import_id,
        "select": select,
        "archived_at": archived.get("archived_at"),
        "current": rebuilt,
        "archived": {
            "legs": archived.get("legs", []),
            "chains": archived.get("chains", []),
            "isolated": archived.get("isolated", []),
        },
    }
    payload["view"] = payload["archived"] if select == "archived" else {
        "legs": rebuilt["legs"],
        "chains": rebuilt["chains"],
        "isolated": rebuilt["isolated_legs"],
        "recalculated_connections": rebuilt["recalculated_connections"],
    }
    # The correction picker must reach isolated legs too, otherwise the rows
    # that were quarantined for a DST gap or an unselected overlap could never
    # be repaired from the page.
    seen: set[str] = set()
    selectable = []
    for leg in list(payload["view"]["legs"]) + list(payload["view"]["isolated"]):
        key = leg.get("leg_key")
        if not key or key in seen:
            continue
        seen.add(key)
        selectable.append({
            "leg_key": key,
            "revision": leg.get("revision", 1),
            "isolation_code": leg.get("isolation_code"),
        })
    payload["selectable"] = selectable
    return payload
