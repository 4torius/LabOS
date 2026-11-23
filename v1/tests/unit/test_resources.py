"""The resource model: what a step occupies, and who gets to say so.

The case that drives all of this is the mobile robot. Its tasks are named
`place_on_OT`, `pick_from_tecan` — a step **on the robot** that physically
occupies **somebody else's deck or tray**. Nothing in such a step mentions
"opentrons", so exclusion keyed on the step's instrument would let the Opentrons
pipette into a deck with an arm inside it while working exactly as designed.

The tests are grouped by the property being defended, because each one exists
against a specific way of getting this wrong:

* parsing is forgiving (the document arrives over the network from another
  machine) but an unresolved claim is not (it fails closed);
* `provides` has one owner, so a typo cannot silently disable exclusion;
* an undeclared server serialises, so adding the model cannot *introduce*
  the hazard it removes;
* and claims acquired as a set cannot deadlock.
"""
import asyncio
import sys
from pathlib import Path

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
if str(V1_DIR) not in sys.path:
    sys.path.insert(0, str(V1_DIR))

from src.resources import (  # noqa: E402
    UNDECLARED,
    CommandResources,
    ResourceLedger,
    build_topology,
    instrument_resource,
    parse_config,
    parse_declaration,
)

# The real shape, from the servers this lab actually runs.
OPENTRONS = {
    "provides": ["instrument:opentrons", "deck:opentrons"],
    "commands": {
        "ExecuteRecipe": {"claims": ["instrument:opentrons", "deck:opentrons"]},
        "CaptureImage": {"claims": ["instrument:opentrons"]},
    },
}
MOBILE = {
    "provides": ["instrument:mobile"],
    "commands": {
        "ExecuteTask": {
            "claims": ["instrument:mobile"],
            "by_parameter": {"TaskName": {
                "place_on_OT": ["deck:opentrons"],
                "pick_from_tecan": ["tray:tecan"],
                "go_to_Opentrons": [],
            }},
        },
    },
}
TECAN = {
    "provides": ["instrument:tecan", "tray:tecan"],
    "commands": {
        "RunMeasurement": {"claims": ["instrument:tecan", "tray:tecan"]},
        "PlateIn": {"claims": ["instrument:tecan", "tray:tecan"]},
    },
}


def _topology(**servers):
    return build_topology({k: parse_declaration(v) for k, v in servers.items()})


# ── Parsing is forgiving, because the document crosses a network ──────────────

class TestParsingNeverRaises:

    @pytest.mark.parametrize("raw", [
        "not json at all", 42, [], {"provides": "deck:opentrons"},
        {"commands": "nope"}, {"commands": {"X": "nope"}},
        {"commands": {"X": {"claims": [{"deep": "wrong"}]}}},
        {"commands": {"X": {"by_parameter": {"P": "nope"}}}},
    ])
    def test_a_malformed_document_costs_the_entry_not_the_process(self, raw):
        declaration = parse_declaration(raw, source="test")
        assert isinstance(declaration.provides, tuple)

    def test_a_bad_resource_id_is_dropped_and_named(self):
        declaration = parse_declaration({"provides": ["deck:opentrons", "NO COLON", "Deck:Bad"]})
        assert declaration.provides == ("deck:opentrons",)
        assert any("NO COLON" in p for p in declaration.problems)

    def test_a_missing_section_is_not_a_declaration(self):
        # Distinct from an empty one: "I said nothing" and "I occupy nothing
        # beyond myself" have to behave differently, see the undeclared tests.
        assert parse_declaration(None).declared is False
        assert parse_declaration({}).declared is True


