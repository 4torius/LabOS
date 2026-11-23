"""`labos.py resources` and the resource check inside `labos.py doctor`.

The resource model is invisible when it works, which is the reason to be able
to print it — and the reason these need a test: a broken `resources` command
would be noticed only when somebody needed it to diagnose something else.

The offline topology builder has one subtlety worth pinning. A catalog is found
by folder (`SiLA2/OpentronsSiLA2Server`) while every other part of the system
keys a server by its config key (`opentrons`). Without the mapping between
them each local server is counted twice — once declaring, once undeclared —
which reads as two owners for one resource and three spurious warnings.
"""
import sys
from pathlib import Path

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
if str(V1_DIR) not in sys.path:
    sys.path.insert(0, str(V1_DIR))

import labos  # noqa: E402


@pytest.fixture(scope="module")
def built():
    return labos._shipped_topology()


class TestTheOfflineTopology:

    def test_it_works_without_a_single_server_running(self, built):
        # doctor has to be usable before anything is started, so the builder
        # reads catalogs from disk rather than discovering.
        topology, problems = built
        assert topology.providers, "nothing was read from the catalogs on disk"
        assert problems == []

    def test_each_server_is_counted_once_under_its_config_key(self, built):
        topology, _ = built
        owners = set(topology.providers.values())
        assert owners == {"opentrons", "tecan", "manual_station", "mobile"}, (
            "a server was counted under its folder name as well as its config key"
        )

    def test_no_resource_has_two_owners(self, built):
        topology, _ = built
        assert topology.problems == []

    def test_every_claim_resolves(self, built):
        topology, _ = built
        assert topology.unresolved == []

    def test_nothing_is_left_undeclared(self, built):
        topology, _ = built
        assert topology.undeclared == ()

    def test_the_template_is_not_treated_as_a_deployed_server(self, built):
        topology, _ = built
        assert "instrument:newinstrument" not in topology.providers
        assert "_NewInstrumentTemplate" not in topology.providers.values()

    def test_the_shared_spaces_are_claimed_across_servers(self, built):
        # The relationship the whole model exists for, as the command prints it.
        topology, _ = built
        claimants = topology.claimants()
        assert any(c.startswith("mobile.") for c in claimants.get("deck:opentrons", []))
        assert any(c.startswith("mobile.") for c in claimants.get("tray:tecan", []))


class TestTheCommands:

    def test_resources_exits_zero_on_a_clean_lab(self, capsys):
        assert labos.cmd_resources(None) == 0
        printed = capsys.readouterr().out
        assert "deck:opentrons" in printed
        assert "owned by opentrons" in printed

    def test_resources_names_the_cross_instrument_claimant(self, capsys):
        labos.cmd_resources(None)
        printed = capsys.readouterr().out
        assert "also claimed by mobile.ExecuteTask" in printed

    def test_both_commands_are_registered(self):
        parser = labos.build_parser()
        for command in ("resources", "doctor"):
            assert parser.parse_args([command]).func is not None
