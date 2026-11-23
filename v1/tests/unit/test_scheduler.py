"""A step starts when its own dependencies are done - not when a wave is.

The executor used to run a workflow in topological waves and wait for a whole
wave before starting the next. With two independent chains that made them
advance in lockstep: plate B's second step could not start until plate A's
unrelated, slower first step had finished. These tests pin the scheduler that
replaced it, and the three ways the old code could still force one step at a
time or hang.
"""
import asyncio
import datetime
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
if str(V1_DIR) not in sys.path:
    sys.path.insert(0, str(V1_DIR))

from src.catalog import parse_catalog  # noqa: E402
from src.client import CommandResult  # noqa: E402
from src.resources import parse_config  # noqa: E402
from src.workflow import (  # noqa: E402
    PnPWorkflowExecutor,
    Workflow,
    WorkflowContext,
    WorkflowStep,
    halt_running_workflows,
)

_ISOLATED = Path(__file__).resolve().parent / "_no_such_config_dir"


def _declares(name):
    return {"resources": {"provides": [f"instrument:{name}"],
                          "commands": {"Do": {"claims": [f"instrument:{name}"]}}}}


def _step(number, instrument, action="Do", parameters=None, depends_on=None, **extra):
    return WorkflowStep(
        step_number=number, instrument=instrument, action=action,
        parameters=dict(parameters or {}), depends_on=list(depends_on or []),
        retry_count=0, **extra)


def _executor(servers, durations=None, fail=()):
    """servers: key -> (announced name, catalog). Records start/end per step."""
    registry = MagicMock()
    registry.servers = {
        key: SimpleNamespace(name=name, catalog=parse_catalog(catalog))
        for key, (name, catalog) in servers.items()
    }
    registry.list_servers = lambda: list(registry.servers.values())
    registry.get_server_by_name = lambda name: registry.servers.get(name)
    events = []

    async def _execute(instrument, action, parameters, timeout=None):
        tag = parameters.get("Tag", instrument)
        events.append(("start", tag))
        await asyncio.sleep((durations or {}).get(tag, 0.02))
        events.append(("end", tag))
        if tag in fail:
            return CommandResult(success=False, error="it broke")
        return CommandResult(success=True, data={})

    registry.execute = AsyncMock(side_effect=_execute)
    executor = PnPWorkflowExecutor(registry, base_dir=_ISOLATED)
    executor._error_config = executor._error_config.__class__(
        retry_strategy="none", base_delay_seconds=0.0, max_delay_seconds=0.0,
        max_retries=0, enable_human_intervention=False)
    executor._max_concurrency = None
    return executor, events


async def _run(executor, steps, **kwargs):
    workflow = Workflow(name="W", steps=steps)
    workflow.start_time = datetime.datetime.now()
    return await executor._execute_waves(workflow, WorkflowContext(), **kwargs)


FOUR = {key: (key, _declares(key)) for key in ("a", "b", "c", "d")}


class TestTwoChainsDoNotWaitForEachOther:

    @pytest.mark.asyncio
    async def test_a_chain_is_not_held_back_by_a_slow_step_in_the_other(self):
        # Chain 1: a (slow) -> b.   Chain 2: c (fast) -> d.
        # In waves, d waited for a. It must not.
        executor, events = _executor(FOUR, durations={"a": 0.3})
        done = await _run(executor, [
            _step(1, "a"), _step(2, "b", depends_on=[1]),
            _step(3, "c"), _step(4, "d", depends_on=[3]),
        ])
        assert done[0] == 4
        assert events.index(("start", "d")) < events.index(("end", "a")), (
            "the second chain waited for an unrelated step of the first")

    @pytest.mark.asyncio
    async def test_a_dependent_still_waits_for_its_own_dependency(self):
        executor, events = _executor(FOUR, durations={"a": 0.1})
        await _run(executor, [_step(1, "a"), _step(2, "b", depends_on=[1])])
        assert events.index(("end", "a")) < events.index(("start", "b"))

    @pytest.mark.asyncio
    async def test_contending_steps_keep_the_order_they_were_written_in(self):
        executor, events = _executor({"a": ("a", _declares("a"))})
        await _run(executor, [
            _step(n, "a", parameters={"Tag": f"s{n}"}) for n in (1, 2, 3, 4)])
        assert [tag for kind, tag in events if kind == "start"] == ["s1", "s2", "s3", "s4"]

    @pytest.mark.asyncio
    async def test_a_limit_of_one_is_strictly_one_at_a_time(self):
        executor, events = _executor(FOUR)
        await _run(executor, [_step(1, "a"), _step(2, "b"), _step(3, "c")],
                   max_concurrency=1)
        kinds = [kind for kind, _ in events]
        assert kinds == ["start", "end"] * 3