class TestWhatACommandOccupies:

    def test_the_claim_follows_the_task_not_the_command(self):
        spec = parse_declaration(MOBILE).commands["ExecuteTask"]
        assert spec.resolve({"TaskName": "place_on_OT"}) == {
            "instrument:mobile", "deck:opentrons"}
        assert spec.resolve({"TaskName": "pick_from_tecan"}) == {
            "instrument:mobile", "tray:tecan"}

    def test_a_task_that_occupies_nothing_else_claims_only_the_robot(self):
        spec = parse_declaration(MOBILE).commands["ExecuteTask"]
        assert spec.resolve({"TaskName": "go_to_Opentrons"}) == {"instrument:mobile"}

    def test_an_unknown_task_falls_back_to_the_unconditional_claims(self):
        spec = parse_declaration(MOBILE).commands["ExecuteTask"]
        assert spec.resolve({"TaskName": "taught_last_tuesday"}) == {"instrument:mobile"}

    def test_a_misspelled_parameter_key_still_resolves(self):
        # Workflow JSON is written by hand; `taskname` for `TaskName` is common.
        spec = parse_declaration(MOBILE).commands["ExecuteTask"]
        assert spec.resolve({"taskname": "place_on_OT"}) == {
            "instrument:mobile", "deck:opentrons"}

    def test_resolve_all_sees_every_branch(self):
        spec = parse_declaration(MOBILE).commands["ExecuteTask"]
        assert spec.resolve_all() == {
            "instrument:mobile", "deck:opentrons", "tray:tecan"}


# ── The case the whole model exists for ───────────────────────────────────────

class TestTheCrossInstrumentHazard:

    def test_the_robot_in_the_deck_conflicts_with_the_recipe(self):
        topology = _topology(opentrons=OPENTRONS, mobile=MOBILE, tecan=TECAN)
        arm_in_deck = topology.claims_for("mobile", "ExecuteTask", {"TaskName": "place_on_OT"})
        pipetting = topology.claims_for("opentrons", "ExecuteRecipe")
        assert topology.conflicts(arm_in_deck, pipetting), (
            "an arm inside the deck must exclude a recipe on that deck"
        )

    def test_an_instrument_level_lock_would_have_missed_it(self):
        # The point of the model in one assertion: the two steps share no
        # instrument, so nothing keyed on the instrument could have caught it.
        topology = _topology(opentrons=OPENTRONS, mobile=MOBILE)
        assert "instrument:mobile" != "instrument:opentrons"
        arm = topology.claims_for("mobile", "ExecuteTask", {"TaskName": "place_on_OT"})
        recipe = topology.claims_for("opentrons", "ExecuteRecipe")
        assert arm & recipe == {"deck:opentrons"}

    def test_the_robot_elsewhere_does_not_block_the_recipe(self):
        topology = _topology(opentrons=OPENTRONS, mobile=MOBILE, tecan=TECAN)
        driving = topology.claims_for("mobile", "ExecuteTask", {"TaskName": "go_to_Opentrons"})
        pipetting = topology.claims_for("opentrons", "ExecuteRecipe")
        assert not topology.conflicts(driving, pipetting), (
            "driving to a station must not serialise against pipetting"
        )


# ── One owner per resource ────────────────────────────────────────────────────

class TestProvidesCatchesWhatClaimsAloneCouldNot:

    def test_a_typo_in_a_claim_is_an_error_not_a_silent_no_op(self):
        # The failure mode that made `provides` necessary: with claims only,
        # `deck:OT` against `deck:opentrons` yields no error and no exclusion.
        typo = {
            "provides": ["instrument:mobile"],
            "commands": {"ExecuteTask": {"claims": ["instrument:mobile", "deck:OT"]}},
        }
        topology = _topology(opentrons=OPENTRONS, mobile=typo)
        assert topology.unresolved, "an unprovided claim must fail closed"
        assert "deck:OT" in str(topology.unresolved[0])
        assert topology.to_dict()["ok"] is False

    def test_a_claim_hidden_behind_one_task_value_is_still_checked(self):
        typo = {
            "provides": ["instrument:mobile"],
            "commands": {"ExecuteTask": {
                "by_parameter": {"TaskName": {"place_on_OT": ["deck:tipo"]}}}},
        }
        topology = _topology(opentrons=OPENTRONS, mobile=typo)
        assert [u.resource for u in topology.unresolved] == ["deck:tipo"]

    def test_two_owners_for_one_resource_is_reported(self):
        greedy = {"provides": ["deck:opentrons"], "commands": {}}
        topology = _topology(opentrons=OPENTRONS, impostor=greedy)
        assert any("one owner per resource" in p for p in topology.problems)

    def test_a_correct_topology_is_clean(self):
        topology = _topology(opentrons=OPENTRONS, mobile=MOBILE, tecan=TECAN)
        assert topology.unresolved == []
        assert topology.problems == []
        assert topology.to_dict()["ok"] is True

    def test_an_instrument_always_provides_itself(self):
        topology = _topology(bare={})
        assert topology.providers[instrument_resource("bare")] == "bare"
        assert topology.unresolved == []


