"""SQLite persistence layer for LabOS — workflow runs, plates, wells, measurements."""

from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime as _dt
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
#
#  Two clearly separated groups of tables, in one file for atomic writes and
#  simple backup/query — not two databases:
#
#    AUTOMATION_TABLES — workflow/step execution log (run_results). Audit
#    trail of *how* a run went (timing, retries, status). Not meant to be
#    queried for chemistry.
#
#    CHEMICAL_TABLES — the actual scientific record: what was measured
#    (instrument_measurements, protocols, protocol_channels), what a plate
#    is and what went into each well (plates, wells, reagent_catalog), and
#    the resulting per-well values (well_measurements). This is what the
#    Plates page/tab renders and what export_plate_csv()/export_run_json()
#    are built around.
# ─────────────────────────────────────────────────────────────────────────────

AUTOMATION_TABLES = ("run_results", "run_steps")
CHEMICAL_TABLES = (
    "protocols", "protocol_channels", "instrument_measurements",
    "plates", "reagent_catalog", "wells", "well_measurements",
)

_SCHEMA = """
-- ═══════════════════════════════════════════════════════════════════════════
--  AUTOMATION / EXECUTION LOG
-- ═══════════════════════════════════════════════════════════════════════════

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

-- ── Per-step execution timeline ───────────────────────────────────────────────
-- One row per step attempt (a retry is a new row, so the retry history stays
-- visible instead of being overwritten). Written as the step starts and again
-- as it ends, so a run that crashes mid-way still leaves a diagnosable trail:
-- run_results alone only ever recorded a start and an end.
CREATE TABLE IF NOT EXISTS run_steps (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL,
    step_number       INTEGER,
    attempt           INTEGER DEFAULT 1,
    instrument        TEXT,
    action            TEXT,
    parameters_json   TEXT,
    status            TEXT,          -- pending/running/success/failed/skipped/waiting_intervention
    started_at        TEXT,
    ended_at          TEXT,
    duration_seconds  REAL,
    error             TEXT,
    result_json       TEXT,
    plate_id          TEXT,
    FOREIGN KEY (run_id) REFERENCES run_results(run_id) ON DELETE CASCADE
);

-- ═══════════════════════════════════════════════════════════════════════════
--  CHEMICAL RECORD
-- ═══════════════════════════════════════════════════════════════════════════

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

-- ── Protocol channels: one row per distinct technique/wavelength within a  ──
-- ── single AnIML file (a Tecan run can mix e.g. absorbance + fluorescence) ──
CREATE TABLE IF NOT EXISTS protocol_channels (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol_id       INTEGER NOT NULL,
    step_name         TEXT,            -- AnIML ExperimentStep name/index
    channel_index     INTEGER,         -- 0-based order within the file
    measurement_type  TEXT,
    wavelength_nm     REAL,
    excitation_nm     REAL,
    emission_nm       REAL,
    integration_ms    REAL,
    num_reads         INTEGER,
    bandwidth_nm      REAL,
    parameters_json   TEXT,
    created_at        TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(protocol_id) REFERENCES protocols(id),
    UNIQUE(protocol_id, step_name)
);

-- ── Tecan/instrument command records (one row per SiLA2 command invocation) ──
CREATE TABLE IF NOT EXISTS instrument_measurements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT,
    plate_id        TEXT,
    instrument      TEXT,
    command         TEXT,
    executed_at     TEXT,
    animl_path      TEXT,
    protocol_name   TEXT,
    protocol_id     INTEGER,
    protocol_channel_id INTEGER,
    measurement_type TEXT,
    result_json     TEXT,
    FOREIGN KEY(run_id)      REFERENCES run_results(run_id),
    FOREIGN KEY(plate_id)    REFERENCES plates(plate_id),
    FOREIGN KEY(protocol_id) REFERENCES protocols(id),
    FOREIGN KEY(protocol_channel_id) REFERENCES protocol_channels(id)
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
    protocol_channel_id INTEGER,      -- FK to protocol_channels.id (which technique/wavelength)
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
    FOREIGN KEY(protocol_id)   REFERENCES protocols(id),
    FOREIGN KEY(protocol_channel_id) REFERENCES protocol_channels(id)
);

-- ── Indexes ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_run_results_workflow   ON run_results(workflow_name);
CREATE INDEX IF NOT EXISTS idx_run_results_status     ON run_results(status);
CREATE INDEX IF NOT EXISTS idx_measurements_run       ON instrument_measurements(run_id);
CREATE INDEX IF NOT EXISTS idx_measurements_plate     ON instrument_measurements(plate_id);
CREATE INDEX IF NOT EXISTS idx_protocol_channels_proto ON protocol_channels(protocol_id);
CREATE INDEX IF NOT EXISTS idx_plates_run             ON plates(run_id);
CREATE INDEX IF NOT EXISTS idx_plates_recipe          ON plates(recipe_name);
CREATE INDEX IF NOT EXISTS idx_wells_plate            ON wells(plate_id);
CREATE INDEX IF NOT EXISTS idx_wells_reagent          ON wells(reagent_name);
-- Real uniqueness on the chemical content of a well: allows several
-- reagents in the same well (multi-reagent wells), rejects true duplicate
-- (plate, well, reagent) rows on re-extraction. NULL reagent_name is never
-- deduplicated by SQLite's UNIQUE-index-with-NULL semantics, but save_wells()
-- never inserts a row without a reagent_name today.
CREATE UNIQUE INDEX IF NOT EXISTS uq_wells_content    ON wells(plate_id, well_id, reagent_name);
CREATE INDEX IF NOT EXISTS idx_well_meas_plate        ON well_measurements(plate_id);
CREATE INDEX IF NOT EXISTS idx_well_meas_protocol     ON well_measurements(protocol_id);
CREATE INDEX IF NOT EXISTS idx_reagent_name           ON reagent_catalog(name);
CREATE INDEX IF NOT EXISTS idx_run_steps_run          ON run_steps(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_step_attempt ON run_steps(run_id, step_number, attempt);
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
    "ALTER TABLE run_results ADD COLUMN notes TEXT",
    # Chemical/automation split follow-up (see database.py module docstring/banners):
    "ALTER TABLE instrument_measurements ADD COLUMN plate_id TEXT",
    "ALTER TABLE instrument_measurements ADD COLUMN protocol_channel_id INTEGER",
    "ALTER TABLE well_measurements ADD COLUMN protocol_channel_id INTEGER",
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
    """Insert or replace a workflow run summary record (automation group only).

    Does NOT touch instrument_measurements — those are written eagerly by
    save_instrument_measurement() at the moment a Tecan step actually
    completes (see src/plate_tracking.py), not replayed from step_results
    here. Replaying was the root cause of instrument_measurements.animl_path
    always being NULL (step_results never carried the structured SiLA2
    response) and of measurement_id/plate linkage races.
    """
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
    except Exception as e:
        logger.error(f"Failed to save run to database: {e}")


def save_run_step(step_data: Dict[str, Any]) -> None:
    """Insert or update one step attempt.

    Called twice per attempt: once when the step starts (status "running",
    no end time) and once when it finishes. Keyed on
    (run_id, step_number, attempt) so the second call updates the first row
    rather than adding a duplicate, while a *retry* — a new attempt number —
    gets its own row and the retry history stays readable.
    """
    try:
        params = step_data.get("parameters")
        result = step_data.get("result")
        with _conn() as con:
            con.execute(
                """
                INSERT INTO run_steps
                    (run_id, step_number, attempt, instrument, action,
                     parameters_json, status, started_at, ended_at,
                     duration_seconds, error, result_json, plate_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, step_number, attempt) DO UPDATE SET
                    -- COALESCE on everything the completion call may legitimately
                    -- omit: it reports an outcome, and must not blank out what the
                    -- start call already recorded. Only status always wins.
                    instrument       = COALESCE(excluded.instrument, run_steps.instrument),
                    action           = COALESCE(excluded.action, run_steps.action),
                    parameters_json  = COALESCE(excluded.parameters_json, run_steps.parameters_json),
                    started_at       = COALESCE(run_steps.started_at, excluded.started_at),
                    status           = excluded.status,
                    ended_at         = COALESCE(excluded.ended_at, run_steps.ended_at),
                    duration_seconds = COALESCE(excluded.duration_seconds, run_steps.duration_seconds),
                    error            = COALESCE(excluded.error, run_steps.error),
                    result_json      = COALESCE(excluded.result_json, run_steps.result_json),
                    plate_id         = COALESCE(excluded.plate_id, run_steps.plate_id)
                """,
                (
                    step_data.get("run_id"),
                    step_data.get("step_number"),
                    int(step_data.get("attempt", 1) or 1),
                    step_data.get("instrument"),
                    step_data.get("action"),
                    json.dumps(params, default=str) if params is not None else None,
                    step_data.get("status"),
                    step_data.get("started_at"),
                    step_data.get("ended_at"),
                    step_data.get("duration_seconds"),
                    step_data.get("error"),
                    json.dumps(result, default=str) if result is not None else None,
                    step_data.get("plate_id"),
                ),
            )
    except Exception as e:
        # Never let bookkeeping abort a run that is physically in progress.
        logger.error(f"Failed to save run step: {e}")


def get_run_steps(run_id: str) -> List[Dict[str, Any]]:
    """Every recorded step attempt for a run, in execution order."""
    try:
        with _conn() as con:
            rows = con.execute(
                "SELECT * FROM run_steps WHERE run_id = ? ORDER BY step_number, attempt",
                (run_id,),
            ).fetchall()
            steps = []
            for row in rows:
                step = dict(row)
                for raw_key, key in (("parameters_json", "parameters"), ("result_json", "result")):
                    if step.get(raw_key):
                        try:
                            step[key] = json.loads(step[raw_key])
                        except (TypeError, ValueError):
                            step[key] = None
                steps.append(step)
            return steps
    except Exception as e:
        logger.error(f"Failed to get steps for run {run_id}: {e}")
        return []


def update_run_status(run_id: str, status: str, **fields: Any) -> None:
    """Patch a run's status (and any other run_results columns) in place.

    Used by the run manager for the transitions a run goes through while it is
    alive — running -> paused -> running -> completed/failed/aborted — without
    rewriting the whole record through save_run().
    """
    allowed = {
        "completed_at", "duration_seconds", "steps_total", "steps_completed",
        "steps_failed", "steps_skipped", "result_json", "notes", "workflow_name",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    updates["status"] = status
    assignments = ", ".join(f"{k} = ?" for k in updates)
    try:
        with _conn() as con:
            con.execute(
                f"UPDATE run_results SET {assignments} WHERE run_id = ?",
                (*updates.values(), run_id),
            )
    except Exception as e:
        logger.error(f"Failed to update run {run_id}: {e}")


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
                       steps_skipped, notes, created_at
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
            data["steps"] = get_run_steps(run_id)
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


def search_runs(
    query: str = "",
    date_from: str = "",
    date_to: str = "",
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Full-text + date-range search over run_results."""
    try:
        filters: list = []
        params: list = []
        if query:
            filters.append("workflow_name LIKE ?")
            params.append(f"%{query}%")
        if date_from:
            filters.append("started_at >= ?")
            params.append(date_from)
        if date_to:
            filters.append("started_at <= ?")
            params.append(date_to + "T23:59:59")
        if status:
            filters.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        params += [limit, offset]
        with _conn() as con:
            rows = con.execute(
                f"""
                SELECT run_id, workflow_name, started_at, completed_at, status,
                       duration_seconds, steps_total, steps_completed, steps_failed,
                       steps_skipped, notes, created_at
                FROM run_results
                {where}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Failed to search runs: {e}")
        return []


def rename_run(run_id: str, new_name: Optional[str], notes: Optional[str]) -> bool:
    """Update workflow_name and/or notes for a run. Returns True if found."""
    try:
        with _conn() as con:
            cur = con.execute(
                """UPDATE run_results
                   SET workflow_name = COALESCE(?, workflow_name),
                       notes = ?
                   WHERE run_id = ?""",
                (new_name, notes, run_id),
            )
            return cur.rowcount > 0
    except Exception as e:
        logger.error(f"Failed to rename run '{run_id}': {e}")
        return False


def delete_run(run_id: str) -> bool:
    """Delete a run and its instrument_measurements. Plates are preserved."""
    try:
        with _conn() as con:
            con.execute("DELETE FROM instrument_measurements WHERE run_id = ?", (run_id,))
            cur = con.execute("DELETE FROM run_results WHERE run_id = ?", (run_id,))
            return cur.rowcount > 0
    except Exception as e:
        logger.error(f"Failed to delete run '{run_id}': {e}")
        return False


def update_plate(plate_id: str, display_name: Optional[str], notes: Optional[str]) -> bool:
    """Update display_name and/or notes for a plate. Returns True if found."""
    try:
        with _conn() as con:
            cur = con.execute(
                """UPDATE plates
                   SET display_name = COALESCE(?, display_name),
                       notes = ?
                   WHERE plate_id = ?""",
                (display_name, notes, plate_id),
            )
            return cur.rowcount > 0
    except Exception as e:
        logger.error(f"Failed to update plate '{plate_id}': {e}")
        return False


def delete_plate(plate_id: str) -> bool:
    """Delete a plate and all its wells and well_measurements."""
    try:
        with _conn() as con:
            con.execute("DELETE FROM well_measurements WHERE plate_id = ?", (plate_id,))
            con.execute("DELETE FROM wells WHERE plate_id = ?", (plate_id,))
            cur = con.execute("DELETE FROM plates WHERE plate_id = ?", (plate_id,))
            return cur.rowcount > 0
    except Exception as e:
        logger.error(f"Failed to delete plate '{plate_id}': {e}")
        return False


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


def get_or_create_protocol_channel(
    protocol_id: Optional[int], step_name: str, channel_data: Optional[Dict[str, Any]] = None
) -> Optional[int]:
    """Return protocol_channels id for (protocol_id, step_name), inserting if absent.

    One channel per distinct AnIML experiment_step, so a single file mixing
    e.g. absorbance + fluorescence records each technique separately instead
    of collapsing into one protocols row (which only ever captured the first
    non-null step).
    """
    if not protocol_id or not step_name:
        return None
    try:
        with _conn() as con:
            row = con.execute(
                "SELECT id FROM protocol_channels WHERE protocol_id = ? AND step_name = ?",
                (protocol_id, step_name),
            ).fetchone()
            if row:
                return row["id"]
            d = channel_data or {}
            cur = con.execute(
                """INSERT INTO protocol_channels
                    (protocol_id, step_name, channel_index, measurement_type,
                     wavelength_nm, excitation_nm, emission_nm,
                     integration_ms, num_reads, bandwidth_nm, parameters_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    protocol_id,
                    step_name,
                    d.get("channel_index"),
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
        logger.error(f"Failed to get/create protocol channel '{step_name}': {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Instrument measurements (one row per SiLA2 command invocation that
#  produced/touched a measurement — chemical group, see module banner above)
# ─────────────────────────────────────────────────────────────────────────────

def save_instrument_measurement(data: Dict[str, Any]) -> Optional[int]:
    """Insert one instrument_measurements row and return its id immediately.

    Called eagerly, synchronously, right when a Tecan step completes and its
    animl_path/plate_id are already known — not derived later from a replay
    of step_results (see save_run()'s docstring for why that was broken).
    """
    try:
        with _conn() as con:
            cur = con.execute(
                """
                INSERT INTO instrument_measurements
                    (run_id, plate_id, instrument, command, executed_at,
                     animl_path, protocol_name, protocol_id, protocol_channel_id,
                     measurement_type, result_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("run_id"),
                    data.get("plate_id"),
                    data.get("instrument"),
                    data.get("command"),
                    data.get("executed_at"),
                    data.get("animl_path"),
                    data.get("protocol_name"),
                    data.get("protocol_id"),
                    data.get("protocol_channel_id"),
                    data.get("measurement_type"),
                    json.dumps(data.get("result"), default=str) if data.get("result") else None,
                ),
            )
            return cur.lastrowid
    except Exception as e:
        logger.error(f"Failed to save instrument measurement: {e}")
        return None


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


def search_plates(
    query: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Full-text + date-range search over plates - mirrors search_runs()."""
    try:
        filters: list = []
        params: list = []
        if query:
            filters.append("(plate_id LIKE ? OR recipe_name LIKE ? OR display_name LIKE ?)")
            params += [f"%{query}%", f"%{query}%", f"%{query}%"]
        if date_from:
            filters.append("prepared_at >= ?")
            params.append(date_from)
        if date_to:
            filters.append("prepared_at <= ?")
            params.append(date_to + "T23:59:59")
        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        params += [limit, offset]
        with _conn() as con:
            rows = con.execute(
                f"SELECT * FROM plates {where} ORDER BY prepared_at DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Failed to search plates: {e}")
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
            # Raw instrument runs (AnIML files etc.) linked to this plate via
            # instrument_measurements.plate_id — lets the UI link back to the
            # actual raw output file, not just the DB-computed values.
            data["raw_results"] = [
                dict(r)
                for r in con.execute(
                    """SELECT id, run_id, instrument, command, executed_at,
                              animl_path, protocol_name, measurement_type
                       FROM instrument_measurements
                       WHERE plate_id = ?
                       ORDER BY executed_at""",
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
                    (plate_id, well_id, measurement_id, protocol_id, protocol_channel_id,
                     value, unit, measurement_type,
                     wavelength_nm, excitation_nm, emission_nm,
                     cycle, measured_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        plate_id,
                        wv.get("well"),
                        measurement_id,
                        protocol_id,
                        wv.get("protocol_channel_id"),
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
    Return a structured dict with the run's automation record only: run
    metadata, per-instrument measurement log entries (which instrument, which
    AnIML file, when), and which plate(s) it touched by reference
    (plate_id/display_name/status — no wells/reagents/measurements).

    Deliberately excludes the chemical record (wells, reagent_catalog,
    well_measurements) — that belongs to export_plate_json() only, so a run
    export answers "what did the orchestrator do" without duplicating "what
    was in the wells", which the user wants kept strictly separate.
    """
    try:
        run = get_run(run_id)
        if run is None:
            return None
        # Remove heavy result_json from summary
        run.pop("result_json", None)
        return {
            "schema_version": "3.0",
            "run": run,
        }
    except Exception as e:
        logger.error(f"Failed to export run JSON {run_id}: {e}")
        return None


def export_plate_json(plate_id: str) -> Optional[Dict[str, Any]]:
    """
    Return the complete chemical record for a plate, ready for AI
    consumption: everything get_plate() returns (wells, reagent-catalog
    joins, well_measurements, raw_results) plus, for every raw_results row
    that has an animl_path, the actual parsed measurement data under
    "animl_data" (key kept for API stability even though the source file
    parsed underneath may be the raw XML - see below) — so consuming this
    export doesn't require a second round-trip to fetch and parse the raw
    instrument file, AND so a well with a multi-wavelength scan carries its
    full spectrum here (measurements[].spectrum), not just the single
    scan-mean value shown in well_measurements/the heatmap.

    Parses the same source plate_tracking.ingest_animl_measurement() prefers
    (the raw Tecan XML sibling of the AnIML file, when present - see
    plate_tracking._resolve_source_path) so this export can never show data
    inconsistent with what's actually in well_measurements/the DB, and never
    depends on the AnIML conversion step (the source of every bug found in
    this pipeline) for its own numbers.

    A raw_results row whose source file is missing or fails to parse gets
    animl_data=None (logged as a warning) rather than failing the whole
    export — one bad/rotated-away file shouldn't hide the rest of the plate.
    """
    try:
        data = get_plate(plate_id)
        if data is None:
            return None

        from src.plate_tracking import _resolve_source_path

        for raw_result in data.get("raw_results", []):
            animl_path = raw_result.get("animl_path")
            raw_result["animl_data"] = None
            if not animl_path:
                continue
            try:
                source_path = _resolve_source_path(animl_path)
                if source_path.suffix.lower() == ".xml":
                    from src.tecan_xml_parser import parse_tecan_xml
                    raw_result["animl_data"] = parse_tecan_xml(source_path)
                else:
                    from src.animl_parser import parse_animl
                    raw_result["animl_data"] = parse_animl(source_path)
            except Exception as e:
                logger.warning(f"Could not parse measurement file for '{animl_path}' in plate export: {e}")

        return {
            "schema_version": "1.0",
            "plate": data,
        }
    except Exception as e:
        logger.error(f"Failed to export plate JSON {plate_id}: {e}")
        return None


def export_ai_context(
    status_filter: str = "completed",
    run_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Return a structured dict with ALL runs (default: completed only) suitable
    for feeding to an LLM to generate new experiment recipes.

    Structure:
    {
      "schema_version": "2.0",
      "export_purpose": "AI recipe generation context",
      "reagent_catalog": [...],   # all known reagents
      "protocols": [...],         # all measurement protocols
      "experiments": [            # one entry per run
        {
          "run_id": ...,
          "workflow_name": ...,
          "status": ...,
          "duration_seconds": ...,
          "started_at": ...,
          "plates": [
            {
              "plate_id": ...,
              "plate_type": ...,
              "recipe_name": ...,
              "wells": [...],         # reagent + volume per well
              "measurements": [...]   # OD/fluorescence per well
            }
          ],
          "errors": [...]
        }
      ]
    }
    """
    try:
        with _conn() as con:
            if run_ids:
                placeholders = ",".join("?" * len(run_ids))
                where = f"WHERE run_id IN ({placeholders})"
                params_r: list = list(run_ids)
            elif status_filter:
                where = "WHERE status = ?"
                params_r = [status_filter]
            else:
                where, params_r = "", []
            run_rows = con.execute(
                f"SELECT * FROM run_results {where} ORDER BY started_at",
                params_r,
            ).fetchall()

            reagents = [dict(r) for r in con.execute("SELECT * FROM reagent_catalog ORDER BY name").fetchall()]
            protocols = [dict(r) for r in con.execute("SELECT * FROM protocols ORDER BY name").fetchall()]

        experiments = []
        for rrow in run_rows:
            run_dict = dict(rrow)
            result_json = run_dict.pop("result_json", None)
            errors: list = []
            step_results_raw: list = []
            if result_json:
                try:
                    _parsed = json.loads(result_json)
                    errors = _parsed.get("errors", [])
                    step_results_raw = _parsed.get("step_results", [])
                except Exception:
                    pass

            enriched_steps = []
            for _i, _step in enumerate(step_results_raw):
                _s = dict(_step)
                if _step.get("timestamp") and _i + 1 < len(step_results_raw):
                    _next_ts = step_results_raw[_i + 1].get("timestamp")
                    if _next_ts:
                        try:
                            _t1 = _dt.fromisoformat(_step["timestamp"].replace("Z", "+00:00"))
                            _t2 = _dt.fromisoformat(_next_ts.replace("Z", "+00:00"))
                            _s["time_to_next_step_seconds"] = round((_t2 - _t1).total_seconds(), 1)
                        except Exception:
                            pass
                enriched_steps.append(_s)

            operator_waits = [
                {"step": _s.get("step"), "action": _s.get("action", _s.get("command")),
                 "wait_seconds": _s["time_to_next_step_seconds"]}
                for _s in enriched_steps
                if "Manual" in _s.get("instrument", "")
                   and _s.get("time_to_next_step_seconds") is not None
            ]

            with _conn() as con:
                plate_rows = con.execute(
                    "SELECT * FROM plates WHERE run_id = ?", (run_dict["run_id"],)
                ).fetchall()

            plates_out = []
            for plate in plate_rows:
                p = dict(plate)
                with _conn() as con:
                    wells = [
                        dict(w) for w in con.execute(
                            """SELECT w.well_id, w.row_label, w.col_number,
                                      w.reagent_name, w.volume_ul, w.concentration_mm,
                                      w.liquid_class, w.phase_name,
                                      r.cas_number, r.molecular_formula,
                                      r.display_name AS reagent_display
                               FROM wells w
                               LEFT JOIN reagent_catalog r ON r.id = w.reagent_catalog_id
                               WHERE w.plate_id = ?
                               ORDER BY w.col_number, w.row_label""",
                            (p["plate_id"],),
                        ).fetchall()
                    ]
                    measurements = [
                        dict(m) for m in con.execute(
                            """SELECT wm.well_id, wm.value, wm.unit, wm.measurement_type,
                                      wm.wavelength_nm, wm.excitation_nm, wm.emission_nm,
                                      wm.cycle, pr.name AS protocol_name
                               FROM well_measurements wm
                               LEFT JOIN protocols pr ON pr.id = wm.protocol_id
                               WHERE wm.plate_id = ?
                               ORDER BY wm.well_id, wm.cycle""",
                            (p["plate_id"],),
                        ).fetchall()
                    ]

                plates_out.append({
                    "plate_id": p["plate_id"],
                    "plate_type": p["plate_type"],
                    "display_name": p["display_name"],
                    "recipe_name": p["recipe_name"],
                    "status": p["status"],
                    "wells": wells,
                    "measurements": measurements,
                })

            experiments.append({
                "run_id": run_dict["run_id"],
                "workflow_name": run_dict["workflow_name"],
                "notes": run_dict.get("notes"),
                "status": run_dict["status"],
                "duration_seconds": run_dict["duration_seconds"],
                "started_at": run_dict["started_at"],
                "errors": errors,
                "step_results": enriched_steps,
                "operator_wait_times": operator_waits,
                "total_operator_wait_seconds": round(sum(w["wait_seconds"] for w in operator_waits), 1),
                "failed_steps": [_s for _s in enriched_steps if _s.get("status") == "failed"],
                "plates": plates_out,
            })

        return {
            "schema_version": "2.0",
            "export_purpose": "AI recipe generation context",
            "total_experiments": len(experiments),
            "reagent_catalog": reagents,
            "protocols": protocols,
            "experiments": experiments,
        }
    except Exception as e:
        logger.error(f"Failed to export AI context: {e}")
        return {}


def get_well_heatmap(
    plate_id: str, measurement_type: Optional[str] = None, measurement_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Return well grid data for heatmap rendering, shaped exactly as
    webapp/templates/database.html (formerly plates.html) consumes it.

    Result: {plate_id, rows, columns, row_labels, value_min, value_max,
             grid: [[{well_id, value, unit, reagent_name, volume_ul,
                      concentration_mm, measurement_type, wavelength_nm}, ...], ...]}

    Driven by the UNION of well_ids present in `wells` and `well_measurements`
    (not just `wells`), so a well that was measured but has no pipetting
    record (extraction failed, or a manual/no-recipe measurement) still
    shows up instead of silently disappearing.

    measurement_id restricts the grid to ONE raw result (one
    instrument_measurements row / one attached AnIML file) instead of
    averaging every well_measurements row for the well - without it, a plate
    with several timepoints of the SAME technique (e.g. t0/3h/24h
    absorbance, all "Absorbance") silently blends them into one AVG(value)
    per well, which looks like real data but isn't any single actual
    reading. measurement_type still narrows by technique on top of that
    (or alone, if measurement_id is omitted) exactly as before.
    """
    empty = {"plate_id": plate_id, "rows": 8, "columns": 12, "row_labels": [],
              "value_min": None, "value_max": None, "grid": []}
    try:
        with _conn() as con:
            plate = con.execute("SELECT rows, columns FROM plates WHERE plate_id = ?", (plate_id,)).fetchone()
            rows_count = (plate["rows"] if plate and plate["rows"] else 8) or 8
            cols_count = (plate["columns"] if plate and plate["columns"] else 12) or 12

            extra_filters = []
            params = [plate_id, plate_id, plate_id, plate_id]
            if measurement_type:
                extra_filters.append("AND wm.measurement_type = ?")
                params.append(measurement_type)
            if measurement_id is not None:
                extra_filters.append("AND wm.measurement_id = ?")
                params.append(measurement_id)
            q_filter = " ".join(extra_filters)

            well_rows = con.execute(
                f"""
                WITH well_ids AS (
                    SELECT well_id FROM wells WHERE plate_id = ?
                    UNION
                    SELECT well_id FROM well_measurements WHERE plate_id = ?
                )
                SELECT
                    wi.well_id,
                    w.reagent_name, w.volume_ul, w.concentration_mm,
                    AVG(wm.value) AS value, wm.unit, wm.measurement_type,
                    wm.wavelength_nm
                FROM well_ids wi
                LEFT JOIN wells w             ON w.plate_id = ? AND w.well_id = wi.well_id
                LEFT JOIN well_measurements wm ON wm.plate_id = ? AND wm.well_id = wi.well_id {q_filter}
                GROUP BY wi.well_id
                """,
                params,
            ).fetchall()

            wells_by_id = {r["well_id"]: dict(r) for r in well_rows}

            row_labels = [chr(ord('A') + i) for i in range(rows_count)]
            grid: List[List[Dict[str, Any]]] = []
            all_values: List[float] = []
            for ri, row_label in enumerate(row_labels):
                row_cells = []
                for col in range(1, cols_count + 1):
                    well_id = f"{row_label}{col}"
                    w = wells_by_id.get(well_id, {})
                    value = w.get("value")
                    if value is not None:
                        all_values.append(value)
                    row_cells.append({
                        "well_id": well_id,
                        "value": value,
                        "unit": w.get("unit"),
                        "reagent_name": w.get("reagent_name"),
                        "volume_ul": w.get("volume_ul"),
                        "concentration_mm": w.get("concentration_mm"),
                        "measurement_type": w.get("measurement_type"),
                        "wavelength_nm": w.get("wavelength_nm"),
                    })
                grid.append(row_cells)

            return {
                "plate_id": plate_id,
                "rows": rows_count,
                "columns": cols_count,
                "row_labels": row_labels,
                "value_min": min(all_values) if all_values else None,
                "value_max": max(all_values) if all_values else None,
                "grid": grid,
            }
    except Exception as e:
        logger.error(f"Failed to get heatmap for {plate_id}: {e}")
        return empty
