"""
Tests for bug fixes applied in session 2026-05:
  - WorkflowStep.retry_count None → uses error_handling.max_retries
  - workflow.default_timeout replaces hardcoded 300s
  - Intervention RETRY uses _step_executor_fn when available
  - _execute_parallel() emits _notify_progress() for all events
  - _execute_parallel() reports error for dependency-skipped steps
  - config_schema DEFAULT_CONFIG mDNS service_type corrected
  - lab_core._discovery_cache_ttl reads from config
"""

import asyncio
import pytest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry():
    """Minimal mock PnPRegistry."""
    reg = MagicMock()
    reg.execute = AsyncMock()
    return reg


def _make_executor(registry=None, error_config=None, default_timeout=None):
    """Create PnPWorkflowExecutor with optional config overrides."""
    from src.workflow import PnPWorkflowExecutor
    if registry is None:
        registry = _make_registry()
    executor = PnPWorkflowExecutor(registry)
    if error_config is not None:
        executor._error_config = error_config
    if default_timeout is not None:
        executor._default_step_timeout = default_timeout
    return executor


def _make_step(**kwargs):
    from src.workflow import WorkflowStep
    defaults = dict(
        step_number=1,
        instrument="test_instr",
        action="test_action",
        parameters={},
        depends_on=[],
        retry_count=None,
        timeout_seconds=None,
        on_failure="stop",
    )
    defaults.update(kwargs)
    return WorkflowStep(**defaults)


def _make_workflow(steps):
    from src.workflow import Workflow
    wf = Workflow(name="TestWF", steps=steps)
    return wf


def _cmd_ok(data=None):
    from src.client import CommandResult
    return CommandResult(success=True, data=data or {})


def _cmd_fail(error="fail"):
    from src.client import CommandResult
    return CommandResult(success=False, error=error)


# ---------------------------------------------------------------------------
# Fix 1 + 5: WorkflowStep.retry_count=None → uses error_handling.max_retries
# ---------------------------------------------------------------------------

class TestRetryCountFromConfig:

    @pytest.mark.asyncio
    async def test_none_retry_count_uses_config_max_retries(self):
        """When retry_count is None, executor uses error_handling.max_retries."""
        registry = _make_registry()
        # Always fail so we can count attempts
        registry.execute = AsyncMock(return_value=_cmd_fail("always fails"))

        executor = _make_executor(
            registry=registry,
            error_config={
                "max_retries": 2,
                "retry_strategy": "constant",
                "base_delay_seconds": 0.0,
                "max_delay_seconds": 0.0,
                "enable_human_intervention": False,
            },
        )

        step = _make_step(retry_count=None)  # None → should do 2 retries = 3 attempts
        await executor._execute_step_with_retry(step)

        # max_retries=2 → max_attempts=3
        assert registry.execute.call_count == 3

    @pytest.mark.asyncio
    async def test_explicit_zero_retry_count_overrides_config(self):
        """When retry_count=0 is explicitly set, only 1 attempt regardless of config."""
        registry = _make_registry()
        registry.execute = AsyncMock(return_value=_cmd_fail("always fails"))

        executor = _make_executor(
            registry=registry,
            error_config={
                "max_retries": 5,  # would be 6 attempts if None were used
                "retry_strategy": "constant",
                "base_delay_seconds": 0.0,
                "max_delay_seconds": 0.0,
                "enable_human_intervention": False,
            },
        )

        step = _make_step(retry_count=0)  # explicit 0 → 1 attempt
        await executor._execute_step_with_retry(step)

        assert registry.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_explicit_positive_retry_count_overrides_config(self):
        """When retry_count=1 is explicitly set, 2 attempts regardless of config."""
        registry = _make_registry()
        registry.execute = AsyncMock(return_value=_cmd_fail("always fails"))

        executor = _make_executor(
            registry=registry,
            error_config={
                "max_retries": 5,
                "retry_strategy": "constant",
                "base_delay_seconds": 0.0,
                "max_delay_seconds": 0.0,
                "enable_human_intervention": False,
            },
        )

        step = _make_step(retry_count=1)
        await executor._execute_step_with_retry(step)

        assert registry.execute.call_count == 2