# ── Adding the model must not introduce the hazard it removes ─────────────────

class TestConcurrencyIsEarnedByDeclaring:

    def test_a_server_that_declared_nothing_serialises_against_its_peers(self):
        # Today's state: no catalog carries a `resources` section. If undeclared
        # steps carried only their own instrument claim they would all be
        # disjoint, and all 19 steps of Test_Bicocca would dispatch at once.
        topology = _topology(opentrons=None, tecan=None, mobile=None)
        a = topology.claims_for("opentrons", "ExecuteRecipe")
        b = topology.claims_for("tecan", "RunMeasurement")
        assert UNDECLARED in a and UNDECLARED in b
        assert topology.conflicts(a, b), "undeclared steps must not run concurrently"

    def test_an_explicitly_empty_declaration_is_believed(self):
        topology = _topology(thermo={"provides": [], "commands": {}}, tecan=TECAN)
        a = topology.claims_for("thermo", "GetTemperature")
        b = topology.claims_for("tecan", "RunMeasurement")
        assert UNDECLARED not in a
        assert not topology.conflicts(a, b)

    def test_an_undeclared_server_is_still_serialised_against_itself(self):
        topology = _topology(mobile=None)
        a = topology.claims_for("mobile", "ExecuteTask", {"TaskName": "x"})
        b = topology.claims_for("mobile", "ExecuteTask", {"TaskName": "y"})
        assert topology.conflicts(a, b)

    def test_declared_peers_run_concurrently(self):
        topology = _topology(opentrons=OPENTRONS, tecan=TECAN)
        assert not topology.conflicts(
            topology.claims_for("opentrons", "CaptureImage"),
            topology.claims_for("tecan", "RunMeasurement"),
        )

    def test_the_undeclared_instruments_are_listed_for_the_panel(self):
        topology = _topology(opentrons=OPENTRONS, mobile=None, tecan=None)
        assert topology.undeclared == ("mobile", "tecan")


# ── The config layers ─────────────────────────────────────────────────────────

