"""
Run Archive - persists per-run command logs + summaries, with rotation
========================================================================

Opentrons keeps the authoritative run history in its own app/API until the
run is explicitly removed there - this local copy is a convenience
supplement for debugging and tip/waste reconciliation, not the sole
historical record. Retention is therefore kept short deliberately (see
config.yaml run_archive section) without risking data loss.
"""
import gzip
import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class RunArchive:
    """
    Writes one JSON file per run ({summary, commands}) and applies a
    two-stage retention policy on every save:
        - files younger than retention_uncompressed_days: left as plain .json
        - older than that (but younger than retention_compressed_days,
          measured from the ORIGINAL run time, not from compression time):
          gzip'd to .json.gz
        - older than retention_compressed_days: deleted
    """

    def __init__(
        self,
        archive_dir: str,
        retention_uncompressed_days: float = 3,
        retention_compressed_days: float = 30,
    ):
        self._dir = archive_dir
        self._retention_uncompressed_days = retention_uncompressed_days
        self._retention_compressed_days = retention_compressed_days
        os.makedirs(self._dir, exist_ok=True)

    def save(self, run_id: str, summary: Dict[str, Any], commands: List[Dict[str, Any]]) -> str:
        """
        Write one archive file for a completed run. Filename is prefixed with
        a sortable UTC timestamp so retention sweeps can rely on filename
        order without re-parsing every file. Returns the path written, or
        "" on failure (never raises - archiving must not break a real run).
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_run_id = "".join(c for c in run_id if c.isalnum() or c in "-_") or "unknown"
        filename = f"{timestamp}_{safe_run_id}.json"
        path = os.path.join(self._dir, filename)
        payload = {"summary": summary, "commands": commands}
        try:
            os.makedirs(self._dir, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Failed to write run archive for {run_id}: {e}")
            return ""
        return path

    def sweep_retention(self, now: Optional[float] = None) -> Dict[str, int]:
        """
        Apply the retention policy to everything currently in the archive
        directory. Cheap (a directory listing plus per-file mtime checks) -
        safe to call after every run. Never raises; logs and skips files it
        can't process.
        """
        now = now if now is not None else time.time()
        compressed = 0
        deleted = 0

        if not os.path.exists(self._dir):
            return {"compressed": 0, "deleted": 0}

        uncompressed_cutoff = now - self._retention_uncompressed_days * 86400
        compressed_cutoff = now - self._retention_compressed_days * 86400

        for entry in os.listdir(self._dir):
            path = os.path.join(self._dir, entry)
            if not os.path.isfile(path):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue

            if entry.endswith(".json") and mtime < uncompressed_cutoff:
                gz_path = path + ".gz"
                try:
                    with open(path, 'rb') as f_in, gzip.open(gz_path, 'wb') as f_out:
                        shutil.copyfileobj(f_in, f_out)
                    # Preserve the original run time on the compressed file so
                    # the *next* sweep's compressed_cutoff check is measured
                    # from when the run happened, not from when it got gzip'd -
                    # otherwise compressing late would silently extend total
                    # retention past retention_compressed_days.
                    os.utime(gz_path, (mtime, mtime))
                    os.remove(path)
                    compressed += 1
                except Exception as e:
                    logger.warning(f"Failed to compress run archive {entry}: {e}")

            elif entry.endswith(".json.gz") and mtime < compressed_cutoff:
                try:
                    os.remove(path)
                    deleted += 1
                except Exception as e:
                    logger.warning(f"Failed to delete old run archive {entry}: {e}")

        if compressed or deleted:
            logger.info(f"Run archive retention sweep: compressed={compressed}, deleted={deleted}")
        return {"compressed": compressed, "deleted": deleted}
