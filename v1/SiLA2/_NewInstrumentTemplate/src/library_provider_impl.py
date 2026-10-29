"""LabOSLibraryProvider: what this server declares about itself.

The same file is in every LabOS server; `python labos.py doctor` checks the
copies have not drifted apart. Nothing in it is specific to an instrument -
everything specific is in this server's `library_catalog.json`:

    display             how the instrument looks in the web interface
    library_types       files it reads from Library/, and which parameters pick them
    result_types        folders under Results/ it writes its data into
    parameter_defaults  parameters a workflow may leave out
    traceability        which commands prepare or measure a plate

When the server starts, every folder the catalog names is created, so an
instrument that needs `Results/Cells` has it before its first measurement,
without anybody making it by hand. The orchestrator reads the same catalog at
discovery and creates the folders on its side too, which is what matters when
this server runs on another computer.

Folders are relative to the LabOS root: the directory holding `SiLA2/`, or
`LABOS_ROOT` when this server has been copied somewhere else.
"""

import json
import logging
import os
import sys
from pathlib import Path, PurePosixPath

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from sila2.server import MetadataDict

from generated.laboslibraryprovider.laboslibraryprovider_base import LabOSLibraryProviderBase

logger = logging.getLogger(__name__)

_EMPTY_CATALOG = '{"library_types": []}'

#: The only trees a catalog may create folders in.
_ALLOWED_TOPS = ("Library", "Results")


def labos_root() -> Path:
    """Where Library/ and Results/ live, as seen from this server."""
    override = os.environ.get("LABOS_ROOT")
    if override:
        return Path(override)
    return Path(_SERVER_DIR).resolve().parent.parent


def declared_folders(catalog: dict) -> list:
    """Every concrete folder a catalog names, as relative POSIX paths."""
    folders = []
    for entry in catalog.get("library_types") or []:
        if isinstance(entry, dict) and entry.get("folder") and not entry.get("values"):
            folders.append(entry["folder"])
    for entry in catalog.get("result_types") or []:
        if isinstance(entry, dict) and entry.get("folder"):
            folders.append(entry["folder"])
    return folders


def ensure_declared_folders(catalog: dict, root: Path) -> list:
    """Create the declared folders under root. Returns the ones created."""
    created = []
    for folder in declared_folders(catalog):
        path = PurePosixPath(str(folder).replace("\\", "/").strip("/"))
        if (path.is_absolute() or ".." in path.parts or not path.parts
                or path.parts[0] not in _ALLOWED_TOPS):
            logger.warning("Catalog folder %r is not under %s; not created",
                           folder, " or ".join(_ALLOWED_TOPS))
            continue
        target = root.joinpath(*path.parts)
        if not target.is_dir():
            try:
                target.mkdir(parents=True, exist_ok=True)
                created.append(target)
                logger.info("Created %s (declared in library_catalog.json)", target)
            except OSError as exc:
                logger.error("Could not create %s: %s", target, exc)
    return created


class LibraryProviderImpl(LabOSLibraryProviderBase):
    def __init__(self, parent_server):
        super().__init__(parent_server=parent_server)
        self._catalog_path = Path(__file__).parent.parent / "library_catalog.json"
        try:
            ensure_declared_folders(self._read_catalog(), labos_root())
        except Exception as exc:  # a folder problem must not stop the instrument
            logger.error("Could not prepare the folders this server declares: %s", exc)

    def _read_catalog(self) -> dict:
        if not self._catalog_path.exists():
            return {}
        try:
            data = json.loads(self._catalog_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error("Unreadable catalog %s: %s", self._catalog_path, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def get_LibraryCatalog(self, *, metadata: MetadataDict) -> str:
        if not self._catalog_path.exists():
            return _EMPTY_CATALOG
        try:
            return self._catalog_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error(f"Failed to read library catalog {self._catalog_path}: {exc}")
            return _EMPTY_CATALOG
