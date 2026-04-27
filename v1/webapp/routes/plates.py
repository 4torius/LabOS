"""Plate tracking API routes."""
import asyncio
from datetime import datetime
from typing import Callable

from fastapi import APIRouter, HTTPException, Request


def create_plates_router(state, plate_tracking: dict, save_plate_tracking: Callable, plate_tracking_lock: asyncio.Lock) -> APIRouter:
    router = APIRouter(prefix="/api/plates", tags=["plates"])

    @router.get("")
    async def list_plates():
        return {
            "plates": [
                {
                    "plate_id": pid,
                    "created": info.get("created"),
                    "source_file": info.get("source_file"),
                    "well_count": info.get("well_count"),
                    "status": info.get("status"),
                    "has_analysis": len(info.get("analysis_results", [])) > 0
                }
                for pid, info in plate_tracking.items()
            ]
        }

    @router.post("")
    async def create_plate(request: Request):
        """Manually create a plate entry."""
        data = await request.json()
        plate_id = data.get("plate_id", "").strip()
        if not plate_id:
            raise HTTPException(status_code=400, detail="plate_id is required")
        if plate_id in plate_tracking:
            raise HTTPException(status_code=409, detail=f"Plate {plate_id} already exists")
        entry = {
            "created": datetime.now().isoformat(),
            "status": "pending",
            "well_count": data.get("well_count"),
            "notes": data.get("notes"),
            "source_file": "manual",
            "analysis_results": []
        }
        async with plate_tracking_lock:
            plate_tracking[plate_id] = entry
            await save_plate_tracking(plate_tracking)
        state.add_log("info", f"Plate {plate_id} created manually", "plates")
        return {"status": "created", "plate_id": plate_id}

    @router.get("/{plate_id}")
    async def get_plate_details(plate_id: str):
        if plate_id not in plate_tracking:
            raise HTTPException(status_code=404, detail=f"Plate {plate_id} not found")
        return {"plate_id": plate_id, **plate_tracking[plate_id]}

    @router.post("/{plate_id}/analysis")
    async def link_analysis_to_plate(plate_id: str, request: Request):
        """Link analysis results to a plate for traceability."""
        if plate_id not in plate_tracking:
            plate_tracking[plate_id] = {
                "created": datetime.now().isoformat(),
                "status": "completed",
                "analysis_results": []
            }

        data = await request.json()
        analysis_entry = {
            "timestamp": datetime.now().isoformat(),
            "result_file": data.get("result_file"),
            "measurement_type": data.get("measurement_type"),
            "protocol": data.get("protocol"),
            "notes": data.get("notes"),
            "data": data.get("data")
        }

        async with plate_tracking_lock:
            plate_tracking[plate_id]["analysis_results"].append(analysis_entry)
            plate_tracking[plate_id]["status"] = "analyzed"
            await save_plate_tracking(plate_tracking)

        state.add_log("info", f"Analysis linked to plate {plate_id}", "plates")
        return {
            "status": "success",
            "plate_id": plate_id,
            "analysis_count": len(plate_tracking[plate_id]["analysis_results"])
        }

    @router.put("/{plate_id}/status")
    async def update_plate_status(plate_id: str, request: Request):
        if plate_id not in plate_tracking:
            raise HTTPException(status_code=404, detail=f"Plate {plate_id} not found")
        data = await request.json()
        new_status = data.get("status")
        if new_status:
            async with plate_tracking_lock:
                plate_tracking[plate_id]["status"] = new_status
                await save_plate_tracking(plate_tracking)
            state.add_log("info", f"Plate {plate_id} status updated to: {new_status}", "plates")
        return {"status": "updated", "plate_id": plate_id, "new_status": new_status}

    @router.delete("/{plate_id}")
    async def delete_plate(plate_id: str):
        if plate_id not in plate_tracking:
            raise HTTPException(status_code=404, detail=f"Plate {plate_id} not found")
        async with plate_tracking_lock:
            del plate_tracking[plate_id]
            await save_plate_tracking(plate_tracking)
        state.add_log("info", f"Plate {plate_id} removed from tracking", "plates")
        return {"status": "deleted", "plate_id": plate_id}

    @router.get("/{plate_id}/traceability")
    async def get_plate_traceability(plate_id: str):
        """Get full traceability report: links pipetting operations with analysis results."""
        if plate_id not in plate_tracking:
            raise HTTPException(status_code=404, detail=f"Plate {plate_id} not found")
        plate_info = plate_tracking[plate_id]
        return {
            "plate_id": plate_id,
            "traceability": {
                "pipetting": {
                    "timestamp": plate_info.get("created"),
                    "source_file": plate_info.get("source_file"),
                    "well_count": plate_info.get("well_count"),
                    "status": plate_info.get("status")
                },
                "analysis": plate_info.get("analysis_results", []),
                "timeline": [
                    {"event": "created", "timestamp": plate_info.get("created")},
                    *[{"event": f"analysis_{i+1}", "timestamp": a.get("timestamp")}
                      for i, a in enumerate(plate_info.get("analysis_results", []))]
                ]
            }
        }

    return router
