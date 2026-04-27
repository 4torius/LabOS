"""Hardware Config API routes — HAL config CRUD + catalog endpoints."""
import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request


# Opentrons Flex pipette models (API names used in protocol.load_instrument())
FLEX_PIPETTES = [
    {"model": "flex_1channel_50",    "display": "Flex 1-Channel 50 µL",   "channels": 1},
    {"model": "flex_1channel_1000",  "display": "Flex 1-Channel 1000 µL", "channels": 1},
    {"model": "flex_8channel_50",    "display": "Flex 8-Channel 50 µL",   "channels": 8},
    {"model": "flex_8channel_1000",  "display": "Flex 8-Channel 1000 µL", "channels": 8},
    {"model": "flex_96channel_1000", "display": "Flex 96-Channel 1000 µL","channels": 96},
    {"model": "flex_96channel_200",  "display": "Flex 96-Channel 200 µL", "channels": 96},
]

FLEX_MODULES = [
    {"type": "heaterShakerModuleV1",  "display": "Heater-Shaker Module V1",   "slots": "any",      "icon": "bi-thermometer-half"},
    {"type": "thermocyclerModuleV2",  "display": "Thermocycler Module V2",     "slots": "A1+B1",    "icon": "bi-grid-1x2"},
    {"type": "magneticBlockV1",       "display": "Magnetic Block V1",          "slots": "any",      "icon": "bi-magnet"},
    {"type": "temperatureModuleV2",   "display": "Temperature Module V2",      "slots": "any",      "icon": "bi-snow2"},
    {"type": "absorbanceReaderV1",    "display": "Absorbance Reader V1",       "slots": "any",      "icon": "bi-eye"},
]

# Valid Flex deck slots
FLEX_SLOTS = ["A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3", "D1", "D2", "D3"]


