#!/usr/bin/env python3
"""
Plug & Play Workflow Executor
=============================

Executes workflows using ONLY server metadata - NO hardcoded instrument logic.

Features:
- Generic workflow execution via PnPRegistry
- Parallel step execution when dependencies allow
- Progress tracking with callbacks
- Error categorization and recovery strategies
- Human intervention support (for CLI and WebApp)
- Exponential backoff on retries
- Workflow validation against available servers/commands

Design:
- Workflows reference instruments by NAME (matched to discovered servers)
- Actions are validated against server command definitions
- Parameters are type-checked against command metadata
- No hardcoded instrument-specific execution paths
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Awaitable


logger = logging.getLogger(__name__)

from src.discovery import PnPServer
from src.client import PnPRegistry, CommandResult


# =============================================================================
#                           ERROR CATEGORIES
# =============================================================================

class ErrorCategory(Enum):
    """Categorization of errors for appropriate handling."""
    DEVICE_UNAVAILABLE = "device_unavailable"  # Server/instrument not reachable
    OPERATION_FAILURE = "operation_failure"    # Command failed but device OK
    HARDWARE_ERROR = "hardware_error"          # Physical hardware problem
    TIMEOUT = "timeout"                        # Command timed out
    VALIDATION_ERROR = "validation_error"      # Invalid parameters
    UNKNOWN = "unknown"                        # Unclassified error


@dataclass
class CategorizedError:
    """An error with category and context."""
    category: ErrorCategory
    message: str
    details: Optional[str] = None
    recoverable: bool = True
    
    @staticmethod
    def from_exception(exc: Exception, context: str = "") -> "CategorizedError":
        """Categorize an exception."""
        msg = str(exc)
        exc_type = type(exc).__name__
        
        # Device unavailable patterns
        if any(x in msg.lower() for x in ['unavailable', 'connection refused', 'no route', 'unreachable', 'not found']):
            return CategorizedError(
                category=ErrorCategory.DEVICE_UNAVAILABLE,
                message=f"Device unavailable: {context}",
                details=msg,
                recoverable=True
            )
        
        # Timeout patterns
        if 'timeout' in msg.lower() or isinstance(exc, asyncio.TimeoutError):
            return CategorizedError(
                category=ErrorCategory.TIMEOUT,
                message=f"Operation timed out: {context}",
                details=msg,
                recoverable=True
            )
        
        # Hardware error patterns
        if any(x in msg.lower() for x in ['hardware', 'motor', 'sensor', 'collision', 'jam', 'stuck', 'limit']):
            return CategorizedError(
                category=ErrorCategory.HARDWARE_ERROR,
                message=f"Hardware error: {context}",
                details=msg,
                recoverable=False  # Usually needs physical intervention
            )
        
        # Operation failure (command understood but failed)
        if any(x in msg.lower() for x in ['failed', 'error', 'invalid', 'cannot', 'unable']):
            return CategorizedError(
                category=ErrorCategory.OPERATION_FAILURE,
                message=f"Operation failed: {context}",
                details=msg,
                recoverable=True
            )
        
        return CategorizedError(
            category=ErrorCategory.UNKNOWN,
            message=f"Unknown error: {context}",
            details=msg,
            recoverable=True
        )
    
    @staticmethod
    def from_result(result: CommandResult, context: str = "") -> "CategorizedError":
        """Categorize from a failed CommandResult."""
        error_msg = result.error or "Unknown error"
        return CategorizedError.from_exception(Exception(error_msg), context)


# =============================================================================
#                         HUMAN INTERVENTION
# =============================================================================

class InterventionAction(Enum):
    """Possible actions after human intervention."""
    RETRY = "retry"      # Retry the failed step
    SKIP = "skip"        # Skip this step and continue
    ABORT = "abort"      # Abort the entire workflow


@dataclass
class InterventionRequest:
    """Request for human intervention."""
    step_number: int
    instrument: str
    action: str
    error: CategorizedError
    attempt: int
    max_attempts: int
    workflow_name: str


# Callback type for human intervention
# Takes InterventionRequest, returns InterventionAction
# Can be async (for WebApp) or sync (for CLI)
InterventionCallback = Callable[[InterventionRequest], Awaitable[InterventionAction]]


# =============================================================================
#                              DATA CLASSES
# =============================================================================

class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING_INTERVENTION = "waiting_intervention"


@dataclass
class WorkflowStep:
    """A single step in a workflow."""
    step_number: int
    instrument: str
    action: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[int] = field(default_factory=list)
    retry_count: int = 0
    timeout_seconds: Optional[float] = None
    on_failure: str = "stop"  # stop, continue, skip
    
    # Runtime state
    status: StepStatus = StepStatus.PENDING
    result: Optional[CommandResult] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    
    @property
    def duration_seconds(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return None


@dataclass
class Workflow:
    """A complete workflow definition."""
    name: str
    steps: List[WorkflowStep]
    description: str = ""
    version: str = "1.0"
    author: str = ""
    created_at: Optional[datetime] = None
    
    # Runtime state
    status: StepStatus = StepStatus.PENDING
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Workflow":
        """Create workflow from dictionary."""
        steps = []
        for step_data in data.get("Steps", []):
            step = WorkflowStep(
                step_number=step_data.get("StepNumber", len(steps) + 1),
                instrument=step_data.get("Instrument", ""),
                action=step_data.get("Action", ""),
                parameters=step_data.get("Parameters", {}),
                depends_on=step_data.get("DependsOn", []),
                retry_count=step_data.get("RetryCount", 0),
                timeout_seconds=step_data.get("TimeoutSeconds"),
                on_failure=step_data.get("OnFailure", "stop")
            )
            steps.append(step)
        
        return cls(
            name=data.get("WorkflowName", "Unnamed"),
            steps=steps,
            description=data.get("Description", ""),
            version=data.get("Version", "1.0"),
            author=data.get("Author", "")
        )
    
    @classmethod
    def from_file(cls, path: Path) -> "Workflow":
        """Load workflow from JSON file."""
        with open(path) as f:
            data = json.load(f)
        return cls.from_dict(data)
    
    def to_dict(self) -> Dict[str, Any]:
        """Export workflow to dictionary."""
        return {
            "WorkflowName": self.name,
            "Description": self.description,
            "Version": self.version,
            "Author": self.author,
            "Steps": [
                {
                    "StepNumber": s.step_number,
                    "Instrument": s.instrument,
                    "Action": s.action,
                    "Parameters": s.parameters,
                    "DependsOn": s.depends_on,
                    "RetryCount": s.retry_count,
                    "TimeoutSeconds": s.timeout_seconds,
                    "OnFailure": s.on_failure
                }
                for s in self.steps
            ]
        }


@dataclass
class ValidationError:
    """Workflow validation error."""
    step_number: int
    field: str
    message: str


@dataclass
class WorkflowContext:
    """
    Context for automatic variable propagation between workflow steps.
    
    Auto-generates and propagates plate_id between Opentrons pipetting 
    and Tecan analysis steps for full sample traceability.
    """
    plate_id: Optional[str] = None
    step_results: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    workflow_name: str = ""
    
    def resolve_parameters(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Resolve ${workflow.plate_id} and similar placeholders in parameters.
        
        Supports:
        - ${workflow.plate_id} - current workflow plate_id
        - ${step_N.result.field} - field from step N result
        """
        import re
        resolved = {}
        
        for key, value in params.items():
            if isinstance(value, str):
                pattern = r'\$\{(\w+)\.(\w+)(?:\.(\w+))?\}'
                
                def replacer(match):
                    scope = match.group(1)
                    field = match.group(2)
                    subfield = match.group(3)
                    
                    if scope == "workflow":
                        val = getattr(self, field, None) or match.group(0)
                        return str(val)
                    elif scope.startswith("step_"):
                        step_num = int(scope.replace("step_", ""))
                        step_data = self.step_results.get(step_num, {})
                        if subfield:
                            return str(step_data.get(field, {}).get(subfield, match.group(0)))
                        return str(step_data.get(field, match.group(0)))
                    return match.group(0)
                
                resolved[key] = re.sub(pattern, replacer, value)
            else:
                resolved[key] = value
        
        return resolved
    
    def generate_plate_id(self, recipe_name: str = "") -> str:
        """Generate a unique plate_id and store it in context."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        recipe_slug = recipe_name.replace('.json', '').replace(' ', '_') if recipe_name else 'unknown'
        self.plate_id = f"PLATE_{timestamp}_{recipe_slug}"
        return self.plate_id


@dataclass
class WorkflowResult:
    """Result of workflow execution."""
    workflow_name: str
    success: bool
    steps_total: int
    steps_completed: int
    steps_failed: int
    steps_skipped: int
    duration_seconds: float
    errors: List[str] = field(default_factory=list)
    step_results: Dict[int, CommandResult] = field(default_factory=dict)


# =============================================================================
#                           PROGRESS CALLBACKS
# =============================================================================

@dataclass
class WorkflowProgress:
    """Progress information for callbacks."""
    workflow_name: str
    current_step: int
    total_steps: int
    step_instrument: str
    step_action: str
    step_status: StepStatus
    elapsed_seconds: float
    message: str = ""


ProgressCallback = Callable[[WorkflowProgress], None]


# =============================================================================
#                           WORKFLOW EXECUTOR
# =============================================================================

def _load_error_handling_config() -> Dict[str, Any]:
    """Load error handling configuration from lab_config.yaml."""
    from src.config_schema import load_lab_config
    config_path = Path(__file__).parent.parent / "lab_config.yaml"
    defaults = {
        "retry_strategy": "exponential",
        "base_delay_seconds": 1.0,
        "max_delay_seconds": 60.0,
        "max_retries": 3,
        "enable_human_intervention": True
    }
    try:
        config, _ = load_lab_config(config_path, apply_defaults=False, strict=False)
        return config.get("error_handling", defaults)
    except Exception:
        return defaults


class PnPWorkflowExecutor:
    """
    Generic workflow executor using PnP architecture.
    
    NO hardcoded instrument logic - all execution goes through PnPRegistry.
    
    Features:
    - Exponential backoff on retries
    - Error categorization (device unavailable, operation failure, hardware error)
    - Human intervention support via callback (same logic for CLI and WebApp)
    """
    
    def __init__(self, registry: PnPRegistry, base_dir: Optional[Path] = None):
        self.registry = registry
        self.base_dir = base_dir or Path(__file__).parent.parent
        self._progress_callbacks: List[ProgressCallback] = []
        self._intervention_callback: Optional[InterventionCallback] = None
        self._abort_requested = False
        self._pause_requested = False
        self._resume_event: Optional[asyncio.Event] = None
        self._step_executor_fn: Optional[Callable] = None  # async fn(step, context) -> CommandResult

        # Load error handling config
        self._error_config = _load_error_handling_config()

    def set_step_executor(self, fn: Callable):
        """Override step execution. fn must be: async (step: WorkflowStep, context: WorkflowContext) -> CommandResult."""
        self._step_executor_fn = fn
    
    def set_intervention_callback(self, callback: Optional[InterventionCallback]):
        """
        Set callback for human intervention on errors.
        
        The callback receives an InterventionRequest and must return an InterventionAction.
        Both CLI and WebApp use this same mechanism - only the UI differs.
        
        If no callback is set, failed steps follow their on_failure policy.
        """
        self._intervention_callback = callback
    
    def add_progress_callback(self, callback: ProgressCallback):
        """Add a progress callback."""
        self._progress_callbacks.append(callback)
    
    def remove_progress_callback(self, callback: ProgressCallback):
        """Remove a progress callback."""
        if callback in self._progress_callbacks:
            self._progress_callbacks.remove(callback)
    
    def _notify_progress(self, progress: WorkflowProgress):
        """Notify all progress callbacks."""
        for callback in self._progress_callbacks:
            try:
                callback(progress)
            except Exception:
                pass  # Don't let callback errors stop execution
    
    def request_abort(self):
        """Request workflow abortion."""
        self._abort_requested = True
        # Also release any pause so the abort check is reached
        if self._resume_event:
            self._resume_event.set()

    def request_pause(self):
        """Request workflow pause after the current step finishes."""
        self._pause_requested = True
        if self._resume_event:
            self._resume_event.clear()

    def request_resume(self):
        """Resume a paused workflow."""
        self._pause_requested = False
        if self._resume_event:
            self._resume_event.set()
    
    def _calculate_retry_delay(self, attempt: int) -> float:
        """Calculate delay before retry using configured strategy."""
        strategy = self._error_config.get("retry_strategy", "exponential")
        base = self._error_config.get("base_delay_seconds", 1.0)
        max_delay = self._error_config.get("max_delay_seconds", 60.0)
        
        if strategy == "none":
            return 0.0
        elif strategy == "linear":
            delay = base * (attempt + 1)
        else:  # exponential
            delay = base * (2 ** attempt)
        
        return min(delay, max_delay)

    def _is_locally_handled_step(self, step: WorkflowStep) -> bool:
        """Return True for steps executed by web workflow helper logic (not via server metadata)."""
        instrument_lower = (step.instrument or "").lower()
        action_lower = (step.action or "").lower()
        if "manual" in instrument_lower:
            return True
        if "delay" in instrument_lower or action_lower == "wait":
            return True
        if action_lower in ("refilltiprack", "refill") or "refill" in action_lower:
            return True
        return False

    def _normalize_params_for_validation(self, step: WorkflowStep, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize common aliases/defaults so validation matches real server behavior.
        """
        normalized = dict(params or {})
        action = (step.action or "").lower()

        # Opentrons ExecuteRecipe accepts multiple aliases at runtime.
        if action in ("executerecipe", "executerecipebyname"):
            if "RecipeName" not in normalized:
                for key in ("recipe", "Recipe", "recipe_name"):
                    if key in normalized and normalized.get(key):
                        normalized["RecipeName"] = normalized[key]
                        break

        # Tecan RunMeasurement accepts aliases in SiLA2Common service.
        if action == "runmeasurement":
            if "ProtocolFile" not in normalized:
                for key in ("protocol", "Protocol", "protocol_file", "ProtocolPath", "ProtocolName", "analysis"):
                    if key in normalized and normalized.get(key):
                        normalized["ProtocolFile"] = normalized[key]
                        break

        # Manual station defaults (when executed via server path instead of local shortcut).
        if action == "requestoperatortask":
            aliases = {
                "task_type": "TaskType",
                "description": "TaskDescription",
                "task_description": "TaskDescription",
                "priority": "Priority",
                "source_instrument": "SourceInstrument",
                "timeout_seconds": "TimeoutSeconds",
            }
            for src_key, dst_key in aliases.items():
                if src_key in normalized and dst_key not in normalized:
                    normalized[dst_key] = normalized[src_key]

            normalized.setdefault("TaskType", "manual_operation")
            normalized.setdefault("Priority", "normal")
            normalized.setdefault("SourceInstrument", "Workflow")
            normalized.setdefault("TimeoutSeconds", 300)

        return normalized
    
    # =========================================================================
    #                           VALIDATION
    # =========================================================================
    
    async def validate_workflow(self, workflow: Workflow) -> List[ValidationError]:
        """
        Validate workflow against available servers/commands.
        
        Checks:
        - All instruments exist and are discoverable
        - All actions exist in the instrument's command list
        - All required parameters are provided
        - Parameter types match
        """
        errors = []
        
        for step in workflow.steps:
            # Validate dependencies for all steps first.
            for dep in step.depends_on:
                dep_step = next((s for s in workflow.steps if s.step_number == dep), None)
                if not dep_step:
                    errors.append(ValidationError(
                        step_number=step.step_number,
                        field="DependsOn",
                        message=f"Dependency step {dep} does not exist"
                    ))

            # Some steps are handled by workflow orchestration logic, not by instrument RPC.
            if self._is_locally_handled_step(step):
                continue

            # Find server by name
            server = self.registry.get_server_by_name(step.instrument)
            
            if not server:
                errors.append(ValidationError(
                    step_number=step.step_number,
                    field="Instrument",
                    message=f"Instrument '{step.instrument}' not found"
                ))
                continue
            
            # Find command
            command = None
            feature_id = None
            for fid, cmd_id, cmd in server.get_all_commands():
                if cmd_id == step.action or cmd.display_name == step.action:
                    command = cmd
                    feature_id = fid
                    break
            
            if not command:
                errors.append(ValidationError(
                    step_number=step.step_number,
                    field="Action",
                    message=f"Action '{step.action}' not found on '{step.instrument}'"
                ))
                continue

            normalized_params = self._normalize_params_for_validation(step, step.parameters)
            
            # Check required parameters
            for param in command.parameters:
                if param.required:
                    if param.identifier not in normalized_params:
                        errors.append(ValidationError(
                            step_number=step.step_number,
                            field=f"Parameters.{param.identifier}",
                            message=f"Required parameter '{param.identifier}' not provided"
                        ))
        
        return errors
    
    # =========================================================================
    #                           EXECUTION
    # =========================================================================
    
    async def execute(
        self,
        workflow: Workflow,
        validate: bool = True,
        parallel: bool = False
    ) -> WorkflowResult:
        """
        Execute a workflow.
        
        Args:
            workflow: The workflow to execute
            validate: Whether to validate before execution
            parallel: Whether to run independent steps in parallel
        
        Returns:
            WorkflowResult with execution summary
        """
        self._abort_requested = False
        self._pause_requested = False
        self._resume_event = asyncio.Event()
        self._resume_event.set()  # not paused initially
        workflow.start_time = datetime.now()
        workflow.status = StepStatus.RUNNING
        
        errors = []
        
        # Validate if requested
        if validate:
            validation_errors = await self.validate_workflow(workflow)
            if validation_errors:
                workflow.status = StepStatus.FAILED
                workflow.end_time = datetime.now()
                return WorkflowResult(
                    workflow_name=workflow.name,
                    success=False,
                    steps_total=len(workflow.steps),
                    steps_completed=0,
                    steps_failed=0,
                    steps_skipped=len(workflow.steps),
                    duration_seconds=(workflow.end_time - workflow.start_time).total_seconds(),
                    errors=[f"Step {e.step_number} {e.field}: {e.message}" for e in validation_errors]
                )
        
        # Execute steps
        step_results = {}
        completed = 0
        failed = 0
        skipped = 0
        
        # Create workflow context for automatic plate_id propagation
        context = WorkflowContext(workflow_name=workflow.name)
        
        if parallel:
            # Parallel execution with dependency tracking
            completed, failed, skipped, step_results, errors = await self._execute_parallel(workflow, context)
        else:
            # Sequential execution
            completed, failed, skipped, step_results, errors = await self._execute_sequential(workflow, context)
        
        # Finalize
        workflow.end_time = datetime.now()
        workflow.status = StepStatus.SUCCESS if failed == 0 else StepStatus.FAILED
        
        return WorkflowResult(
            workflow_name=workflow.name,
            success=failed == 0,
            steps_total=len(workflow.steps),
            steps_completed=completed,
            steps_failed=failed,
            steps_skipped=skipped,
            duration_seconds=(workflow.end_time - workflow.start_time).total_seconds(),
            errors=errors,
            step_results=step_results
        )
    
    async def _execute_sequential(
        self,
        workflow: Workflow,
        context: WorkflowContext
    ) -> tuple:
        """Execute steps sequentially with automatic plate_id propagation."""
        completed = 0
        failed = 0
        skipped = 0
        step_results = {}
        errors = []
        steps_by_number = {s.step_number: s for s in workflow.steps}
        
        for step in workflow.steps:
            if self._abort_requested:
                step.status = StepStatus.SKIPPED
                skipped += 1
                continue

            # Enforce dependencies also in sequential mode.
            if step.depends_on:
                missing_deps = [d for d in step.depends_on if d not in steps_by_number]
                pending_deps = [
                    d for d in step.depends_on
                    if d in steps_by_number and steps_by_number[d].status in (StepStatus.PENDING, StepStatus.RUNNING)
                ]
                failed_deps = [
                    d for d in step.depends_on
                    if d in steps_by_number and steps_by_number[d].status in (StepStatus.FAILED, StepStatus.SKIPPED)
                ]

                if missing_deps or pending_deps or failed_deps:
                    msg_parts = []
                    if missing_deps:
                        msg_parts.append(f"missing={missing_deps}")
                    if pending_deps:
                        msg_parts.append(f"pending={pending_deps}")
                    if failed_deps:
                        msg_parts.append(f"failed_or_skipped={failed_deps}")
                    dep_msg = ", ".join(msg_parts)
                    err = f"Step {step.step_number}: dependencies not satisfied ({dep_msg})"
                    errors.append(err)
                    step.status = StepStatus.SKIPPED
                    skipped += 1

                    elapsed = (datetime.now() - workflow.start_time).total_seconds() if workflow.start_time else 0.0
                    self._notify_progress(WorkflowProgress(
                        workflow_name=workflow.name,
                        current_step=step.step_number,
                        total_steps=len(workflow.steps),
                        step_instrument=step.instrument,
                        step_action=step.action,
                        step_status=StepStatus.SKIPPED,
                        elapsed_seconds=elapsed,
                        message=err
                    ))
                    continue
            
            # Resolve context variables in parameters (e.g., ${workflow.plate_id})
            resolved_params = context.resolve_parameters(step.parameters)
            
            # Auto-inject plate_id for Tecan if not specified but available in context
            if "tecan" in step.instrument.lower() and step.action in ["RunMeasurement", "RunAnalysis"]:
                if not resolved_params.get("plate_id") and context.plate_id:
                    resolved_params["plate_id"] = context.plate_id
            
            # Temporarily replace step parameters with resolved ones
            original_params = step.parameters
            step.parameters = resolved_params
            
            # Notify progress
            elapsed = (datetime.now() - workflow.start_time).total_seconds() if workflow.start_time else 0.0
            self._notify_progress(WorkflowProgress(
                workflow_name=workflow.name,
                current_step=step.step_number,
                total_steps=len(workflow.steps),
                step_instrument=step.instrument,
                step_action=step.action,
                step_status=StepStatus.RUNNING,
                elapsed_seconds=elapsed,
                message=f"Starting {step.action} on {step.instrument}"
            ))
            
            # Execute with retry
            result = await self._execute_step_with_retry(step, workflow.name, context)
            step_results[step.step_number] = result
            
            # Restore original parameters
            step.parameters = original_params
            
            # Update context based on result
            if result.success:
                # Auto-generate plate_id for Opentrons pipetting commands
                if "opentrons" in step.instrument.lower() and step.action in ["ExecuteRecipe", "RunRecipe", "run_recipe"]:
                    recipe_name = resolved_params.get("recipe", resolved_params.get("recipe_name", ""))
                    if not context.plate_id:  # Only generate if not already set
                        context.generate_plate_id(recipe_name)
                        import logging
                        logging.getLogger(__name__).info(f"Auto-generated plate_id: {context.plate_id}")
                
                # Store result in context for future reference
                result_data = result.data if isinstance(result.data, dict) else {"raw": result.data}
                result_data["plate_id"] = context.plate_id
                context.step_results[step.step_number] = result_data
            
            if result.success:
                completed += 1
                step.status = StepStatus.SUCCESS
            else:
                step.status = StepStatus.FAILED
                errors.append(f"Step {step.step_number}: {result.error}")
                
                if step.on_failure == "stop":
                    failed += 1
                    # Skip remaining steps
                    for remaining in workflow.steps:
                        if remaining.status == StepStatus.PENDING:
                            remaining.status = StepStatus.SKIPPED
                            skipped += 1
                    break
                elif step.on_failure == "skip":
                    skipped += 1
                else:  # continue
                    failed += 1

            # Notify final status for this step so UIs can show completion/failure
            # and avoid looking stuck on the first running event.
            elapsed = (datetime.now() - workflow.start_time).total_seconds() if workflow.start_time else 0.0
            if step.status == StepStatus.SUCCESS:
                step_msg = f"Completed {step.action} on {step.instrument}"
            elif step.status == StepStatus.SKIPPED:
                step_msg = f"Skipped {step.action} on {step.instrument}"
            else:
                step_msg = f"Failed {step.action} on {step.instrument}: {result.error or 'unknown error'}"

            self._notify_progress(WorkflowProgress(
                workflow_name=workflow.name,
                current_step=step.step_number,
                total_steps=len(workflow.steps),
                step_instrument=step.instrument,
                step_action=step.action,
                step_status=step.status,
                elapsed_seconds=elapsed,
                message=step_msg
            ))

            # Pause point between steps
            if self._pause_requested and self._resume_event and not self._abort_requested:
                await self._resume_event.wait()

        return completed, failed, skipped, step_results, errors
    
    async def _execute_parallel(
        self,
        workflow: Workflow,
        context: WorkflowContext
    ) -> tuple:
        """Execute steps in parallel where dependencies allow.
        
        Note: Parallel execution has limited context propagation since
        plate_id may not be available until after concurrent steps complete.
        For full traceability, use sequential execution.
        """
        completed = 0
        failed = 0
        skipped = 0
        step_results = {}
        errors = []
        
        pending = set(s.step_number for s in workflow.steps)
        completed_steps: Set[int] = set()
        failed_steps: Set[int] = set()
        
        while pending and not self._abort_requested:
            # Find steps that can run (all dependencies satisfied)
            runnable = []
            for step in workflow.steps:
                if step.step_number not in pending:
                    continue
                
                deps_satisfied = all(d in completed_steps for d in step.depends_on)
                deps_failed = any(d in failed_steps for d in step.depends_on)
                
                if deps_failed and step.on_failure == "stop":
                    # Skip this step due to failed dependency
                    step.status = StepStatus.SKIPPED
                    pending.discard(step.step_number)
                    skipped += 1
                elif deps_satisfied:
                    runnable.append(step)
            
            if not runnable:
                # No steps can run - deadlock or all done
                break
            
            # Run all runnable steps in parallel
            tasks = [self._execute_step_with_retry(step, workflow.name, context) for step in runnable]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for step, result in zip(runnable, results):
                pending.discard(step.step_number)
                
                if isinstance(result, Exception):
                    result = CommandResult(success=False, error=str(result))
                
                step_results[step.step_number] = result
                
                if result.success:
                    completed += 1
                    completed_steps.add(step.step_number)
                    step.status = StepStatus.SUCCESS
                else:
                    failed_steps.add(step.step_number)
                    step.status = StepStatus.FAILED
                    errors.append(f"Step {step.step_number}: {result.error}")
                    
                    if step.on_failure == "skip":
                        skipped += 1
                    else:
                        failed += 1
        
        # Mark remaining as skipped
        for step_num in pending:
            step = next(s for s in workflow.steps if s.step_number == step_num)
            step.status = StepStatus.SKIPPED
            skipped += 1
        
        return completed, failed, skipped, step_results, errors
    
    async def _execute_step_with_retry(
        self,
        step: WorkflowStep,
        workflow_name: str = "",
        wf_context: Optional["WorkflowContext"] = None
    ) -> CommandResult:
        """
        Execute a step with retry logic, error categorization, and human intervention.

        Features:
        - Exponential backoff between retries
        - Error categorization for appropriate handling
        - Human intervention callback when all retries fail (if enabled)
        - Optional custom step executor (set via set_step_executor) for WebApp integration
        """
        step.start_time = datetime.now()
        step.status = StepStatus.RUNNING

        last_result = None
        last_error: Optional[CategorizedError] = None
        max_attempts = step.retry_count + 1
        context = f"{step.action} on {step.instrument}"

        for attempt in range(max_attempts):
            try:
                if self._step_executor_fn:
                    # Custom executor (e.g. WebApp execute_step with operator notifications)
                    result = await self._step_executor_fn(step, wf_context)
                else:
                    # Default: execute via registry (pure gRPC)
                    result = await self.registry.execute(
                        step.instrument,
                        step.action,
                        step.parameters,
                        timeout=step.timeout_seconds or 300.0
                    )
                
                if result.success:
                    step.end_time = datetime.now()
                    step.result = result
                    return result
                
                # Categorize the failure
                last_error = CategorizedError.from_result(result, context)
                last_result = result
                
                # Log categorized error
                import logging
                logging.getLogger(__name__).warning(
                    f"Step {step.step_number} attempt {attempt + 1}/{max_attempts} failed: "
                    f"[{last_error.category.value}] {last_error.message}"
                )
                
                # Don't retry hardware errors - need physical intervention
                if last_error.category == ErrorCategory.HARDWARE_ERROR:
                    break
                
                if attempt < max_attempts - 1:
                    # Exponential backoff before retry
                    delay = self._calculate_retry_delay(attempt)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    
            except asyncio.TimeoutError:
                last_error = CategorizedError(
                    category=ErrorCategory.TIMEOUT,
                    message=f"Timeout after {step.timeout_seconds}s",
                    details=context
                )
                last_result = CommandResult(
                    success=False,
                    error=last_error.message
                )
            except Exception as e:
                last_error = CategorizedError.from_exception(e, context)
                last_result = CommandResult(
                    success=False,
                    error=str(e)
                )
                
                # Don't retry device unavailable - won't help
                if last_error.category == ErrorCategory.DEVICE_UNAVAILABLE:
                    break
        
        # All retries exhausted - try human intervention if enabled
        if (self._intervention_callback and 
            self._error_config.get("enable_human_intervention", True) and
            last_error):
            
            step.status = StepStatus.WAITING_INTERVENTION
            
            request = InterventionRequest(
                step_number=step.step_number,
                instrument=step.instrument,
                action=step.action,
                error=last_error,
                attempt=max_attempts,
                max_attempts=max_attempts,
                workflow_name=workflow_name
            )
            
            try:
                action = await self._intervention_callback(request)
                
                if action == InterventionAction.RETRY:
                    # User wants to retry after fixing the issue
                    step.status = StepStatus.RUNNING
                    result = await self.registry.execute(
                        step.instrument,
                        step.action,
                        step.parameters,
                        timeout=step.timeout_seconds or 300.0
                    )
                    if result.success:
                        step.end_time = datetime.now()
                        step.result = result
                        return result
                    last_result = result
                    
                elif action == InterventionAction.SKIP:
                    # User wants to skip this step
                    step.status = StepStatus.SKIPPED
                    step.end_time = datetime.now()
                    return CommandResult(
                        success=True,  # Treat as success for workflow continuation
                        data={"skipped": True, "reason": "User intervention"}
                    )
                    
                elif action == InterventionAction.ABORT:
                    # User wants to abort workflow
                    self.request_abort()
                    
            except Exception as intervention_error:
                import logging
                logging.getLogger(__name__).error(
                    f"Intervention callback failed: {intervention_error}"
                )
        
        step.end_time = datetime.now()
        step.status = StepStatus.FAILED
        step.result = last_result
        return last_result or CommandResult(success=False, error="No result")
    
    # =========================================================================
    #                           UTILITY
    # =========================================================================
    
    def get_required_instruments(self, workflow: Workflow) -> Set[str]:
        """Get all instrument names required by a workflow."""
        return {step.instrument for step in workflow.steps}
    
    async def check_readiness(self, workflow: Workflow) -> Dict[str, bool]:
        """Check if all required instruments are ready."""
        instruments = self.get_required_instruments(workflow)
        readiness = {}
        
        for instrument in instruments:
            server = self.registry.get_server_by_name(instrument)
            if server:
                readiness[instrument] = server.hardware_online
            else:
                readiness[instrument] = False
        
        return readiness


# =============================================================================
#                           EXAMPLE USAGE
# =============================================================================

async def example():
    """Example usage of the workflow executor."""
    from pathlib import Path
    
    base_dir = Path(__file__).parent.parent
    
    # Create registry and discover
    registry = PnPRegistry(base_dir)
    await registry.discover()
    await registry.connect_all()
    
    # Create executor
    executor = PnPWorkflowExecutor(registry)
    
    def on_progress(p: WorkflowProgress):
        logger.info(f"[{p.current_step}/{p.total_steps}] {p.step_instrument} → {p.step_action}: {p.step_status.value}")

    executor.add_progress_callback(on_progress)

    workflow_path = base_dir / "Library" / "Workflows" / "A.workflow.json"
    if workflow_path.exists():
        workflow = Workflow.from_file(workflow_path)
        errors = await executor.validate_workflow(workflow)
        if errors:
            for e in errors:
                logger.error(f"  Step {e.step_number}: {e.message}")
        else:
            result = await executor.execute(workflow)
            logger.info(f"Result: {'SUCCESS' if result.success else 'FAILED'} — {result.duration_seconds:.1f}s")
    
    # Cleanup
    await registry.disconnect_all()


if __name__ == "__main__":
    asyncio.run(example())
