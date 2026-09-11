"""What happens when a workflow step fails.

The executor has always been able to ask the operator whether to retry, skip or
abort. Nobody could ever answer.

The request was broadcast on the websocket and nowhere else. The Operator page
pushes an incoming `intervention_required` into a local array, and every three
seconds `refreshPendingActions()` replaces that array wholesale with
`GET /api/operator/pending` - which interventions were never part of. So the
buttons appeared and were wiped within three seconds; a page opened after the
failure, or simply reloaded, never saw the request at all. Checked against the
first commit in the repository: it was written this way from the start, so this
is not a regression, it is a feature that never worked.

Reproduced on the bench: a three-step workflow whose middle step failed sat at
"running" indefinitely while `/api/operator/pending` returned an empty list.
"""
import inspect
import re
import sys
from pathlib import Path

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V1_DIR))

SOURCE = (V1_DIR / "webapp" / "routes" / "workflows.py").read_text(encoding="utf-8-sig")
OPERATOR_PAGE = (V1_DIR / "webapp" / "templates" / "operator.html").read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    start = SOURCE.index(f"async def {name}(") if f"async def {name}(" in SOURCE \
        else SOURCE.index(f"def {name}(")
    following = re.compile(r"^(?:async )?def ", re.M).search(SOURCE, start + 1)
    return SOURCE[start:following.start() if following else len(SOURCE)]


class TestTheRequestReachesTheOperatorQueue:

    def test_it_is_added_to_the_queue_the_page_actually_reads(self):
        body = _function_body("_ask_operator")
        assert "pending_operator_actions.append(notification)" in body, (
            "a websocket broadcast alone is wiped by the page's own 3-second "
            "refresh; it has to be in the queue /api/operator/pending serves"
        )

    def test_the_queue_it_uses_is_the_one_the_endpoint_serves(self):
        """Same list object as manual steps and tip refills."""
        endpoint = (V1_DIR / "webapp" / "routes" / "operator.py").read_text(encoding="utf-8-sig")
        assert "return {\"actions\": list(pending_operator_actions)}" in endpoint
        assert "pending_operator_actions: list," in SOURCE

    def test_the_entry_carries_what_the_page_needs_to_draw_the_buttons(self):
        body = _function_body("_ask_operator")
        for field in ('"is_intervention": True', '"intervention_id"', '"id": intervention_id'):
            assert field in body, f"missing {field}"

    def test_the_page_draws_retry_skip_and_abort_for_such_an_entry(self):
        assert "action.is_intervention" in OPERATOR_PAGE
        for verb in ("'retry'", "'skip'", "'abort'"):
            assert verb in OPERATOR_PAGE

    def test_the_websocket_still_carries_it_too(self):
        """The queue is what makes it durable; the broadcast is what makes it
        immediate. Both, not one."""
        body = _function_body("_ask_operator")
        assert "ws_manager.broadcast(broadcast)" in body

    def test_it_is_removed_however_the_wait_ends(self):
        body = _function_body("_ask_operator")
        assert "finally:" in body
        tail = body[body.index("finally:"):]
        assert "pending_interventions.pop(intervention_id, None)" in tail
        assert "a.get(\"id\") != intervention_id" in tail

    def test_answering_clears_it_immediately(self):
        """Not three seconds later, which invites a second click."""
        body = _function_body("respond_to_intervention")
        assert "pending_operator_actions[:]" in body


class TestNobodyAnswering:

    def test_it_aborts_rather_than_skipping(self):
        """Skipping a step nobody agreed to skip is the outcome that quietly
        ruins a plate: the workflow carries on as if the step had happened."""
        body = _function_body("_ask_operator")
        timeout_branch = body[body.index("except asyncio.TimeoutError:"):]
        assert "InterventionAction.ABORT" in timeout_branch
        assert "InterventionAction.SKIP" not in timeout_branch

    def test_skipping_is_still_available_as_a_decision(self):
        assert '"skip": InterventionAction.SKIP' in SOURCE

    def test_the_timeout_is_the_configured_one(self):
        body = _function_body("_ask_operator")
        assert "webapp.timeouts.intervention_timeout" in body

    def test_giving_up_is_announced(self):
        body = _function_body("_ask_operator")
        assert "intervention_timeout" in body
        assert "aborting the workflow" in body


class TestBothExecutionPathsUseIt:
    """A single workflow and a batch run had two copies of this logic; only one
    of them would ever have been fixed."""

    @pytest.mark.parametrize("callback", ["on_intervention", "on_batch_intervention"])
    def test_the_callback_delegates(self, callback):
        body = _function_body(callback)
        assert "_ask_operator(" in body

    @pytest.mark.parametrize("callback", ["on_intervention", "on_batch_intervention"])
    def test_neither_keeps_its_own_copy_of_the_waiting(self, callback):
        body = _function_body(callback)
        assert "asyncio.wait_for" not in body
        assert "create_future" not in body