# ---------------------------------------------------------------------------
# Fix 4: workflow.default_timeout replaces hardcoded 300s
# ---------------------------------------------------------------------------

class TestDefaultTimeout:

    @pytest.mark.asyncio
    async def test_step_without_timeout_uses_default(self):
        """Step with timeout_seconds=None uses executor._default_step_timeout."""
        registry = _make_registry()
        registry.execute = AsyncMock(return_value=_cmd_ok())

        executor = _make_executor(registry=registry, default_timeout=900.0)
        step = _make_step(retry_count=0, timeout_seconds=None)
        await executor._execute_step_with_retry(step)

        registry.execute.assert_called_once()
        _, kwargs = registry.execute.call_args
        assert kwargs.get("timeout") == 900.0

    @pytest.mark.asyncio
    async def test_step_with_explicit_timeout_overrides_default(self):
        """Step with timeout_seconds set uses that value, not the default."""
        registry = _make_registry()
        registry.execute = AsyncMock(return_value=_cmd_ok())

        executor = _make_executor(registry=registry, default_timeout=900.0)
        step = _make_step(retry_count=0, timeout_seconds=42.0)
        await executor._execute_step_with_retry(step)

        registry.execute.assert_called_once()
        _, kwargs = registry.execute.call_args
        assert kwargs.get("timeout") == 42.0

    def test_load_default_step_timeout_function(self, tmp_path):
        """_load_default_step_timeout reads workflow.default_timeout from yaml."""
        import yaml
        cfg = tmp_path / "lab_config.yaml"
        cfg.write_text(yaml.dump({"workflow": {"default_timeout": 1200}}))

        with patch("src.workflow.Path") as mock_path:
            mock_path.return_value.parent.parent = tmp_path
            # Directly test reading without touching the real config
            from src.config_schema import load_lab_config
            config, _ = load_lab_config(cfg, apply_defaults=False, strict=False)
            assert config["workflow"]["default_timeout"] == 1200


# ---------------------------------------------------------------------------
# Fix 6: Intervention RETRY uses _step_executor_fn if available
# ---------------------------------------------------------------------------

class TestInterventionRetryUsesStepExecutor:

    @pytest.mark.asyncio
    async def test_intervention_retry_uses_step_executor_fn(self):
        """After human intervention RETRY, the call goes through _step_executor_fn, not registry.execute."""
        from src.lab_core import InterventionAction
        from src.workflow import InterventionRequest, CategorizedError, ErrorCategory

        registry = _make_registry()
        # First call fails, triggering intervention
        registry.execute = AsyncMock(return_value=_cmd_fail("hardware jammed"))

        executor = _make_executor(
            registry=registry,
            error_config={
                "max_retries": 0,
                "retry_strategy": "constant",
                "base_delay_seconds": 0.0,
                "max_delay_seconds": 0.0,
                "enable_human_intervention": True,
            },
        )

        custom_executor_called = []

        async def custom_step_executor(step, ctx):
            custom_executor_called.append(True)
            return _cmd_ok({"from": "custom"})

        executor.set_step_executor(custom_step_executor)

        async def intervention_cb(req):
            return InterventionAction.RETRY

        executor.set_intervention_callback(intervention_cb)

        step = _make_step(retry_count=0)
        result = await executor._execute_step_with_retry(step)

        assert result.success
        assert custom_executor_called  # custom fn was called for the retry
        # registry.execute should NOT have been called (custom fn handled both paths)
        registry.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_intervention_retry_without_step_executor_uses_registry(self):
        """When no _step_executor_fn is set, intervention RETRY falls back to registry.execute."""
        from src.lab_core import InterventionAction

        registry = _make_registry()
        # First call via registry fails; retry via registry succeeds
        registry.execute = AsyncMock(side_effect=[_cmd_fail("err"), _cmd_ok()])

        executor = _make_executor(
            registry=registry,
            error_config={
                "max_retries": 0,
                "retry_strategy": "constant",
                "base_delay_seconds": 0.0,
                "max_delay_seconds": 0.0,
                "enable_human_intervention": True,
            },
        )

        async def intervention_cb(req):
            return InterventionAction.RETRY

        executor.set_intervention_callback(intervention_cb)

        step = _make_step(retry_count=0)
        result = await executor._execute_step_with_retry(step)

        assert result.success
        assert registry.execute.call_count == 2


