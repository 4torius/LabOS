"""A step's own TimeoutSeconds, and whether it means anything.

It meant one thing from `labos run` and nothing at all from the web interface.
The executor passes `step_timeout` only in the branch that runs when no custom
step executor is installed, and the webapp always installs one - so the field
worked on the path nobody uses and was silently ignored on the path everybody
does.

The fix enforces an *explicit* value on both paths and deliberately does not
push the 600s fallback onto the webapp path: doing that would start killing
every long step that has always run unbounded, a two-hour recipe among them.

Two neighbouring fields were suspected of the same fault and are not guilty.
`OnFailure` is read by `_execute_waves` and `RetryCount` by `_run_one`, neither
of which is behind that branch, so both have always worked however a workflow
is started. The tests below pin that down so they are not "cleaned up" on the
strength of a wrong summary.
"""
import asyncio
import re
import sys
from pathlib import Path

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V1_DIR))

from src.settings import normalize_timeout  # noqa: E402
from src.workflow import Workflow, WorkflowStep  # noqa: E402

SOURCE = (V1_DIR / "src" / "workflow.py").read_text(encoding="utf-8-sig")


class TestTheFieldIsParsed:

    def _step(self, **overrides):
        data = {"WorkflowName": "w", "Steps": [dict(
            {"StepNumber": 1, "Instrument": "x", "Action": "y"}, **overrides)]}
        return Workflow.from_dict(data).steps[0]

    def test_timeout_seconds_survives_the_round_trip(self):
        assert self._step(TimeoutSeconds=90).timeout_seconds == 90

    def test_zero_is_kept_and_means_unlimited(self):
        """`or` would lose it: 0 is falsy."""
        step = self._step(TimeoutSeconds=0)
        assert step.timeout_seconds == 0
        assert normalize_timeout(step.timeout_seconds) is None

    def test_absent_stays_absent(self):
        assert self._step().timeout_seconds is None

    def test_on_failure_and_retry_count_round_trip_too(self):
        step = self._step(OnFailure="continue", RetryCount=1)
        assert step.on_failure == "continue"
        assert step.retry_count == 1


class TestItIsEnforcedOnBothPaths:

    def _dispatch(self):
        start = SOURCE.index("        async def _dispatch():")
        following = re.compile(r"^        \S", re.M).search(SOURCE, start + 1)
        return SOURCE[start:following.start() if following else start + 1200]

    def test_the_custom_executor_path_honours_an_explicit_timeout(self):
        body = self._dispatch()
        assert "asyncio.wait_for(" in body
        assert "timeout=explicit_timeout" in body

    def test_no_explicit_value_leaves_the_step_unbounded_there(self):
        """Pushing the 600s fallback onto this path would kill long recipes."""
        body = self._dispatch()
        assert "if explicit_timeout is None:" in body
        assert "return await self._step_executor_fn(step, wf_context)" in body

    def test_the_registry_path_keeps_its_fallback(self):
        body = self._dispatch()
        assert "timeout=step_timeout" in body

    def test_both_attempts_and_retries_go_through_it(self):
        """The retry inside an intervention used to be a second, divergent copy."""
        assert SOURCE.count("await _dispatch()") == 2

    def test_a_timeout_during_a_retry_is_reported_as_one(self):
        assert 'error=f"Timeout after {explicit_timeout}s"' in SOURCE


class TestExplicitTimeoutBehaviour:
    """The wait_for semantics the fix relies on, exercised directly."""

    @pytest.mark.parametrize("declared,expected", [
        (30, 30.0), (0, None), (None, None),
    ])
    def test_normalisation(self, declared, expected):
        assert (normalize_timeout(declared) if declared is not None else None) == expected

    def test_wait_for_cancels_a_slow_step(self):
        async def scenario():
            async def slow():
                await asyncio.sleep(5)
                return "finished"
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(slow(), timeout=0.05)
        asyncio.run(scenario())

    def test_wait_for_leaves_a_quick_step_alone(self):
        async def scenario():
            async def quick():
                return "finished"
            assert await asyncio.wait_for(quick(), timeout=5) == "finished"
        asyncio.run(scenario())