class TestAFailedValidationStopsTheWorkflow:
    """`ValidateRecipe` answering "not valid" is a successful command - it ran,
    and the verdict is its result - but a failed step. The robot cannot execute
    that recipe, so everything after it fails anyway, on a deck that has already
    been touched."""

    def _check(self, action, data, success=True):
        sys.path.insert(0, str(V1_DIR))
        from webapp.routes.workflows import _validation_failure

        class R:
            pass
        r = R()
        r.success = success
        r.data = data
        return _validation_failure(action, r)

    def test_an_invalid_recipe_produces_a_complaint(self):
        message = self._check("ValidateRecipe", {"ValidationResult": (
            '{"valid": false, "errors": ["\'MyPlate\' is not in the loaded HAL '
            'config (01_SPAAC)"], "hal_config": "01_SPAAC"}')})
        assert message is not None
        assert "MyPlate" in message
        assert "01_SPAAC" in message

    def test_a_valid_recipe_passes(self):
        assert self._check("ValidateRecipe",
                           {"ValidationResult": '{"valid": true, "errors": []}'}) is None

    def test_a_missing_recipe_is_also_a_failure(self):
        message = self._check("ValidateRecipe", {"ValidationResult": (
            '{"valid": false, "errors": ["Recipe not found: nope.json"]}')})
        assert message is not None and "nope.json" in message

    def test_other_commands_are_untouched(self):
        assert self._check("ExecuteRecipe", {"RunId": "abc"}) is None
        assert self._check("CaptureImage", {"CaptureResult": '{"saved": false}'}) is None

    def test_an_unreadable_result_does_not_invent_a_failure(self):
        assert self._check("ValidateRecipe", {"ValidationResult": "not json"}) is None
        assert self._check("ValidateRecipe", {}) is None
        assert self._check("ValidateRecipe", None) is None

    def test_a_command_that_already_failed_is_left_alone(self):
        assert self._check("ValidateRecipe", None, success=False) is None

    def test_the_step_executor_raises_on_it(self):
        body = _function_body("_execute_step_core") if "_execute_step_core" in SOURCE else SOURCE
        assert "_validation_failure(action, result)" in body
        assert "raise Exception(invalid)" in body


class TestTheTimelineClosesAFailedAttempt:
    """A step that fails raises, so the completion write below it never ran.

    On the bench, one step retried four times showed up in the run timeline as
    four rows all reading "running", with no reason recorded against any of
    them - for a step that had already failed four times and was sitting on an
    intervention. A timeline that says "in progress" about something that
    stopped is worse than no timeline.
    """

    HANDLER = "except (Exception, asyncio.CancelledError) as exc:"

    def _failure_handler(self):
        body = _function_body("web_execute_step")
        return body[body.index(self.HANDLER):]

    def test_the_failure_is_recorded_before_it_is_re_raised(self):
        body = _function_body("web_execute_step")
        assert self.HANDLER in body
        failure = self._failure_handler()
        assert "runs.record_step" in failure
        assert '"status": "timeout" if isinstance' in failure

    def test_a_cancelled_step_is_caught_too(self):
        """A step cut off by its own TimeoutSeconds is cancelled from outside,
        and CancelledError is not an Exception subclass."""
        assert "asyncio.CancelledError" in self.HANDLER
        assert self.HANDLER in _function_body("web_execute_step")

    def test_the_failure_still_reaches_the_retry_machinery(self):
        """Recorded, not swallowed."""
        failure = self._failure_handler()
        assert "raise" in failure.split("runs.record_step")[1]

    def test_the_recorded_row_carries_the_reason(self):
        failure = self._failure_handler()
        assert "str(exc)" in failure

    def test_each_attempt_is_its_own_row(self):
        """So four retries read as four attempts, not one overwritten row."""
        failure = self._failure_handler()
        assert '"attempt": _attempts.get(_n, 1)' in failure

    def test_the_row_is_closed_with_a_duration(self):
        failure = self._failure_handler()
        assert '"ended_at"' in failure
        assert '"duration_seconds"' in failure


class TestTheOperatorIsToldWhatWentWrong:
    """CategorizedError.message is a category label - "Hardware error:
    ValidateRecipe on Opentrons Flex". The raw text is in .details. Asking
    somebody to choose between retry, skip and abort while showing only the
    label is asking for a decision without saying what it is about."""

    def test_the_message_shows_the_details(self):
        body = _function_body("_ask_operator")
        assert "req.error.details or req.error.message" in body

    def test_the_error_field_shows_the_details_too(self):
        body = _function_body("_ask_operator")
        assert '"error": req.error.details or req.error.message' in body

    def test_the_category_is_kept_but_as_a_label(self):
        body = _function_body("_ask_operator")
        assert '"category_label": req.error.message' in body
        assert '"category": req.error.category.value' in body

    def test_the_log_line_carries_it_as_well(self):
        body = _function_body("_ask_operator")
        log = body[body.index("state.add_log("):]
        assert "req.error.details or req.error.message" in log