class TestTheConfigSpeaksForServersThatCannot:

    def test_a_vendor_server_gets_its_resources_from_the_config(self):
        config = parse_config({"declare": {"tecan": TECAN}})
        topology = build_topology(
            {"tecan": parse_declaration(None), "mobile": parse_declaration(MOBILE)},
            config,
        )
        assert topology.providers["tray:tecan"] == "tecan"
        assert topology.conflicts(
            topology.claims_for("tecan", "PlateIn"),
            topology.claims_for("mobile", "ExecuteTask", {"TaskName": "pick_from_tecan"}),
        )
        assert UNDECLARED not in topology.claims_for("tecan", "PlateIn")

    def test_the_server_wins_over_the_config_and_the_clash_is_reported(self):
        # Rule 1: the instrument is the authority on itself. Central config
        # cannot contradict it, or plug-and-play is gone.
        config = parse_config({"declare": {"opentrons": {
            "provides": ["deck:elsewhere"],
            "commands": {"ExecuteRecipe": {"claims": ["deck:elsewhere"]}},
        }}})
        topology = build_topology({"opentrons": parse_declaration(OPENTRONS)}, config)
        assert topology.claims_for("opentrons", "ExecuteRecipe") == {
            "instrument:opentrons", "deck:opentrons"}
        assert any("is ignored" in p for p in topology.problems)

    def test_an_alias_covers_a_swapped_workstation(self):
        taught = {
            "provides": ["instrument:mobile"],
            "commands": {"ExecuteTask": {
                "claims": ["instrument:mobile"],
                "by_parameter": {"TaskName": {"place_on_station_1": ["deck:station_1"]}}}},
        }
        config = parse_config({"aliases": {"station_1": "deck:opentrons"}})
        topology = build_topology(
            {"opentrons": parse_declaration(OPENTRONS), "mobile": parse_declaration(taught)},
            config,
        )
        assert topology.unresolved == []
        assert topology.conflicts(
            topology.claims_for("mobile", "ExecuteTask", {"TaskName": "place_on_station_1"}),
            topology.claims_for("opentrons", "ExecuteRecipe"),
        )

    def test_an_alias_to_nowhere_is_reported(self):
        config = parse_config({"aliases": {"station_9": "deck:ghost"}})
        topology = build_topology({"opentrons": parse_declaration(OPENTRONS)}, config)
        assert any("station_9" in p and "no instrument provides" in p
                   for p in topology.problems)

    def test_an_alias_chain_is_refused_rather_than_followed(self):
        config = parse_config({"aliases": {"a": "deck:b", "b": "deck:opentrons"}})
        assert any("chains are not resolved" in p for p in config.problems)

    @pytest.mark.parametrize("raw", [
        "nope", 7, {"declare": "nope"}, {"aliases": "nope"},
        {"aliases": {"has:colon": "deck:opentrons"}},
        {"aliases": {"ok": "not a resource id"}},
    ])
    def test_a_malformed_config_section_never_raises(self, raw):
        assert isinstance(parse_config(raw).aliases, dict)


# ── The ledger ────────────────────────────────────────────────────────────────

