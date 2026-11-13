"""The mobile robot's catalog is written by task name and served by task id."""
import importlib.util
import json
import sys
from pathlib import Path

V1_DIR = Path(__file__).resolve().parents[2]
if str(V1_DIR) not in sys.path:
    sys.path.insert(0, str(V1_DIR))

from src.catalog import parse_catalog  # noqa: E402
from src.resources import topology_of  # noqa: E402

MOBILE_DIR = V1_DIR / "SiLA2" / "MobileSiLA2Server"


def _module():
    spec = importlib.util.spec_from_file_location(
        "mobile_task_resources", MOBILE_DIR / "src" / "task_resources.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CATALOG = json.loads((MOBILE_DIR / "library_catalog.json").read_text(encoding="utf-8"))
TASKS = [("id-place-ot", "place_on_OT"), ("id-go", "go_to_Tecan"),
         ("id-new", "place_on_incubator")]


def _topology(catalog):
    return topology_of([
        ("mobile", "GoFaGo Mobile Robot", parse_catalog(catalog).resources),
        ("opentrons", "OpentronsFlex", parse_catalog({"resources": {
            "provides": ["deck:opentrons", "slot:opentrons"]}}).resources),
        ("tecan", "TecanM200Pro", parse_catalog({"resources": {
            "provides": ["tray:tecan", "slot:tecan"]}}).resources),
    ])


class TestTheServerAddsTheIds:

    def test_a_declared_task_is_reachable_by_its_id(self):
        topology = _topology(_module().expand_task_names(CATALOG, TASKS))
        assert topology.unresolved == []
        claims = topology.claims_for("mobile", "ExecuteTask", {"TaskId": "id-place-ot"})
        assert "deck:opentrons" in claims
        assert topology.acquires_for(
            "mobile", "ExecuteTask", {"TaskId": "id-place-ot"}) == {"slot:opentrons"}

    def test_a_task_that_only_moves_the_robot_is_listed_and_occupies_nothing_else(self):
        topology = _topology(_module().expand_task_names(CATALOG, TASKS))
        assert topology.unlisted_for("mobile", "ExecuteTask", {"TaskId": "id-go"}) == []
        assert topology.claims_for(
            "mobile", "ExecuteTask", {"TaskId": "id-go"}) == {"instrument:mobile"}

    def test_a_task_nobody_declared_is_refused_not_assumed_harmless(self):
        topology = _topology(_module().expand_task_names(CATALOG, TASKS))
        assert topology.unlisted_for("mobile", "ExecuteTask", {"TaskId": "id-new"})

    def test_the_file_on_disk_is_not_modified(self):
        before = json.dumps(CATALOG, sort_keys=True)
        _module().expand_task_names(CATALOG, TASKS)
        assert json.dumps(CATALOG, sort_keys=True) == before

    def test_without_the_expansion_an_id_is_refused_rather_than_unguarded(self):
        # The robot PC still running a server that does not expand, or a
        # failed expansion: the step must not slip through with no claims.
        assert _topology(CATALOG).unlisted_for(
            "mobile", "ExecuteTask", {"TaskId": "id-place-ot"})


class TestTheProviderHook:

    def test_every_server_can_augment_what_it_serves(self):
        source = (MOBILE_DIR / "src" / "library_provider_impl.py").read_text(encoding="utf-8")
        assert "def __init__(self, parent_server, augment=None):" in source

    def test_the_robot_server_uses_it(self):
        source = (MOBILE_DIR / "main.py").read_text(encoding="utf-8")
        assert "augment=lambda catalog: expand_task_names(catalog, bridge_tasks(bridge))" in source

    def test_it_stays_within_python_3_8(self):
        # The robot PC runs ROS Noetic, which is Python 3.8.
        import ast
        tree = ast.parse((MOBILE_DIR / "src" / "task_resources.py").read_text(encoding="utf-8"))
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Match)]
        source = (MOBILE_DIR / "src" / "task_resources.py").read_text(encoding="utf-8")
        assert "list[" not in source and "dict[" not in source and " | None" not in source
