"""What a server declares about itself, and what the orchestrator does with it.

The user-facing promise: an instrument that needs a folder says so in its own
server, the folder exists from the first start, the web interface lists what
the instrument writes there, and nothing in the orchestrator or the web
interface names the instrument. These tests hold each part of that.
"""
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V1_DIR))

from src.catalog import (  # noqa: E402
    ServerCatalog, ensure_folders, feature_name, is_infrastructure_feature, parse_catalog,
)
from src.lab_core import LabCore  # noqa: E402


class TestParsing:

    def test_the_old_shape_still_reads(self):
        catalog = parse_catalog('{"library_types": [{"hint": "recipe", "folder": "Library/Recipes", '
                                '"parameter_names": ["RecipeName"]}]}')
        assert catalog.problems == []
        assert catalog.hint_for_parameter("RecipeName") == "recipe"
        assert catalog.result_types == [] and catalog.display == {}

    def test_every_section(self):
        catalog = parse_catalog({
            "display": {"icon": "bi-camera", "color": "warning"},
            "result_types": [{"display_name": "Cells", "folder": "Results/Cells",
                              "file_extensions": ["tif", ".PNG"]}],
            "parameter_defaults": {"Snap": {"Exposure": 20}},
            "traceability": {"Snap": {"role": "measures_plate", "plate_parameter": "PlateID"}},
        })
        assert catalog.problems == []
        assert catalog.display == {"icon": "bi-camera", "color": "warning"}
        (result,) = catalog.result_types
        assert (result.key, result.folder, result.file_extensions) == ("Cells", "Results/Cells", (".tif", ".png"))
        assert catalog.defaults_for("Snap") == {"Exposure": 20}
        assert catalog.role_of("Snap").plate_parameter == "PlateID"

    @pytest.mark.parametrize("raw", ["not json", "[1, 2", "42", None])
    def test_garbage_never_raises(self, raw):
        catalog = parse_catalog(raw)
        assert isinstance(catalog, ServerCatalog)

    @pytest.mark.parametrize("folder", ["../../Windows", "/etc", "C:/Users", "Results/../src"])
    def test_a_folder_that_leaves_its_tree_is_refused(self, folder):
        catalog = parse_catalog({"result_types": [{"folder": folder}]})
        assert catalog.result_types == []
        assert catalog.problems

    @pytest.mark.parametrize("display", [
        {"icon": "bi-x\" onmouseover=\"alert(1)"}, {"color": "red; background:url(x)"}])
    def test_display_values_are_not_markup(self, display):
        """Pages render these into HTML; a server on the network is not trusted."""
        catalog = parse_catalog({"display": display})
        assert catalog.display == {}
        assert catalog.problems

    def test_an_unknown_role_is_reported_not_guessed(self):
        catalog = parse_catalog({"traceability": {"Go": {"role": "does_something"}}})
        assert catalog.traceability == {} and catalog.problems

    def test_a_malformed_entry_costs_only_itself(self):
        catalog = parse_catalog({"library_types": [{"no": "hint"}, {"hint": "ok", "folder": "Library/Ok"}],
                                 "result_types": ["nope", {"folder": "Results/Fine"}]})
        assert [e["hint"] for e in catalog.library_types] == ["ok"]
        assert [r.folder for r in catalog.result_types] == ["Results/Fine"]
        assert len(catalog.problems) == 2


class TestFeatureNames:

    @pytest.mark.parametrize("identifier,name", [
        ("org.silastandard/core/SiLAService/v1", "SiLAService"),
        ("de.bicocca.labos/core/LabOSLibraryProvider/v1", "LabOSLibraryProvider"),
        ("org.silastandard/instruments/WorkflowAPI/v1", "WorkflowAPI"),
        ("WorkflowAPI", "WorkflowAPI"),
    ])
    def test_bare_name(self, identifier, name):
        assert feature_name(identifier) == name

    def test_infrastructure(self):
        assert is_infrastructure_feature("org.silastandard/core/SiLAService/v1")
        assert not is_infrastructure_feature("org.silastandard/instruments/WorkflowAPI/v1")


