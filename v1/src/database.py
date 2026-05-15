"""SQLite persistence layer for LabOS — workflow runs, plates, wells, measurements."""

from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DB_PATH: Optional[Path] = None


def configure(db_path: Path) -> None:
    """Set the database file path. Call once at startup before any other function."""
    global _DB_PATH
    _DB_PATH = db_path


def _get_path() -> Path:
    if _DB_PATH is None:
        raise RuntimeError("database not configured — call configure() first")
    return _DB_PATH


@contextmanager
def _conn():
    path = _get_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
#  Schema
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMA = """
-- ── Workflow run summary ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS run_results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT UNIQUE NOT NULL,
    workflow_name     TEXT,
    started_at        TEXT,
    completed_at      TEXT,
    status            TEXT,
    duration_seconds  REAL,
    steps_total       INTEGER,
    steps_completed   INTEGER,
    steps_failed      INTEGER,
    steps_skipped     INTEGER,
    result_json       TEXT,
    created_at        TEXT DEFAULT (datetime('now'))
);

-- ── Measurement protocols (Tecan .mdfx parameters) ───────────────────────────
CREATE TABLE IF NOT EXISTS protocols (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT UNIQUE NOT NULL,
    measurement_type  TEXT,           -- Absorbance / Fluorescence / Luminescence / Mixed / Unknown
    wavelength_nm     REAL,           -- absorbance wavelength or excitation for fluorescence
    excitation_nm     REAL,
    emission_nm       REAL,
    integration_ms    REAL,
    num_reads         INTEGER,
    bandwidth_nm      REAL,
    parameters_json   TEXT,           -- raw parameters dict from AnIML parse
    created_at        TEXT DEFAULT (datetime('now'))
);

-- ── Tecan/instrument command records (one row per SiLA2 command invocation) ──
CREATE TABLE IF NOT EXISTS instrument_measurements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT,
    instrument      TEXT,
    command         TEXT,
    executed_at     TEXT,
    animl_path      TEXT,
    protocol_name   TEXT,
    protocol_id     INTEGER,
    measurement_type TEXT,
    result_json     TEXT,
    FOREIGN KEY(run_id)      REFERENCES run_results(run_id),
    FOREIGN KEY(protocol_id) REFERENCES protocols(id)
);

-- ── Plates prepared by Opentrons ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS plates (
    plate_id          TEXT PRIMARY KEY,
    run_id            TEXT,
    plate_type        TEXT,           -- Opentrons load name (e.g. corning_96_wellplate_360ul_flat)
    display_name      TEXT,           -- human-readable plate name
    rows              INTEGER,
    columns           INTEGER,
    total_wells       INTEGER,
    max_volume_ul     REAL,
    working_volume_ul REAL,
    well_shape        TEXT,           -- circular / square / rectangular
    well_depth_mm     REAL,
    bottom_type       TEXT,           -- flat / round / v-bottom
    brand             TEXT,
    tecan_compatible  INTEGER DEFAULT 1,
    labware_slot      TEXT,
    hal_config        TEXT,
    recipe_name       TEXT,
    prepared_at       TEXT,
    status            TEXT DEFAULT 'prepared',  -- prepared / measured / archived
    notes             TEXT,
    FOREIGN KEY(run_id) REFERENCES run_results(run_id)
);

-- ── Reagent catalog (populated incrementally from recipe extraction) ──────────
CREATE TABLE IF NOT EXISTS reagent_catalog (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT UNIQUE NOT NULL,  -- logical labware name or reagent name
    display_name          TEXT,
    cas_number            TEXT,
    molecular_formula     TEXT,
    molar_mass_gmol       REAL,
    supplier              TEXT,
    catalog_number        TEXT,
    stock_concentration_mm REAL,
    storage_conditions    TEXT,
    hazard_codes          TEXT,
    solvent               TEXT,
    notes                 TEXT,
    created_at            TEXT DEFAULT (datetime('now')),
    updated_at            TEXT DEFAULT (datetime('now'))
);

-- ── Wells: what was pipetted where ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wells (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_id          TEXT,
    well_id           TEXT,           -- "A1", "B12"
    row_label         TEXT,           -- "A"
    col_number        INTEGER,        -- 1, 2, ...
    reagent_name      TEXT,           -- logical labware name from recipe
    reagent_catalog_id INTEGER,       -- FK to reagent_catalog (if registered)
    volume_ul         REAL,
    concentration_mm  REAL,           -- if known from catalog or recipe metadata
    liquid_class      TEXT,           -- Aqueous / Viscous / HighlyViscous / etc.
    phase_name        TEXT,           -- recipe generator group/phase name
    source_well       TEXT,           -- "MyReservoir:A1"
    pipette_mount     TEXT,           -- left / right
    pipetted_at       TEXT,
    FOREIGN KEY(plate_id)          REFERENCES plates(plate_id),
    FOREIGN KEY(reagent_catalog_id) REFERENCES reagent_catalog(id)
);

-- ── Per-well measurement values ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS well_measurements (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_id         TEXT,
    well_id          TEXT,
    measurement_id   INTEGER,         -- FK to instrument_measurements.id
    protocol_id      INTEGER,         -- FK to protocols.id (denormalized for fast joins)
    value            REAL,
    unit             TEXT,
    measurement_type TEXT,            -- Absorbance / Fluorescence / Luminescence
    wavelength_nm    REAL,
    excitation_nm    REAL,
    emission_nm      REAL,
    cycle            INTEGER DEFAULT 1,
    measured_at      TEXT,
    FOREIGN KEY(plate_id)      REFERENCES plates(plate_id),
    FOREIGN KEY(measurement_id) REFERENCES instrument_measurements(id),
    FOREIGN KEY(protocol_id)   REFERENCES protocols(id)
);

-- ── Indexes ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_run_results_workflow   ON run_results(workflow_name);
CREATE INDEX IF NOT EXISTS idx_run_results_status     ON run_results(status);
CREATE INDEX IF NOT EXISTS idx_measurements_run       ON instrument_measurements(run_id);
CREATE INDEX IF NOT EXISTS idx_plates_run             ON plates(run_id);
CREATE INDEX IF NOT EXISTS idx_plates_recipe          ON plates(recipe_name);
CREATE INDEX IF NOT EXISTS idx_wells_plate            ON wells(plate_id);
CREATE INDEX IF NOT EXISTS idx_wells_reagent          ON wells(reagent_name);
CREATE INDEX IF NOT EXISTS idx_well_meas_plate        ON well_measurements(plate_id);
CREATE INDEX IF NOT EXISTS idx_well_meas_protocol     ON well_measurements(protocol_id);
CREATE INDEX IF NOT EXISTS idx_reagent_name           ON reagent_catalog(name);
"""