class TestTheOtherTwoFieldsWereNeverBroken:
    """Both are read outside the branch that swallowed TimeoutSeconds, so both
    have always applied however a workflow is started. Deleting them as "dead"
    would remove working behaviour."""

    def test_on_failure_is_read_by_the_wave_loop(self):
        waves = SOURCE[SOURCE.index("async def _execute_waves("):]
        waves = waves[:waves.index("\n    async def ", 1)]
        assert 'step.on_failure == "skip"' in waves
        assert 'step.on_failure == "continue"' in waves

    def test_on_failure_decides_what_happens_to_dependents(self):
        waves = SOURCE[SOURCE.index("async def _execute_waves("):]
        assert 'if step.on_failure == "continue":' in waves

    def test_retry_count_sets_the_attempt_budget(self):
        assert ("retry_count = step.retry_count if step.retry_count is not None "
                "else self._error_config.max_retries") in SOURCE
        assert "max_attempts = retry_count + 1" in SOURCE

    def test_neither_sits_behind_the_custom_executor_branch(self):
        """The fault TimeoutSeconds had: readable only in one of two branches."""
        dispatch_start = SOURCE.index("        async def _dispatch():")
        dispatch_end = SOURCE.index("        context = f\"{step.action}", 0)
        block = SOURCE[min(dispatch_start, dispatch_end):max(dispatch_start, dispatch_end)]
        assert "on_failure" not in block
        assert "retry_count" not in block


class TestTheManualStepParameterIsADifferentThing:
    """`RequestOperatorTask` takes a `TimeoutSeconds` *parameter*, which is how
    long a person has to confirm. Same name, different field, and it works -
    the shipped Opentrons_Refill_Test workflow uses it."""

    def test_the_manual_station_declares_its_default(self):
        """It used to be a setdefault in the executor, keyed on the action name.
        The server says it now, in its own catalog, and dispatch applies it."""
        import json
        catalog = json.loads((V1_DIR / "SiLA2" / "ManualStationSiLA2Server"
                              / "library_catalog.json").read_text(encoding="utf-8"))
        defaults = catalog["parameter_defaults"]["RequestOperatorTask"]
        assert defaults["TimeoutSeconds"] == 600

    def test_a_manual_step_is_dispatched_with_it(self):
        from types import SimpleNamespace
        from src.client import map_parameters
        declared = [SimpleNamespace(identifier=n) for n in
                    ("TaskDescription", "TaskType", "Priority", "SourceInstrument", "TimeoutSeconds")]
        sent, _ = map_parameters({"TaskDescription": "Load the plate", "TimeoutSeconds": 1800},
                                 declared, {"TimeoutSeconds": 600, "Priority": "normal"})
        assert sent["TimeoutSeconds"] == 1800, "the step's own value wins"
        assert sent["Priority"] == "normal"

    def test_the_shipped_workflow_uses_it_as_a_parameter(self):
        import json
        wf = json.loads((V1_DIR / "Library" / "Workflows"
                         / "Opentrons_Refill_Test.workflow.json").read_text(encoding="utf-8-sig"))
        step = next(s for s in wf["Steps"] if s["Action"] == "RequestOperatorTask")
        assert "TimeoutSeconds" in step["Parameters"]
        assert "TimeoutSeconds" not in step, "that would be the step-level field"


class TestATimedOutStepIsLegible:
    """Two things went missing when a step was cut off by its own timeout.

    The operator was asked to decide about "CaptureImage on Opentrons Flex",
    with no mention of a timeout: the timeout CategorizedError put the useful
    text in .message and the bare context in .details, the opposite of every
    other error, and the request shows .details. And the step's timeline row
    stayed "running", because `wait_for` cancels the step from outside and
    CancelledError is not an Exception, so the failure recording never caught
    it.
    """

    ROUTES = (V1_DIR / "webapp" / "routes" / "workflows.py").read_text(encoding="utf-8-sig")

    def test_the_details_say_it_timed_out_and_after_how_long(self):
        block = SOURCE[SOURCE.index("category=ErrorCategory.TIMEOUT,", SOURCE.index("_run_one")):]
        block = block[:block.index(")")]
        assert "nessuna risposta entro" in block
        assert "step.timeout_seconds" in block

    def test_the_message_still_carries_the_short_form(self):
        assert 'message=f"Timeout after {step.timeout_seconds}s"' in SOURCE

    def test_a_cancelled_step_is_caught_by_the_recorder(self):
        assert "except (Exception, asyncio.CancelledError) as exc:" in self.ROUTES

    def test_it_is_recorded_as_a_timeout_not_a_plain_failure(self):
        assert '"status": "timeout" if isinstance(exc, asyncio.CancelledError) else "failed"' \
            in self.ROUTES

    def test_a_cancellation_with_no_message_still_gets_one(self):
        """CancelledError usually stringifies to nothing at all."""
        assert "TimeoutSeconds dello step scaduto" in self.ROUTES

    def test_the_cancellation_is_re_raised(self):
        """Recorded, not swallowed - the executor still has to see it."""
        start = self.ROUTES.index("except (Exception, asyncio.CancelledError) as exc:")
        # Up to the next statement at the same indentation as the try block.
        following = re.compile(r"^            \S", re.M).search(self.ROUTES, start + 1)
        block = self.ROUTES[start:following.start() if following else len(self.ROUTES)]
        assert re.search(r"^                raise$", block, re.M), (
            "the failure is recorded but never re-raised, so the executor "
            "would treat a timed-out step as having succeeded"
        )
