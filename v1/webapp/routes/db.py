"""
Scientific database API routes.

Endpoints for plates, wells, measurements, reagent catalog, protocols, and
AI-ready exports. All reads come from SQLite via src.database; the plate
tracking dict (in-memory) is used only as a fallback source for plate IDs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response


def create_db_router(db_module, results_dir: Optional[Path] = None, state=None) -> APIRouter:
    """
    Args:
        db_module: the src.database module (already configured with a DB path).
        results_dir: Results/ directory (Results/AnIML holds the .animl files
            attach-measurement below can link to an existing plate). Optional
            so existing callers/tests that don't need that endpoint keep working.
        state: app state, for state.add_log() — same optional-logging pattern
            used elsewhere in this app.
    """
    router = APIRouter(prefix="/api/db", tags=["database"])
    _db = db_module

    # ── Plates ────────────────────────────────────────────────────────────────

    @router.get("/plates")
    async def list_plates(
        run_id: str = "",
        limit: int = 50,
        offset: int = 0,
        search: str = "",
        date_from: str = "",
        date_to: str = "",
    ):
        if search or date_from or date_to:
            plates = _db.search_plates(
                query=search, date_from=date_from, date_to=date_to,
                limit=limit, offset=offset,
            )
        else:
            plates = _db.get_plates(run_id=run_id or None, limit=limit, offset=offset)
        return {"plates": plates, "count": len(plates)}

    @router.get("/plates/{plate_id}")
    async def get_plate(plate_id: str):
        plate = _db.get_plate(plate_id)
        if plate is None:
            raise HTTPException(status_code=404, detail=f"Plate '{plate_id}' not found")
        return plate

    @router.get("/plates/{plate_id}/heatmap")
    async def get_plate_heatmap(plate_id: str, measurement_type: str = "", measurement_id: Optional[int] = None):
        return _db.get_well_heatmap(plate_id, measurement_type or None, measurement_id)

    @router.put("/plates/{plate_id}/status")
    async def update_plate_status(plate_id: str, request: Request):
        data = await request.json()
        status = data.get("status", "").strip()
        if not status:
            raise HTTPException(status_code=400, detail="status field required")
        _db.update_plate_status(plate_id, status)
        return {"plate_id": plate_id, "status": status}

    @router.patch("/plates/{plate_id}")
    async def patch_plate(plate_id: str, request: Request):
        data = await request.json()
        ok = _db.update_plate(plate_id, data.get("display_name"), data.get("notes"))
        if not ok:
            raise HTTPException(status_code=404, detail=f"Plate '{plate_id}' not found")
        return {"plate_id": plate_id, "updated": True}

    @router.delete("/plates/{plate_id}")
    async def delete_plate(plate_id: str):
        ok = _db.delete_plate(plate_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Plate '{plate_id}' not found")
        return {"plate_id": plate_id, "deleted": True}

    @router.post("/plates/{plate_id}/attach-measurement")
    async def attach_measurement(plate_id: str, request: Request):
        """Link an AnIML file that already exists in Results/AnIML (e.g. a
        measurement run outside this webapp, or an earlier run never tied to
        this plate) instead of triggering a new live 'Run measurement' -
        the "Attach existing result" path in the plate's "Add an analysis"
        dialog."""
        if results_dir is None:
            raise HTTPException(status_code=501, detail="results_dir not configured")
        data = await request.json()
        animl_filename = (data.get("animl_filename") or "").strip()
        if not animl_filename:
            raise HTTPException(status_code=400, detail="animl_filename field required")
        protocol_name = (data.get("protocol_name") or "").strip()

        from src.plate_tracking import attach_existing_measurement
        try:
            attach_existing_measurement(
                plate_id, animl_filename, results_dir,
                protocol_name=protocol_name, state=state,
            )
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"plate_id": plate_id, "attached": animl_filename}

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
    async def list_runs(
        limit: int = 50,
        offset: int = 0,
        status: str = "",
        workflow_name: str = "",
        search: str = "",
        date_from: str = "",
        date_to: str = "",
    ):
        if search or date_from or date_to:
            runs = _db.search_runs(
                query=search,
                date_from=date_from,
                date_to=date_to,
                status=status or None,
                limit=limit,
                offset=offset,
            )
        else:
            runs = _db.get_runs(
                limit=limit,
                offset=offset,
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

    @router.patch("/runs/{run_id}")
    async def patch_run(run_id: str, request: Request):
        data = await request.json()
        ok = _db.rename_run(run_id, data.get("workflow_name"), data.get("notes"))
        if not ok:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        return {"run_id": run_id, "updated": True}

    @router.delete("/runs/{run_id}")
    async def delete_run(run_id: str):
        ok = _db.delete_run(run_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        return {"run_id": run_id, "deleted": True}

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
        Automation-record JSON: run metadata, per-instrument measurement log,
        and which plate(s) were touched by reference only (no wells/reagents/
        measurements — that's export_plate_json's job, see below).
        """
        data = _db.export_run_json(run_id)
        if data is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        return Response(
            content=json.dumps(data, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'},
        )

    @router.get("/export/plate/{plate_id}.json")
    async def export_plate_json(plate_id: str):
        """
        Complete chemical-record JSON for a plate, AI-ready: wells, reagent
        catalog entries, well_measurements, and every linked raw AnIML result
        with its measurement data already parsed inline (animl_data).
        """
        data = _db.export_plate_json(plate_id)
        if data is None:
            raise HTTPException(status_code=404, detail=f"Plate '{plate_id}' not found")
        return Response(
            content=json.dumps(data, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{plate_id}.json"'},
        )

    @router.get("/export/ai-context.json")
    async def export_ai_context(status: str = "completed", run_ids: str = ""):
        """
        Full context JSON for AI/LLM recipe generation.
        - status=completed (default) exports all completed runs
        - run_ids=id1,id2,... exports only those specific runs (overrides status)
        Includes step timing, operator wait times, errors per step.
        """
        ids = [r.strip() for r in run_ids.split(",") if r.strip()] if run_ids else None
        data = _db.export_ai_context(status_filter=status if not ids else None, run_ids=ids)
        if not data:
            raise HTTPException(status_code=500, detail="Export failed")
        fname = f"ai_context_{'_'.join(ids[:3])}.json" if ids else "ai_context_all.json"
        return Response(
            content=json.dumps(data, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @router.get("/stats")
    async def get_db_stats():
        stats = _db.get_stats()
        return stats

    return router