class TestASkippedStepIsReportedAsSkipped:
    """An operator-skipped step comes back successful so the rest of the
    workflow can carry on - that is what they decided. It is still not a step
    that ran, and a summary reading "2 completed, 0 skipped" over a step
    somebody waved past is the opposite of a record.

    Seen on the bench: a two-step workflow whose first step the operator
    skipped finished as "completed 2, failed 0, skipped 0".
    """

    EXECUTOR = (V1_DIR / "src" / "workflow.py").read_text(encoding="utf-8-sig")

    def _wave_outcome(self):
        start = self.EXECUTOR.index("if outcome.success:")
        return self.EXECUTOR[start:start + 1400]

    def test_it_is_counted_as_skipped_not_completed(self):
        body = self._wave_outcome()
        assert "operator_skipped" in body
        assert "skipped += 1" in body

    def test_the_step_carries_the_skipped_status(self):
        body = self._wave_outcome()
        assert "StepStatus.SKIPPED" in body

    def test_the_reason_is_kept(self):
        body = self._wave_outcome()
        assert 'outcome.data.get("reason")' in body
        assert "errors.append" in body

    def test_the_workflow_still_continues_past_it(self):
        """Skip means carry on; the step joins `done` so its dependents run."""
        body = self._wave_outcome()
        assert "done.add(step.step_number)" in body
        assert body.index("done.add(step.step_number)") < body.index("if operator_skipped:")

    def test_the_marker_is_the_one_the_intervention_sets(self):
        assert '"skipped": True, "reason": "User intervention"' in self.EXECUTOR


class TestRetryIsNotAOneShotGamble:
    """Pressing Retry used to be a single throw of the dice.

    The intervention block ran once. A retry that failed again fell straight
    through to "step failed" and the workflow halted without asking a second
    time, so an operator who fixed something, pressed Retry and got it slightly
    wrong lost the run with no further say. Seen on the bench: attempt 5
    appeared in the timeline, failed, and the run ended without a second
    request ever reaching the queue.
    """

    EXECUTOR = (V1_DIR / "src" / "workflow.py").read_text(encoding="utf-8-sig")

    def _block(self):
        start = self.EXECUTOR.index("Automatic retries are exhausted")
        end = self.EXECUTOR.index("MAX_INTERVENTION_ROUNDS:", start)
        return self.EXECUTOR[start:end]

    def test_the_operator_is_asked_again_after_a_failed_retry(self):
        block = self._block()
        assert "while (self._intervention_callback" in block

    def test_the_next_question_carries_the_new_error(self):
        """Not the one that started it all."""
        block = self._block()
        assert "last_error = CategorizedError.from_result(result, context)" in block

    def test_a_successful_retry_ends_it(self):
        block = self._block()
        assert "if result.success:" in block
        assert "return result" in block

    def test_skip_and_abort_still_end_it(self):
        block = self._block()
        assert "InterventionAction.SKIP" in block
        assert "InterventionAction.ABORT" in block
        assert "self.request_abort()" in block
        assert "break" in block

    def test_a_retry_that_raises_is_caught_and_asked_about(self):
        """An exception during the retry is a failed retry, not a broken
        intervention callback - they used to be reported as the same thing."""
        block = self._block()
        retry = block[block.index("step.status = StepStatus.RUNNING"):]
        assert "except Exception as exc:" in retry
        assert "CommandResult(success=False, error=str(exc))" in retry

    def test_there_is_a_ceiling(self):
        """A human clicking Retry is not a runaway loop, but a callback that
        answers on its own could be."""
        assert "MAX_INTERVENTION_ROUNDS = 20" in self.EXECUTOR
        assert "rounds < self.MAX_INTERVENTION_ROUNDS" in self.EXECUTOR

    def test_hitting_the_ceiling_is_logged(self):
        assert "times; giving up." in self.EXECUTOR

    def test_a_successful_run_still_reports_what_was_skipped(self):
        """"1 skipped" with no hint of which step or why is a count, not a
        record."""
        assert '"notes": result.errors,' in SOURCE


class TestTheModalOnEveryPageCanRenderIt:
    """`base.html` pops a decision dialog on whatever page the operator happens
    to be looking at. It reads step_number, error_message, attempt and
    max_attempts - none of which anything has ever sent, so it has always
    rendered "Step ? — / Unknown error / Attempt ? / ?": a dialog demanding a
    decision while telling the reader nothing about it."""

    BASE_PAGE = (V1_DIR / "webapp" / "templates" / "base.html").read_text(encoding="utf-8")

    @pytest.mark.parametrize("field", [
        "step_number", "error_message", "attempt", "max_attempts",
    ])
    def test_the_field_the_modal_reads_is_sent(self, field):
        assert f"data.{field}" in self.BASE_PAGE, f"the modal stopped reading {field}"
        assert f'"{field}"' in _function_body("_ask_operator"), f"nothing sends {field}"

    def test_the_operator_page_spelling_is_still_sent_too(self):
        """Two pages, two vocabularies; send both rather than break one."""
        body = _function_body("_ask_operator")
        assert '"step": req.step_number' in body
        assert '"error": req.error.details or req.error.message' in body