class TestAfterAHaltingFailure:

    @pytest.mark.asyncio
    async def test_nothing_new_is_started(self):
        # b fails at once; c is queued behind the slow a on the same
        # instrument. By the time a's instrument is free the run has halted,
        # so c must not be sent.
        servers = {"a": ("a", _declares("a")), "b": ("b", _declares("b"))}
        executor, events = _executor(servers, durations={"first": 0.2}, fail={"b"})
        completed, failed, skipped, _, errors = await _run(executor, [
            _step(1, "a", parameters={"Tag": "first"}),
            _step(2, "b"),
            _step(3, "a", parameters={"Tag": "queued"}),
        ])
        assert ("start", "queued") not in events
        assert (completed, failed, skipped) == (1, 1, 1)
        assert any("Step 3 skipped: workflow halted after step 2" in e for e in errors)

    @pytest.mark.asyncio
    async def test_what_is_already_moving_is_left_to_finish(self):
        servers = {"a": ("a", _declares("a")), "b": ("b", _declares("b"))}
        executor, events = _executor(servers, durations={"a": 0.2}, fail={"b"})
        completed, failed, _, _, _ = await _run(executor, [_step(1, "a"), _step(2, "b")])
        assert ("end", "a") in events and (completed, failed) == (1, 1)


class TestTheRetiredFlagIsNotTheMasterSwitch:
    """`workflow.parallel_execution` defaults to False when it is absent.

    Callers used to read it and pass it on, which forced one step at a time on
    every run - whatever `max_concurrency` said.
    """

    @pytest.mark.asyncio
    async def test_execute_overlaps_independent_steps_by_default(self):
        executor, events = _executor(FOUR, durations={"a": 0.1, "b": 0.1})
        result = await executor.execute(
            Workflow(name="W", steps=[_step(1, "a"), _step(2, "b")]), validate=False)
        assert result.success
        assert events.index(("start", "b")) < events.index(("end", "a"))

    @pytest.mark.asyncio
    async def test_a_cap_given_for_one_run_is_honoured(self):
        executor, events = _executor(FOUR, durations={"a": 0.05, "b": 0.05})
        await executor.execute(
            Workflow(name="W", steps=[_step(1, "a"), _step(2, "b")]),
            validate=False, max_concurrency=1)
        assert events.index(("end", "a")) < events.index(("start", "b"))

    def test_no_caller_passes_the_retired_flag_from_the_settings(self):
        for relative in ("webapp/routes/workflows.py", "src/lab_core.py", "labos.py"):
            source = (V1_DIR / relative).read_text(encoding="utf-8-sig")
            assert ").workflow.parallel_execution" not in source, relative
            assert "parallel=args.parallel" not in source, relative

    def test_max_concurrency_alone_is_the_cap(self):
        from src.settings import build_settings
        settings = build_settings({"workflow": {"max_concurrency": 3}}, V1_DIR, None)
        assert settings.workflow.max_concurrency == 3

    def test_the_shipped_config_still_runs_one_step_at_a_time(self):
        from src.settings import get_settings
        assert get_settings(V1_DIR).workflow.max_concurrency == 1


