import logging
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from pathlib import Path

from sila2.server import MetadataDict

from generated.laboslibraryprovider.laboslibraryprovider_base import LabOSLibraryProviderBase

logger = logging.getLogger(__name__)

_EMPTY_CATALOG = '{"library_types": []}'


class LibraryProviderImpl(LabOSLibraryProviderBase):
    def __init__(self, parent_server):
        super().__init__(parent_server=parent_server)
        self._catalog_path = Path(__file__).parent.parent / "library_catalog.json"

    def get_LibraryCatalog(self, *, metadata: MetadataDict) -> str:
        if not self._catalog_path.exists():
            return _EMPTY_CATALOG
        try:
            return self._catalog_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error(f"Failed to read library catalog {self._catalog_path}: {exc}")
            return _EMPTY_CATALOG