# Migration: add new columns to existing tables (idempotent ALTER TABLE)
_MIGRATIONS = [
    "ALTER TABLE instrument_measurements ADD COLUMN protocol_name TEXT",
    "ALTER TABLE instrument_measurements ADD COLUMN protocol_id INTEGER",
    "ALTER TABLE instrument_measurements ADD COLUMN measurement_type TEXT",
    "ALTER TABLE plates ADD COLUMN display_name TEXT",
    "ALTER TABLE plates ADD COLUMN working_volume_ul REAL",
    "ALTER TABLE plates ADD COLUMN well_shape TEXT",
    "ALTER TABLE plates ADD COLUMN well_depth_mm REAL",
    "ALTER TABLE plates ADD COLUMN bottom_type TEXT",
    "ALTER TABLE plates ADD COLUMN brand TEXT",
    "ALTER TABLE plates ADD COLUMN tecan_compatible INTEGER DEFAULT 1",
    "ALTER TABLE plates ADD COLUMN notes TEXT",
    "ALTER TABLE wells ADD COLUMN reagent_catalog_id INTEGER",
    "ALTER TABLE wells ADD COLUMN concentration_mm REAL",
    "ALTER TABLE wells ADD COLUMN liquid_class TEXT",
    "ALTER TABLE wells ADD COLUMN phase_name TEXT",
    "ALTER TABLE wells ADD COLUMN pipette_mount TEXT",
    "ALTER TABLE well_measurements ADD COLUMN protocol_id INTEGER",
    "ALTER TABLE well_measurements ADD COLUMN measurement_type TEXT",
    "ALTER TABLE well_measurements ADD COLUMN excitation_nm REAL",
    "ALTER TABLE well_measurements ADD COLUMN emission_nm REAL",
]