class TestFolders:

    def _dirs(self, tmp_path):
        return dict(library_dir=tmp_path / "Library", results_dir=tmp_path / "Results")

    def test_declared_folders_are_created_once(self, tmp_path):
        catalog = parse_catalog({"library_types": [{"hint": "stain", "folder": "Library/Stains"}],
                                 "result_types": [{"folder": "Results/Cells"}]})
        created, refused = ensure_folders(catalog, tmp_path, **self._dirs(tmp_path))
        assert {p.name for p in created} == {"Stains", "Cells"} and refused == []
        assert ensure_folders(catalog, tmp_path, **self._dirs(tmp_path)) == ([], [])

    def test_a_results_folder_cannot_land_in_library_or_elsewhere(self, tmp_path):
        catalog = parse_catalog({"result_types": [{"folder": "Library/Sneaky"}, {"folder": "src/x"}]})
        created, refused = ensure_folders(catalog, tmp_path, **self._dirs(tmp_path))
        assert created == [] and sorted(refused) == ["Library/Sneaky", "src/x"]
        assert not (tmp_path / "src").exists()

    def test_a_picker_backed_by_values_needs_no_folder(self, tmp_path):
        catalog = parse_catalog({"library_types": [{"hint": "mode", "values": ["a"], "folder": "Library/X"}]})
        assert catalog.declared_folders() == []


class TestTheServerSide:
    """The provider every server runs creates its folders at startup."""

    @pytest.fixture
    def provider(self):
        path = V1_DIR / "SiLA2" / "_NewInstrumentTemplate" / "src" / "library_provider_impl.py"
        source = path.read_text(encoding="utf-8")
        # Only the pure helpers: importing the module needs the generated package.
        start = source.index("_ALLOWED_TOPS")
        end = source.index("class LibraryProviderImpl")
        namespace = {"__name__": "provider_helpers"}
        exec("import json, logging, os\nfrom pathlib import Path, PurePosixPath\n"
             "logger = logging.getLogger('t')\n" + source[start:end], namespace)
        return SimpleNamespace(**namespace)

    def test_creates_what_the_catalog_declares(self, provider, tmp_path):
        created = provider.ensure_declared_folders(
            {"result_types": [{"folder": "Results/Cells"}],
             "library_types": [{"hint": "h", "folder": "Library/Stains"}]}, tmp_path)
        assert (tmp_path / "Results" / "Cells").is_dir()
        assert (tmp_path / "Library" / "Stains").is_dir()
        assert len(created) == 2

    @pytest.mark.parametrize("folder", ["../outside", "src/x", "/abs", "Results/../../x"])
    def test_refuses_to_create_outside_library_and_results(self, provider, tmp_path, folder):
        work = tmp_path / "root"
        work.mkdir()
        assert provider.ensure_declared_folders({"result_types": [{"folder": folder}]}, work) == []
        assert sorted(p.name for p in tmp_path.iterdir()) == ["root"]
        assert list(work.iterdir()) == []

    def test_every_server_runs_the_same_provider(self):
        template = (V1_DIR / "SiLA2" / "_NewInstrumentTemplate" / "src"
                    / "library_provider_impl.py").read_bytes().replace(b"\r\n", b"\n")
        for server in (V1_DIR / "SiLA2").glob("*SiLA2Server"):
            copy = server / "src" / "library_provider_impl.py"
            assert copy.is_file(), f"{server.name} does not declare anything about itself"
            assert copy.read_bytes().replace(b"\r\n", b"\n") == template, f"{server.name} has drifted"

    def test_every_shipped_catalog_is_clean(self):
        for server in (V1_DIR / "SiLA2").glob("*SiLA2Server"):
            catalog = parse_catalog((server / "library_catalog.json").read_text(encoding="utf-8"))
            assert catalog.problems == [], (server.name, catalog.problems)


def _server(name, catalog, commands=(), port=50099):
    server = MagicMock()
    server.name, server.host, server.port = name, "localhost", port
    server.server_type, server.server_online = "Imager", True
    server.catalog = parse_catalog(catalog)
    server.library_catalog = server.catalog.library_types
    server.features = []
    server.get_all_commands.return_value = list(commands)
    return server