def create_hardware_router(base_dir: Path, library_dir: Path, lab_core=None) -> APIRouter:
    router = APIRouter(tags=["hardware"])
    HAL_DIR = base_dir / "Library" / "HardwareConfig"
    ACTIVE_FILE = HAL_DIR / ".active"
    PLATES_DIR = library_dir / "Labware" / "Plates"
    TIP_RACKS_DIR = library_dir / "Labware" / "TipRacks"

    def _read_active() -> str:
        """Return active HAL config filename, or empty string if none set."""
        try:
            return ACTIVE_FILE.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    # ── Active config ─────────────────────────────────────────────────────────

    @router.get("/api/hardware/active")
    async def get_active_config():
        filename = _read_active()
        if not filename:
            return {"filename": None, "config": None}
        path = HAL_DIR / filename
        if not path.exists():
            return {"filename": None, "config": None}
        return {"filename": filename, "config": json.loads(path.read_text(encoding="utf-8"))}

    @router.put("/api/hardware/active")
    async def set_active_config(request: Request):
        data = await request.json()
        filename = (data.get("filename") or "").strip()
        HAL_DIR.mkdir(parents=True, exist_ok=True)
        if not filename:
            ACTIVE_FILE.unlink(missing_ok=True)
            return {"status": "cleared"}
        if not (HAL_DIR / filename).exists():
            raise HTTPException(404, f"Config '{filename}' not found")
        ACTIVE_FILE.write_text(filename, encoding="utf-8")

        # Apply immediately to Opentrons if available so runtime HAL and tip state
        # stay aligned with the selected active file in the webapp.
        apply_result = {
            "attempted": False,
            "applied": False,
            "instrument": None,
            "message": "",
        }

        core = lab_core
        if core is None:
            try:
                from src.lab_core import get_lab_core
                core = get_lab_core(base_dir)
            except Exception:
                core = None

        if core is not None:
            apply_result["attempted"] = True
            try:
                await core.discover()
                instruments = core.list_instruments()
                opentrons = next(
                    (
                        i for i in instruments
                        if "opentrons" in i.id.lower() or "opentrons" in i.name.lower()
                    ),
                    None,
                )

                if opentrons is not None:
                    apply_result["instrument"] = opentrons.id
                    # Be tolerant with parameter names expected by XML/adapter.
                    attempts = [
                        {"ConfigName": filename},
                        {"config_name": filename},
                        {"config": filename},
                    ]
                    final_error = ""
                    for params in attempts:
                        result = await core.execute_command(opentrons.id, "LoadHardwareConfig", params)
                        if result.success:
                            apply_result["applied"] = True
                            apply_result["message"] = result.message or "LoadHardwareConfig completed"
                            break
                        final_error = result.error or "LoadHardwareConfig failed"

                    if not apply_result["applied"]:
                        apply_result["message"] = final_error
                else:
                    apply_result["message"] = "Opentrons instrument not found"
            except Exception as exc:
                apply_result["message"] = str(exc)

        return {
            "status": "active",
            "filename": filename,
            "apply": apply_result,
        }

    # ── HAL config CRUD ──────────────────────────────────────────────────────

    @router.get("/api/hardware/configs")
    async def list_hal_configs():
        if not HAL_DIR.exists():
            return {"configs": [], "active": None}
        active = _read_active()
        configs = []
        for f in sorted(HAL_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                configs.append({
                    "filename": f.name,
                    "name": data.get("ConfigName", f.stem),
                    "description": data.get("Description", ""),
                    "pipettes": list(data.get("Pipettes", {}).values()),
                    "labware_count": len(data.get("Labware", {})),
                    "module_count": len(data.get("Modules", {})),
                    "active": f.name == active,
                })
            except Exception:
                configs.append({"filename": f.name, "name": f.stem, "description": "", "pipettes": [], "labware_count": 0, "module_count": 0, "active": f.name == active})
        return {"configs": configs, "active": active}

    @router.get("/api/hardware/configs/{filename}")
    async def get_hal_config(filename: str):
        if not filename.endswith(".json"):
            filename += ".json"
        path = HAL_DIR / filename
        if not path.exists():
            raise HTTPException(404, f"Config '{filename}' not found")
        try:
            path.resolve().relative_to(HAL_DIR.resolve())
        except ValueError:
            raise HTTPException(403, "Access denied")
        return json.loads(path.read_text(encoding="utf-8"))

    @router.post("/api/hardware/configs")
    async def save_hal_config(request: Request):
        data = await request.json()
        name = (data.get("ConfigName") or "").strip()
        if not name:
            raise HTTPException(400, "ConfigName is required")
        # Sanitize filename
        safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in name).replace(" ", "_")
        filename = f"{safe_name}.json"
        HAL_DIR.mkdir(parents=True, exist_ok=True)
        out_path = HAL_DIR / filename
        try:
            out_path.resolve().relative_to(HAL_DIR.resolve())
        except ValueError:
            raise HTTPException(403, "Access denied")
        out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"status": "saved", "filename": filename}

    @router.delete("/api/hardware/configs/{filename}")
    async def delete_hal_config(filename: str):
        if not filename.endswith(".json"):
            filename += ".json"
        path = HAL_DIR / filename
        if not path.exists():
            raise HTTPException(404, f"Config '{filename}' not found")
        try:
            path.resolve().relative_to(HAL_DIR.resolve())
        except ValueError:
            raise HTTPException(403, "Access denied")
        path.unlink()
        return {"status": "deleted", "filename": filename}

    # ── Catalog endpoints ────────────────────────────────────────────────────

    @router.get("/api/hardware/catalog/pipettes")
    async def catalog_pipettes():
        return {"pipettes": FLEX_PIPETTES}

    @router.get("/api/hardware/catalog/modules")
    async def catalog_modules():
        return {"modules": FLEX_MODULES}

    @router.get("/api/hardware/catalog/slots")
    async def catalog_slots():
        return {"slots": FLEX_SLOTS}

    @router.get("/api/hardware/catalog/labware")
    async def catalog_labware():
        """Combined catalog: plates + tip racks + reservoirs with category tags."""
        items = []

        # ── Plates ──────────────────────────────────────────────────────────
        if PLATES_DIR.exists():
            for f in sorted(PLATES_DIR.glob("*.plate.json")):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    items.append({
                        "load_name": d.get("load_name", f.stem.replace(".plate", "")),
                        "display_name": d.get("display_name", d.get("load_name", f.stem)),
                        "category": "plate",
                        "format": d.get("format", ""),
                        "total_wells": d.get("total_wells"),
                        "tecan_plate_name": d.get("tecan_plate_name", ""),
                    })
                except Exception:
                    pass

        # ── Tip racks ────────────────────────────────────────────────────────
        if TIP_RACKS_DIR.exists():
            for entry in sorted(TIP_RACKS_DIR.iterdir()):
                if entry.is_dir():
                    # Try to read display name from the versioned JSON inside
                    display = entry.name.replace("_", " ").title()
                    for vf in sorted(entry.glob("*.json")):
                        try:
                            meta = json.loads(vf.read_text(encoding="utf-8")).get("metadata", {})
                            display = meta.get("displayName", display)
                            break
                        except Exception:
                            pass
                    items.append({
                        "load_name": entry.name,
                        "display_name": display,
                        "category": "tiprack",
                        "format": "",
                        "total_wells": None,
                        "tecan_plate_name": "",
                    })

        # ── Reservoirs ───────────────────────────────────────────────────────
        RESERVOIRS_DIR = library_dir / "Labware" / "Reservoirs"
        if RESERVOIRS_DIR.exists():
            for f in sorted(RESERVOIRS_DIR.glob("*.json")):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    meta = d.get("metadata", {})
                    wells = d.get("wells", {})
                    load_name = f.stem
                    display = meta.get("displayName", load_name.replace("_", " ").title())
                    items.append({
                        "load_name": load_name,
                        "display_name": display,
                        "category": "reservoir",
                        "format": f"{len(wells)}-well",
                        "total_wells": len(wells),
                        "tecan_plate_name": "",
                    })
                except Exception:
                    pass

        return {"labware": items}

    return router