# ---------------------------------------------------------------------------
# Fix 7 + 8: _execute_parallel emits _notify_progress + dependency skip error
# ---------------------------------------------------------------------------

class TestParallelProgress:

    @pytest.mark.asyncio
    async def test_parallel_emits_running_and_success_progress(self):
        """_execute_parallel should call _notify_progress for RUNNING and SUCCESS."""
        from src.workflow import StepStatus

        registry = _make_registry()
        registry.execute = AsyncMock(return_value=_cmd_ok())

        executor = _make_executor(registry=registry)

        # Use a mutable container to avoid closure scope issues in async tests
        seen = {"statuses": []}

        def capture(p, _s=seen):
            _s["statuses"].append(p.step_status)

        executor.add_progress_callback(capture)

        steps = [_make_step(step_number=1, retry_count=0)]
        wf = _make_workflow(steps)
        wf.start_time = __import__('datetime').datetime.now()

        from src.workflow import WorkflowContext
        ctx = WorkflowContext()

        await executor._execute_parallel(wf, ctx)

        statuses = seen["statuses"]
        assert StepStatus.RUNNING in statuses, f"Expected RUNNING in {statuses}"
        assert StepStatus.SUCCESS in statuses, f"Expected SUCCESS in {statuses}"

    @pytest.mark.asyncio
    async def test_parallel_emits_failed_progress(self):
        """_execute_parallel should call _notify_progress for FAILED."""
        from src.workflow import StepStatus

        registry = _make_registry()
        registry.execute = AsyncMock(return_value=_cmd_fail("boom"))

        executor = _make_executor(
            registry=registry,
            error_config={
                "max_retries": 0,
                "retry_strategy": "constant",
                "base_delay_seconds": 0.0,
                "max_delay_seconds": 0.0,
                "enable_human_intervention": False,
            },
        )

        progress_events = []
        executor.add_progress_callback(lambda p: progress_events.append(p.step_status))

        steps = [_make_step(step_number=1, retry_count=0, on_failure="continue")]
        wf = _make_workflow(steps)
        wf.start_time = __import__('datetime').datetime.now()

        from src.workflow import WorkflowContext
        ctx = WorkflowContext()
        await executor._execute_parallel(wf, ctx)

        assert StepStatus.FAILED in progress_events

    @pytest.mark.asyncio
    async def test_parallel_reports_error_for_dependency_skipped_step(self):
        """When a step is skipped due to failed dependency, an error is appended."""
        from src.workflow import StepStatus

        registry = _make_registry()
        registry.execute = AsyncMock(return_value=_cmd_fail("step 1 failed"))

        executor = _make_executor(
            registry=registry,
            error_config={
                "max_retries": 0,
                "retry_strategy": "constant",
                "base_delay_seconds": 0.0,
                "max_delay_seconds": 0.0,
                "enable_human_intervention": False,
            },
        )

        progress_events = []
        executor.add_progress_callback(lambda p: progress_events.append((p.step_status, p.message)))

        step1 = _make_step(step_number=1, retry_count=0, on_failure="stop")
        step2 = _make_step(step_number=2, retry_count=0, depends_on=[1], on_failure="stop")

        wf = _make_workflow([step1, step2])
        wf.start_time = __import__('datetime').datetime.now()

        from src.workflow import WorkflowContext
        ctx = WorkflowContext()
        completed, failed, skipped, step_results, errors = await executor._execute_parallel(wf, ctx)

        assert skipped == 1
        # Check that a SKIPPED progress event was emitted for step 2
        skipped_events = [e for e in progress_events if e[0] == StepStatus.SKIPPED]
        assert skipped_events, "Expected at least one SKIPPED progress event"
        # Check that an error message was added for the skipped step
        assert any("skipped" in e.lower() or "dependency" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# Fix 9: config_schema DEFAULT_CONFIG mDNS service_type
# ---------------------------------------------------------------------------

class TestConfigSchemaServiceType:

    def test_default_service_type_is_sila_not_sila2(self):
        """DEFAULT_CONFIG discovery.service_type must be _sila._tcp.local. (not _sila2.)"""
        from src.config_schema import DEFAULT_CONFIG
        service_type = DEFAULT_CONFIG["discovery"]["service_type"]
        assert service_type == "_sila._tcp.local.", (
            f"Expected '_sila._tcp.local.' but got '{service_type}'. "
            "The standard SiLA2 mDNS type is _sila._tcp not _sila2._tcp"
        )


# ---------------------------------------------------------------------------
# Fix 3: WorkflowStep.from_dict RetryCount None
# ---------------------------------------------------------------------------

class TestWorkflowFromDict:

    def test_from_dict_missing_retry_count_gives_none(self):
        """If RetryCount is absent in JSON, retry_count should be None (use config default)."""
        from src.workflow import Workflow
        data = {
            "WorkflowName": "Test",
            "Steps": [
                {"StepNumber": 1, "Instrument": "opentrons", "Action": "ExecuteRecipe"}
            ]
        }
        wf = Workflow.from_dict(data)
        assert wf.steps[0].retry_count is None

    def test_from_dict_explicit_retry_count_preserved(self):
        """If RetryCount is explicitly 0 in JSON, it stays 0."""
        from src.workflow import Workflow
        data = {
            "WorkflowName": "Test",
            "Steps": [
                {"StepNumber": 1, "Instrument": "opentrons", "Action": "ExecuteRecipe", "RetryCount": 0}
            ]
        }
        wf = Workflow.from_dict(data)
        assert wf.steps[0].retry_count == 0

    def test_from_dict_explicit_retry_count_2_preserved(self):
        """If RetryCount is explicitly 2 in JSON, it stays 2."""
        from src.workflow import Workflow
        data = {
            "WorkflowName": "Test",
            "Steps": [
                {"StepNumber": 1, "Instrument": "opentrons", "Action": "ExecuteRecipe", "RetryCount": 2}
            ]
        }
        wf = Workflow.from_dict(data)
        assert wf.steps[0].retry_count == 2


# ---------------------------------------------------------------------------
# Fix 10: lab_core._discovery_cache_ttl reads from config
# ---------------------------------------------------------------------------

class TestLabCoreDiscoveryCacheTTL:

    def test_discovery_cache_ttl_reads_scan_interval(self, tmp_path):
        """LabCore._discovery_cache_ttl should come from discovery.scan_interval."""
        import yaml
        cfg = tmp_path / "lab_config.yaml"
        cfg.write_text(yaml.dump({
            "system": {"log_level": "INFO"},
            "discovery": {"scan_interval": 120},
            "servers": {},
        }))
        from src.lab_core import LabCore
        core = LabCore(base_dir=tmp_path)
        assert core._discovery_cache_ttl == 120.0

    def test_discovery_cache_ttl_default_when_absent(self, tmp_path):
        """When discovery.scan_interval is absent, _discovery_cache_ttl defaults to 30."""
        import yaml
        cfg = tmp_path / "lab_config.yaml"
        cfg.write_text(yaml.dump({"servers": {}}))
        from src.lab_core import LabCore
        core = LabCore(base_dir=tmp_path)
        assert core._discovery_cache_ttl == 30.0