class TestTheOrchestratorSide:

    @pytest.fixture
    def core(self, tmp_path):
        return LabCore(tmp_path)

    def test_an_instrument_carries_its_display_and_results(self, core):
        instrument = core._server_to_instrument(_server("Cell Imager", {
            "display": {"icon": "bi-camera", "color": "warning"},
            "result_types": [{"folder": "Results/Cells", "display_name": "Cell images"}]}), {}, {})
        data = instrument.to_dict()
        assert data["display"] == {"icon": "bi-camera", "color": "warning"}
        assert data["results"][0]["folder"] == "Results/Cells"

    def test_infrastructure_commands_are_not_offered(self, core):
        command = lambda i: SimpleNamespace(identifier=i, display_name=i, description="",
                                            observable=False, parameters=[])
        instrument = core._server_to_instrument(_server("Cell Imager", {}, commands=[
            ("org.silastandard/core/SiLAService/v1", "GetFeatureDefinition", command("GetFeatureDefinition")),
            ("org.example/instruments/Imaging/v1", "Snap", command("Snap")),
        ]), {}, {})
        assert [c.id for c in instrument.commands] == ["Snap"]

    def test_registration_creates_the_declared_folders(self, core, tmp_path):
        instrument = core._server_to_instrument(_server("Cell Imager", {
            "result_types": [{"folder": "Results/Cells"}]}), {}, {})
        core.ensure_declared_folders(instrument)
        assert (tmp_path / "Results" / "Cells").is_dir()

    def test_results_lists_declared_folders_and_whatever_else_exists(self, core, tmp_path):
        imager = core._server_to_instrument(_server("Cell Imager", {
            "result_types": [{"folder": "Results/Cells", "display_name": "Cell images"}]}), {}, {})
        core._instruments = {imager.id: imager}
        core.ensure_declared_folders(imager)
        (tmp_path / "Results" / "Cells" / "a.tif").write_bytes(b"x")
        (tmp_path / "Results" / "Stray").mkdir()
        (tmp_path / "Results" / "runs").mkdir()

        folders = {f["name"]: f for f in core.list_result_folders()}
        assert folders["Cells"]["declared_by"] == ["Cell Imager"]
        assert folders["Cells"]["file_count"] == 1
        assert folders["Stray"]["declared"] is False, "undeclared data is still shown"
        assert "runs" not in folders, "LabOS's own bookkeeping is not instrument data"


class TestNoInstrumentNamesInTheCore:
    """The claim the architecture makes, held as a test.

    `"tecan" in name`, `"opentrons" in name`, `"manual" in name` and
    `"mobile" in name` decided plate tracking, parameter defaults, manual
    steps and task lists. Each is now declared by the server. This fails if
    one comes back.
    """

    FILES = [
        "src/workflow.py", "src/lab_core.py", "src/client.py", "src/discovery.py",
        "src/plate_tracking.py", "webapp/routes/workflows.py", "webapp/routes/instruments.py",
        "webapp/routes/operator.py",
    ]
    NAME_TEST = re.compile(
        r"""["'](tecan|opentrons|mobile|gofa|manual|incubator)["']\s+(not\s+)?in\b""", re.I)
    COMMAND_TEST = re.compile(
        r"""(action|command|command_id)(\.lower\(\))?\s*(==|in)\s*\(?\s*["'](executerecipe|runmeasurement|refreshtasks|requestoperatortask)["']""",
        re.I)

    @staticmethod
    def _code_lines(relative):
        """Source without comments and docstrings, which may tell the history."""
        source = (V1_DIR / relative).read_text(encoding="utf-8-sig")
        source = re.sub(r'"""[\s\S]*?"""', "", source)
        return [line for line in source.splitlines() if not line.strip().startswith("#")]

    @pytest.mark.parametrize("relative", FILES)
    def test_no_substring_match_on_an_instrument_name(self, relative):
        offenders = [line.strip() for line in self._code_lines(relative) if self.NAME_TEST.search(line)]
        assert offenders == [], f"{relative}: {offenders}"

    @pytest.mark.parametrize("relative", FILES)
    def test_no_branch_on_a_particular_instrument_command(self, relative):
        offenders = [line.strip() for line in self._code_lines(relative) if self.COMMAND_TEST.search(line)]
        assert offenders == [], f"{relative}: {offenders}"