class TestTheLedger:

    @pytest.mark.asyncio
    async def test_a_second_claimant_waits_then_proceeds(self):
        ledger = ResourceLedger()
        await ledger.acquire("step-1", {"deck:opentrons", "instrument:opentrons"})

        order = []

        async def contender():
            await ledger.acquire("step-2", {"deck:opentrons"})
            order.append("step-2 got it")

        task = asyncio.create_task(contender())
        await asyncio.sleep(0.05)
        assert order == [], "step-2 took a resource step-1 was holding"
        order.append("step-1 released")
        await ledger.release("step-1")
        await asyncio.wait_for(task, timeout=1)
        assert order == ["step-1 released", "step-2 got it"]

    @pytest.mark.asyncio
    async def test_disjoint_claims_do_not_wait_for_each_other(self):
        ledger = ResourceLedger()
        await ledger.acquire("reading", {"instrument:tecan", "tray:tecan"})
        await asyncio.wait_for(
            ledger.acquire("imaging", {"instrument:opentrons"}), timeout=1)
        assert ledger.holder_of("tray:tecan") == "reading"
        assert ledger.holder_of("instrument:opentrons") == "imaging"

    @pytest.mark.asyncio
    async def test_claims_are_all_or_nothing(self):
        # The property that makes step-scoped claims deadlock-free: a step never
        # holds one resource while waiting for another.
        ledger = ResourceLedger()
        await ledger.acquire("holder", {"tray:tecan"})
        task = asyncio.create_task(
            ledger.acquire("both", {"deck:opentrons", "tray:tecan"}))
        await asyncio.sleep(0.05)
        assert not task.done()
        assert ledger.holder_of("deck:opentrons") is None, (
            "a blocked step must not be holding part of its set"
        )
        await ledger.release("holder")
        await asyncio.wait_for(task, timeout=1)
        assert ledger.holder_of("deck:opentrons") == "both"

    @pytest.mark.asyncio
    async def test_two_steps_crossing_resources_both_finish(self):
        # A → {a, b} and B → {b, a} in opposite order is the classic deadlock.
        # Atomic set acquisition makes it impossible.
        ledger = ResourceLedger()

        async def step(name, claims):
            await ledger.acquire(name, claims)
            await asyncio.sleep(0.01)
            await ledger.release(name)
            return name

        done = await asyncio.wait_for(asyncio.gather(
            step("A", ["deck:opentrons", "tray:tecan"]),
            step("B", ["tray:tecan", "deck:opentrons"]),
        ), timeout=2)
        assert sorted(done) == ["A", "B"]
        assert ledger.held == {}

    @pytest.mark.asyncio
    async def test_max_concurrency_one_forces_strict_serial_execution(self):
        ledger = ResourceLedger(max_concurrency=1)
        await ledger.acquire("first", {"instrument:tecan"})
        task = asyncio.create_task(ledger.acquire("second", {"instrument:opentrons"}))
        await asyncio.sleep(0.05)
        assert not task.done(), "max_concurrency=1 must admit one step at a time"
        await ledger.release("first")
        await asyncio.wait_for(task, timeout=1)

    @pytest.mark.asyncio
    async def test_releasing_an_unknown_holder_is_harmless(self):
        ledger = ResourceLedger()
        await ledger.release("never-acquired")
        assert ledger.held == {}

    @pytest.mark.asyncio
    async def test_a_cancelled_waiter_does_not_wedge_the_queue(self):
        ledger = ResourceLedger()
        await ledger.acquire("holder", {"tray:tecan"})
        doomed = asyncio.create_task(ledger.acquire("doomed", {"tray:tecan"}))
        await asyncio.sleep(0.05)
        doomed.cancel()
        with pytest.raises(asyncio.CancelledError):
            await doomed
        await ledger.release("holder")
        await asyncio.wait_for(ledger.acquire("later", {"tray:tecan"}), timeout=1)
        assert ledger.holder_of("tray:tecan") == "later"


class TestControlCommandsOccupyNothing:
    """A stop that queues behind the run it is stopping is not a stop.

    The implicit per-instrument claim is what makes this need saying: without
    an opt-out, `EmergencyStop` would claim `instrument:opentrons` and wait for
    the recipe it exists to interrupt.
    """

    DECL = {
        "provides": ["instrument:opentrons", "deck:opentrons"],
        "commands": {
            "ExecuteRecipe": {"claims": ["instrument:opentrons", "deck:opentrons"]},
            "EmergencyStop": {"control": True},
            "PauseRun": {"control": True},
        },
    }

    def test_a_control_command_claims_nothing_at_all(self):
        topology = _topology(opentrons=self.DECL)
        assert topology.claims_for("opentrons", "EmergencyStop") == frozenset()

    def test_a_control_command_never_conflicts_with_a_running_step(self):
        topology = _topology(opentrons=self.DECL)
        assert not topology.conflicts(
            topology.claims_for("opentrons", "EmergencyStop"),
            topology.claims_for("opentrons", "ExecuteRecipe"),
        )

    def test_an_ordinary_command_still_claims_its_instrument(self):
        topology = _topology(opentrons=self.DECL)
        assert "instrument:opentrons" in topology.claims_for("opentrons", "ExecuteRecipe")

    def test_control_plus_claims_is_contradictory_and_reported(self):
        declaration = parse_declaration({
            "provides": ["instrument:x"],
            "commands": {"Stop": {"control": True, "claims": ["instrument:x"]}},
        })
        assert declaration.commands["Stop"].claims == ()
        assert any("occupies nothing" in p for p in declaration.problems)

    def test_an_undeclared_command_on_a_declaring_server_is_not_control(self):
        # Silence is not permission: a command the server did not list still
        # claims its instrument.
        topology = _topology(opentrons=self.DECL)
        assert "instrument:opentrons" in topology.claims_for("opentrons", "SetLights")