def init_db() -> None:
    """Create tables and run migrations if they don't exist yet."""
    try:
        with _conn() as con:
            # Migrations run first: adds missing columns to old DBs before
            # executescript tries to build indexes on those columns.
            # Silently ignored on fresh installs (tables don't exist yet).
            for stmt in _MIGRATIONS:
                try:
                    con.execute(stmt)
                except sqlite3.OperationalError:
                    pass
            con.executescript(_SCHEMA)
        logger.info(f"Database initialised at {_get_path()}")
    except Exception as e:
        logger.error(f"Failed to initialise database: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  Run results
# ─────────────────────────────────────────────────────────────────────────────

def save_run(run_data: Dict[str, Any]) -> None:
    """Insert or replace a workflow run record."""
    try:
        steps = run_data.get("step_results", [])
        with _conn() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO run_results
                    (run_id, workflow_name, started_at, completed_at, status,
                     duration_seconds, steps_total, steps_completed, steps_failed,
                     steps_skipped, result_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_data.get("run_id"),
                    run_data.get("workflow_name"),
                    run_data.get("started_at"),
                    run_data.get("completed_at") or run_data.get("started_at"),
                    run_data.get("status"),
                    run_data.get("duration_seconds"),
                    len(steps),
                    run_data.get("steps_completed", 0),
                    run_data.get("steps_failed", 0),
                    run_data.get("steps_skipped", 0),
                    json.dumps(run_data, default=str),
                ),
            )
            run_id = run_data.get("run_id")
            for step in steps:
                instrument = step.get("instrument", "")
                command = step.get("command", step.get("action", ""))
                if "Tecan" in instrument or command in ("RunMeasurement", "PlateIn", "PlateOut"):
                    result = step.get("result") or {}
                    if isinstance(result, dict):
                        data = result.get("data") or {}
                        if not isinstance(data, dict):
                            data = {}
                        animl_path = (
                            result.get("animl_path")
                            or result.get("animl_file_path")
                            or result.get("AnIMLFilePath")
                            or data.get("animl_file_path")
                            or data.get("AnIMLFilePath")
                        )
                        protocol_name = (
                            result.get("protocol_name")
                            or data.get("protocol_name")
                        )
                        meas_type = (
                            result.get("measurement_type")
                            or data.get("measurement_type")
                        )
                    else:
                        animl_path = None
                        protocol_name = None
                        meas_type = None
                    con.execute(
                        """
                        INSERT INTO instrument_measurements
                            (run_id, instrument, command, executed_at,
                             animl_path, protocol_name, measurement_type, result_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            instrument,
                            command,
                            step.get("timestamp") or run_data.get("started_at"),
                            animl_path,
                            protocol_name,
                            meas_type,
                            json.dumps(step, default=str),
                        ),
                    )
    except Exception as e:
        logger.error(f"Failed to save run to database: {e}")


def get_runs(
    limit: int = 100,
    offset: int = 0,
    status: Optional[str] = None,
    workflow_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return run summaries (no full JSON), most recent first."""
    try:
        filters: list = []
        params: list = []
        if status:
            filters.append("status = ?")
            params.append(status)
        if workflow_name:
            filters.append("workflow_name = ?")
            params.append(workflow_name)
        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        params += [limit, offset]
        with _conn() as con:
            rows = con.execute(
                f"""
                SELECT run_id, workflow_name, started_at, completed_at, status,
                       duration_seconds, steps_total, steps_completed, steps_failed,
                       steps_skipped, created_at
                FROM run_results
                {where}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Failed to query runs: {e}")
        return []


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Return full run record including result_json and linked measurements."""
    try:
        with _conn() as con:
            row = con.execute(
                "SELECT * FROM run_results WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            data = dict(row)
            if data.get("result_json"):
                data["result"] = json.loads(data["result_json"])
            measurements = con.execute(
                "SELECT * FROM instrument_measurements WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
            data["measurements"] = [dict(m) for m in measurements]
            plates = con.execute(
                "SELECT plate_id, plate_type, display_name, recipe_name, status FROM plates WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            data["plates"] = [dict(p) for p in plates]
            return data
    except Exception as e:
        logger.error(f"Failed to get run {run_id}: {e}")
        return None


def get_stats() -> Dict[str, Any]:
    """Return aggregate statistics for the dashboard."""
    try:
        with _conn() as con:
            row = con.execute(
                """
                SELECT
                    COUNT(*) AS total_runs,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN status = 'failed'    THEN 1 ELSE 0 END) AS failed,
                    AVG(duration_seconds) AS avg_duration_seconds,
                    MAX(started_at) AS last_run_at
                FROM run_results
                """
            ).fetchone()
            return dict(row) if row else {}
    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
#  Protocols
# ─────────────────────────────────────────────────────────────────────────────

def get_or_create_protocol(name: str, protocol_data: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """Return protocol id for name, inserting if not yet present."""
    if not name:
        return None
    try:
        with _conn() as con:
            row = con.execute("SELECT id FROM protocols WHERE name = ?", (name,)).fetchone()
            if row:
                if protocol_data:
                    # Update fields if caller provides richer data
                    con.execute(
                        """UPDATE protocols SET
                            measurement_type = COALESCE(?, measurement_type),
                            wavelength_nm    = COALESCE(?, wavelength_nm),
                            excitation_nm    = COALESCE(?, excitation_nm),
                            emission_nm      = COALESCE(?, emission_nm),
                            parameters_json  = COALESCE(?, parameters_json)
                        WHERE name = ?""",
                        (
                            protocol_data.get("measurement_type"),
                            protocol_data.get("wavelength_nm"),
                            protocol_data.get("excitation_nm"),
                            protocol_data.get("emission_nm"),
                            json.dumps(protocol_data.get("parameters"), default=str)
                            if protocol_data.get("parameters") else None,
                            name,
                        ),
                    )
                return row["id"]
            d = protocol_data or {}
            cur = con.execute(
                """INSERT INTO protocols
                    (name, measurement_type, wavelength_nm, excitation_nm, emission_nm,
                     integration_ms, num_reads, bandwidth_nm, parameters_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    d.get("measurement_type"),
                    d.get("wavelength_nm"),
                    d.get("excitation_nm"),
                    d.get("emission_nm"),
                    d.get("integration_ms"),
                    d.get("num_reads"),
                    d.get("bandwidth_nm"),
                    json.dumps(d.get("parameters"), default=str) if d.get("parameters") else None,
                ),
            )
            return cur.lastrowid
    except Exception as e:
        logger.error(f"Failed to get/create protocol '{name}': {e}")
        return None


def get_protocols() -> List[Dict[str, Any]]:
    try:
        with _conn() as con:
            rows = con.execute("SELECT * FROM protocols ORDER BY name").fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Failed to query protocols: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
#  Reagent catalog
# ─────────────────────────────────────────────────────────────────────────────

def get_or_create_reagent(name: str, reagent_data: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """Return reagent_catalog id for name, inserting if not yet present."""
    if not name:
        return None
    try:
        with _conn() as con:
            row = con.execute("SELECT id FROM reagent_catalog WHERE name = ?", (name,)).fetchone()
            if row:
                return row["id"]
            d = reagent_data or {}
            cur = con.execute(
                """INSERT INTO reagent_catalog
                    (name, display_name, cas_number, molecular_formula, molar_mass_gmol,
                     supplier, catalog_number, stock_concentration_mm,
                     storage_conditions, hazard_codes, solvent, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    d.get("display_name", name),
                    d.get("cas_number"),
                    d.get("molecular_formula"),
                    d.get("molar_mass_gmol"),
                    d.get("supplier"),
                    d.get("catalog_number"),
                    d.get("stock_concentration_mm"),
                    d.get("storage_conditions"),
                    d.get("hazard_codes"),
                    d.get("solvent"),
                    d.get("notes"),
                ),
            )
            return cur.lastrowid
    except Exception as e:
        logger.error(f"Failed to get/create reagent '{name}': {e}")
        return None


def update_reagent(name: str, reagent_data: Dict[str, Any]) -> None:
    """Update an existing reagent catalog entry."""
    try:
        with _conn() as con:
            con.execute(
                """UPDATE reagent_catalog SET
                    display_name           = COALESCE(?, display_name),
                    cas_number             = COALESCE(?, cas_number),
                    molecular_formula      = COALESCE(?, molecular_formula),
                    molar_mass_gmol        = COALESCE(?, molar_mass_gmol),
                    supplier               = COALESCE(?, supplier),
                    catalog_number         = COALESCE(?, catalog_number),
                    stock_concentration_mm = COALESCE(?, stock_concentration_mm),
                    storage_conditions     = COALESCE(?, storage_conditions),
                    hazard_codes           = COALESCE(?, hazard_codes),
                    solvent                = COALESCE(?, solvent),
                    notes                  = COALESCE(?, notes),
                    updated_at             = datetime('now')
                WHERE name = ?""",
                (
                    reagent_data.get("display_name"),
                    reagent_data.get("cas_number"),
                    reagent_data.get("molecular_formula"),
                    reagent_data.get("molar_mass_gmol"),
                    reagent_data.get("supplier"),
                    reagent_data.get("catalog_number"),
                    reagent_data.get("stock_concentration_mm"),
                    reagent_data.get("storage_conditions"),
                    reagent_data.get("hazard_codes"),
                    reagent_data.get("solvent"),
                    reagent_data.get("notes"),
                    name,
                ),
            )
    except Exception as e:
        logger.error(f"Failed to update reagent '{name}': {e}")


def get_reagents() -> List[Dict[str, Any]]:
    try:
        with _conn() as con:
            rows = con.execute("SELECT * FROM reagent_catalog ORDER BY name").fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Failed to query reagents: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
#  Plates
# ─────────────────────────────────────────────────────────────────────────────

def save_plate(plate_data: Dict[str, Any]) -> None:
    """Insert or replace a plate record."""
    try:
        with _conn() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO plates
                    (plate_id, run_id, plate_type, display_name, rows, columns,
                     total_wells, max_volume_ul, working_volume_ul, well_shape,
                     well_depth_mm, bottom_type, brand, tecan_compatible,
                     labware_slot, hal_config, recipe_name, prepared_at, status, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plate_data.get("plate_id"),
                    plate_data.get("run_id"),
                    plate_data.get("plate_type"),
                    plate_data.get("display_name"),
                    plate_data.get("rows"),
                    plate_data.get("columns"),
                    plate_data.get("total_wells"),
                    plate_data.get("max_volume_ul"),
                    plate_data.get("working_volume_ul"),
                    plate_data.get("well_shape"),
                    plate_data.get("well_depth_mm"),
                    plate_data.get("bottom_type"),
                    plate_data.get("brand"),
                    int(plate_data.get("tecan_compatible", True)),
                    plate_data.get("labware_slot"),
                    plate_data.get("hal_config"),
                    plate_data.get("recipe_name"),
                    plate_data.get("prepared_at"),
                    plate_data.get("status", "prepared"),
                    plate_data.get("notes"),
                ),
            )
    except Exception as e:
        logger.error(f"Failed to save plate: {e}")


def update_plate_status(plate_id: str, status: str) -> None:
    try:
        with _conn() as con:
            con.execute("UPDATE plates SET status = ? WHERE plate_id = ?", (status, plate_id))
    except Exception as e:
        logger.error(f"Failed to update plate status: {e}")


def get_plates(
    run_id: Optional[str] = None, limit: int = 50, offset: int = 0
) -> List[Dict[str, Any]]:
    try:
        where = "WHERE run_id = ?" if run_id else ""
        params: list = ([run_id] if run_id else []) + [limit, offset]
        with _conn() as con:
            rows = con.execute(
                f"SELECT * FROM plates {where} ORDER BY prepared_at DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Failed to query plates: {e}")
        return []


def get_plate(plate_id: str) -> Optional[Dict[str, Any]]:
    """Return plate + wells + well_measurements, or None."""
    try:
        with _conn() as con:
            row = con.execute("SELECT * FROM plates WHERE plate_id = ?", (plate_id,)).fetchone()
            if row is None:
                return None
            data = dict(row)
            data["wells"] = [
                dict(w)
                for w in con.execute(
                    """SELECT w.*, r.cas_number, r.molecular_formula, r.molar_mass_gmol,
                              r.display_name AS reagent_display_name
                       FROM wells w
                       LEFT JOIN reagent_catalog r ON r.id = w.reagent_catalog_id
                       WHERE w.plate_id = ?
                       ORDER BY w.col_number, w.row_label""",
                    (plate_id,),
                ).fetchall()
            ]
            data["measurements"] = [
                dict(m)
                for m in con.execute(
                    """SELECT wm.*, p.name AS protocol_name_ref,
                              p.measurement_type AS protocol_measurement_type
                       FROM well_measurements wm
                       LEFT JOIN protocols p ON p.id = wm.protocol_id
                       WHERE wm.plate_id = ?
                       ORDER BY wm.well_id, wm.cycle""",
                    (plate_id,),
                ).fetchall()
            ]
            return data
    except Exception as e:
        logger.error(f"Failed to get plate {plate_id}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Wells
# ─────────────────────────────────────────────────────────────────────────────

def save_wells(wells: List[Dict[str, Any]]) -> None:
    """Bulk insert well records (skips on conflict)."""
    if not wells:
        return
    try:
        with _conn() as con:
            # Ensure reagents are registered in catalog
            reagent_id_cache: Dict[str, Optional[int]] = {}
            for w in wells:
                rname = w.get("reagent_name") or ""
                if rname and rname not in reagent_id_cache:
                    reagent_id_cache[rname] = _get_or_create_reagent_in_conn(con, rname)

            con.executemany(
                """
                INSERT OR IGNORE INTO wells
                    (plate_id, well_id, row_label, col_number, reagent_name,
                     reagent_catalog_id, volume_ul, concentration_mm,
                     liquid_class, phase_name, source_well, pipette_mount, pipetted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        w.get("plate_id"),
                        w.get("well_id"),
                        w.get("row_label"),
                        w.get("col_number"),
                        w.get("reagent_name"),
                        reagent_id_cache.get(w.get("reagent_name") or ""),
                        w.get("volume_ul"),
                        w.get("concentration_mm"),
                        w.get("liquid_class"),
                        w.get("phase_name"),
                        w.get("source_well"),
                        w.get("pipette_mount"),
                        w.get("pipetted_at"),
                    )
                    for w in wells
                ],
            )
    except Exception as e:
        logger.error(f"Failed to save wells: {e}")


def _get_or_create_reagent_in_conn(con: sqlite3.Connection, name: str) -> Optional[int]:
    row = con.execute("SELECT id FROM reagent_catalog WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = con.execute(
        "INSERT INTO reagent_catalog (name, display_name) VALUES (?, ?)", (name, name)
    )
    return cur.lastrowid


# ─────────────────────────────────────────────────────────────────────────────
#  Well measurements
# ─────────────────────────────────────────────────────────────────────────────

def save_well_measurements(
    plate_id: str,
    measurement_id: Optional[int],
    well_values: List[Dict[str, Any]],
    protocol_id: Optional[int] = None,
) -> None:
    """Insert per-well measurement values from an AnIML parse result."""
    if not well_values:
        return
    try:
        with _conn() as con:
            con.executemany(
                """
                INSERT INTO well_measurements
                    (plate_id, well_id, measurement_id, protocol_id,
                     value, unit, measurement_type,
                     wavelength_nm, excitation_nm, emission_nm,
                     cycle, measured_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        plate_id,
                        wv.get("well"),
                        measurement_id,
                        protocol_id,
                        wv.get("value"),
                        wv.get("unit"),
                        wv.get("measurement_type"),
                        wv.get("wavelength_nm"),
                        wv.get("excitation_nm"),
                        wv.get("emission_nm"),
                        wv.get("cycle", 1),
                        wv.get("timestamp"),
                    )
                    for wv in well_values
                ],
            )
    except Exception as e:
        logger.error(f"Failed to save well measurements: {e}")


def get_last_measurement_id(run_id: str) -> Optional[int]:
    """Return the id of the most recently inserted instrument_measurement for a run."""
    try:
        with _conn() as con:
            row = con.execute(
                "SELECT id FROM instrument_measurements WHERE run_id = ? ORDER BY id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            return row["id"] if row else None
    except Exception as e:
        logger.error(f"Failed to get last measurement id: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Exports for AI consumption
# ─────────────────────────────────────────────────────────────────────────────

def export_plate_csv(plate_id: str) -> Optional[str]:
    """
    Return a CSV string with one row per well, joining pipetting and measurement data.
    Columns: plate_id, well_id, row, col, reagent, volume_ul, concentration_mm,
             liquid_class, phase, measured_value, unit, measurement_type,
             wavelength_nm, excitation_nm, emission_nm, protocol, measured_at
    Returns None if plate not found.
    """
    try:
        with _conn() as con:
            plate = con.execute("SELECT * FROM plates WHERE plate_id = ?", (plate_id,)).fetchone()
            if plate is None:
                return None

            rows = con.execute(
                """
                SELECT
                    w.plate_id,
                    w.well_id,
                    w.row_label,
                    w.col_number,
                    w.reagent_name,
                    COALESCE(rc.display_name, w.reagent_name)  AS reagent_display,
                    rc.cas_number,
                    rc.molecular_formula,
                    w.volume_ul,
                    w.concentration_mm,
                    w.liquid_class,
                    w.phase_name,
                    wm.value                                    AS measured_value,
                    wm.unit,
                    wm.measurement_type,
                    wm.wavelength_nm,
                    wm.excitation_nm,
                    wm.emission_nm,
                    wm.cycle,
                    COALESCE(p.name, im.protocol_name)         AS protocol,
                    wm.measured_at
                FROM wells w
                LEFT JOIN well_measurements wm  ON wm.plate_id = w.plate_id
                                               AND wm.well_id  = w.well_id
                LEFT JOIN protocols p           ON p.id = wm.protocol_id
                LEFT JOIN instrument_measurements im ON im.id = wm.measurement_id
                LEFT JOIN reagent_catalog rc    ON rc.id = w.reagent_catalog_id
                WHERE w.plate_id = ?
                ORDER BY w.col_number, w.row_label, wm.cycle
                """,
                (plate_id,),
            ).fetchall()

            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow([
                "plate_id", "well_id", "row", "col",
                "reagent_name", "reagent_display", "cas_number", "molecular_formula",
                "volume_ul", "concentration_mm", "liquid_class", "phase",
                "measured_value", "unit", "measurement_type",
                "wavelength_nm", "excitation_nm", "emission_nm", "cycle",
                "protocol", "measured_at",
            ])
            for r in rows:
                writer.writerow(list(r))
            return output.getvalue()
    except Exception as e:
        logger.error(f"Failed to export plate CSV {plate_id}: {e}")
        return None


def export_run_json(run_id: str) -> Optional[Dict[str, Any]]:
    """
    Return a structured dict with complete run data suitable for AI consumption.
    Includes: run metadata, plates, per-well pipetting + measurement data,
    reagent catalog entries, protocols used.
    """
    try:
        run = get_run(run_id)
        if run is None:
            return None
        plates_data = []
        with _conn() as con:
            plate_rows = con.execute(
                "SELECT * FROM plates WHERE run_id = ?", (run_id,)
            ).fetchall()
            for plate in plate_rows:
                p = dict(plate)
                p["wells"] = [
                    dict(w) for w in con.execute(
                        """SELECT w.*, r.cas_number, r.molecular_formula,
                                  r.molar_mass_gmol, r.display_name AS reagent_display
                           FROM wells w
                           LEFT JOIN reagent_catalog r ON r.id = w.reagent_catalog_id
                           WHERE w.plate_id = ? ORDER BY w.col_number, w.row_label""",
                        (p["plate_id"],),
                    ).fetchall()
                ]
                p["well_measurements"] = [
                    dict(m) for m in con.execute(
                        """SELECT wm.*, pr.name AS protocol_name_ref
                           FROM well_measurements wm
                           LEFT JOIN protocols pr ON pr.id = wm.protocol_id
                           WHERE wm.plate_id = ? ORDER BY wm.well_id, wm.cycle""",
                        (p["plate_id"],),
                    ).fetchall()
                ]
                plates_data.append(p)

        # Reagents used in this run
        reagent_names = set()
        for p in plates_data:
            for w in p.get("wells", []):
                if w.get("reagent_name"):
                    reagent_names.add(w["reagent_name"])
        reagents = []
        if reagent_names:
            with _conn() as con:
                placeholders = ",".join("?" * len(reagent_names))
                reagents = [
                    dict(r) for r in con.execute(
                        f"SELECT * FROM reagent_catalog WHERE name IN ({placeholders})",
                        list(reagent_names),
                    ).fetchall()
                ]

        # Protocols used in this run
        with _conn() as con:
            protocols_used = [
                dict(r) for r in con.execute(
                    """SELECT DISTINCT p.* FROM protocols p
                       JOIN instrument_measurements im ON im.protocol_id = p.id
                       WHERE im.run_id = ?""",
                    (run_id,),
                ).fetchall()
            ]

        # Remove heavy result_json from summary
        run.pop("result_json", None)

        return {
            "schema_version": "2.0",
            "run": run,
            "plates": plates_data,
            "reagents": reagents,
            "protocols": protocols_used,
        }
    except Exception as e:
        logger.error(f"Failed to export run JSON {run_id}: {e}")
        return None


def get_well_heatmap(plate_id: str, measurement_type: Optional[str] = None) -> Dict[str, Any]:
    """
    Return well grid data for heatmap rendering.
    Result: {plate_id, rows, columns, wells: {well_id: {value, unit, reagent, volume_ul}}}
    """
    try:
        with _conn() as con:
            plate = con.execute("SELECT rows, columns FROM plates WHERE plate_id = ?", (plate_id,)).fetchone()
            rows_count = plate["rows"] if plate else 8
            cols_count = plate["columns"] if plate else 12

            q_filter = "AND wm.measurement_type = ?" if measurement_type else ""
            params = [plate_id]
            if measurement_type:
                params.append(measurement_type)

            well_rows = con.execute(
                f"""SELECT w.well_id, w.reagent_name, w.volume_ul, w.concentration_mm,
                           AVG(wm.value) AS value, wm.unit, wm.measurement_type,
                           wm.wavelength_nm
                    FROM wells w
                    LEFT JOIN well_measurements wm ON wm.plate_id = w.plate_id
                                                  AND wm.well_id  = w.well_id
                    WHERE w.plate_id = ? {q_filter}
                    GROUP BY w.well_id
                    ORDER BY w.col_number, w.row_label""",
                params,
            ).fetchall()

            wells_dict = {r["well_id"]: dict(r) for r in well_rows}
            return {
                "plate_id": plate_id,
                "rows": rows_count,
                "columns": cols_count,
                "wells": wells_dict,
            }
    except Exception as e:
        logger.error(f"Failed to get heatmap for {plate_id}: {e}")
        return {"plate_id": plate_id, "rows": 8, "columns": 12, "wells": {}}
