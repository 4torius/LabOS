"""
Scientific database API routes.

Endpoints for plates, wells, measurements, reagent catalog, protocols, and
AI-ready exports. All reads come from SQLite via src.database; the plate
tracking dict (in-memory) is used only as a fallback source for plate IDs.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response


def create_db_router(db_module) -> APIRouter:
    """
    Args:
        db_module: the src.database module (already configured with a DB path).
    """
    router = APIRouter(prefix="/api/db", tags=["database"])
    _db = db_module

    # ── Plates ────────────────────────────────────────────────────────────────

    @router.get("/plates")
    async def list_plates(run_id: str = "", limit: int = 50, offset: int = 0):
        plates = _db.get_plates(run_id=run_id or None, limit=limit, offset=offset)
        return {"plates": plates, "count": len(plates)}

    @router.get("/plates/{plate_id}")
    async def get_plate(plate_id: str):
        plate = _db.get_plate(plate_id)
        if plate is None:
            raise HTTPException(status_code=404, detail=f"Plate '{plate_id}' not found")
        return plate

    @router.get("/plates/{plate_id}/heatmap")
    async def get_plate_heatmap(plate_id: str, measurement_type: str = ""):
        return _db.get_well_heatmap(plate_id, measurement_type or None)

    @router.put("/plates/{plate_id}/status")
    async def update_plate_status(plate_id: str, request: Request):
        data = await request.json()
        status = data.get("status", "").strip()
        if not status:
            raise HTTPException(status_code=400, detail="status field required")
        _db.update_plate_status(plate_id, status)
        return {"plate_id": plate_id, "status": status}

    # ── Protocols ─────────────────────────────────────────────────────────────

    @router.get("/protocols")
    async def list_protocols():
        return {"protocols": _db.get_protocols()}

    @router.put("/protocols/{name}")
    async def upsert_protocol(name: str, request: Request):
        data = await request.json()
        pid = _db.get_or_create_protocol(name, data)
        return {"name": name, "id": pid}

    # ── Reagent catalog ───────────────────────────────────────────────────────

    @router.get("/reagents")
    async def list_reagents():
        return {"reagents": _db.get_reagents()}

    @router.put("/reagents/{name}")
    async def upsert_reagent(name: str, request: Request):
        """
        Create or update a reagent catalog entry.
        Body (all optional): display_name, cas_number, molecular_formula,
        molar_mass_gmol, supplier, catalog_number, stock_concentration_mm,
        storage_conditions, hazard_codes, solvent, notes.
        """
        data = await request.json()
        rid = _db.get_or_create_reagent(name, data)
        if data:
            _db.update_reagent(name, data)
        return {"name": name, "id": rid}

    # ── Runs ──────────────────────────────────────────────────────────────────

    @router.get("/runs")
    async def list_runs(limit: int = 50, offset: int = 0,
                        status: str = "", workflow_name: str = ""):
        runs = _db.get_runs(
            limit=limit, offset=offset,
            status=status or None,
            workflow_name=workflow_name or None,
        )
        return {"runs": runs, "count": len(runs)}

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str):
        run = _db.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        return run

    # ── AI exports ────────────────────────────────────────────────────────────

    @router.get("/export/plate/{plate_id}.csv")
    async def export_plate_csv(plate_id: str):
        """
        CSV with one row per well: reagent, volume, liquid_class, phase,
        measured value, unit, wavelength, protocol. Ready for AI/analysis.
        """
        csv_str = _db.export_plate_csv(plate_id)
        if csv_str is None:
            raise HTTPException(status_code=404, detail=f"Plate '{plate_id}' not found")
        return Response(
            content=csv_str,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{plate_id}.csv"'},
        )

    @router.get("/export/run/{run_id}.json")
    async def export_run_json(run_id: str):
        """
        Full structured JSON for AI consumption: run metadata, plates, wells
        (with reagent catalog entries), per-well measurements, protocols used.
        """
        data = _db.export_run_json(run_id)
        if data is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        return data

    @router.get("/stats")
    async def get_db_stats():
        stats = _db.get_stats()
        return stats

    return router