class TestTheTopologyIsKeyedLikeAWorkflowIsWritten:
    """A workflow writes `mobile`; the robot announces `GoFaGo Mobile Robot`.

    The executor keyed its topology by the announced name, so
    `resources.declare.mobile` matched nothing and the robot's claims never
    applied - while the Settings page, keyed by `mobile`, showed them applied.
    """

    SERVERS = {
        "mobile": ("GoFaGo Mobile Robot", {}),   # declares nothing itself
        "opentrons": ("OpentronsFlex", {"resources": {
            "provides": ["instrument:opentrons", "deck:opentrons"],
            "commands": {"Do": {"claims": ["instrument:opentrons", "deck:opentrons"]}}}}),
    }
    CONFIG = {"declare": {"mobile": {
        "provides": ["instrument:mobile"],
        "commands": {"Do": {"claims": ["instrument:mobile", "deck:opentrons"]}}}}}

    def _with_config(self, monkeypatch, config):
        import src.workflow as workflow_module
        real = workflow_module.get_settings

        def _settings(base_dir=None, **kwargs):
            settings = real(base_dir, **kwargs)
            return SimpleNamespace(resources=parse_config(config),
                                   error_handling=settings.error_handling,
                                   workflow=settings.workflow)
        monkeypatch.setattr(workflow_module, "get_settings", _settings)

    def test_a_declaration_in_the_config_reaches_the_executor(self, monkeypatch):
        self._with_config(monkeypatch, self.CONFIG)
        executor, _ = _executor(self.SERVERS)
        topology = executor._topology()
        assert topology.problems == []
        assert "deck:opentrons" in executor._claims_of(_step(1, "mobile"), topology)

    @pytest.mark.asyncio
    async def test_so_the_arm_and_the_deck_never_overlap(self, monkeypatch):
        self._with_config(monkeypatch, self.CONFIG)
        executor, events = _executor(self.SERVERS, durations={"mobile": 0.1})
        await _run(executor, [_step(1, "mobile"), _step(2, "opentrons")])
        assert events.index(("end", "mobile")) < events.index(("start", "opentrons"))

    def test_a_declare_entry_written_against_the_announced_name_still_applies(
            self, monkeypatch):
        self._with_config(monkeypatch, {"declare": {
            "GoFaGo Mobile Robot": self.CONFIG["declare"]["mobile"]}})
        executor, _ = _executor(self.SERVERS)
        topology = executor._topology()
        assert "deck:opentrons" in executor._claims_of(_step(1, "mobile"), topology)

    def test_a_declare_entry_for_no_instrument_is_said_out_loud(self, monkeypatch):
        self._with_config(monkeypatch, {"declare": {
            "moblie": self.CONFIG["declare"]["mobile"]}})
        executor, _ = _executor(self.SERVERS)
        problems = executor._topology().problems
        assert any("declare.moblie matches no discovered instrument" in p
                   and "NOT applied" in p for p in problems)

    def test_the_page_and_the_executor_build_it_the_same_way(self):
        source = (V1_DIR / "webapp/routes/instruments.py").read_text(encoding="utf-8-sig")
        assert "topology_of(" in source and "build_topology(" not in source


class TestAnEmergencyStopReachesTheRun:

    @pytest.mark.asyncio
    async def test_a_running_workflow_is_halted_and_nothing_is_re_sent(self):
        executor, events = _executor(FOUR, durations={"a": 0.3})
        executor._retry_on_failure = True
        executor._error_config = executor._error_config.__class__(
            retry_strategy="constant", base_delay_seconds=0.0, max_delay_seconds=0.0,
            max_retries=3, enable_human_intervention=False)
        steps = [_step(1, "a"), _step(2, "b", depends_on=[1])]
        steps[0].retry_count = None
        workflow = Workflow(name="W", steps=steps)
        run = asyncio.ensure_future(executor.execute(workflow, validate=False))
        await asyncio.sleep(0.05)

        halted = halt_running_workflows()
        assert halted == [executor]
        for each in halted:
            each.cancel_inflight()
        result = await asyncio.wait_for(run, timeout=5)

        assert not result.success
        assert [tag for kind, tag in events if kind == "start"] == ["a"], (
            "the stopped step was sent again, or the next one was started")

    @pytest.mark.asyncio
    async def test_an_executor_is_only_listed_while_it_runs(self):
        executor, _ = _executor(FOUR)
        await executor.execute(Workflow(name="W", steps=[_step(1, "a")]), validate=False)
        assert executor not in halt_running_workflows()
