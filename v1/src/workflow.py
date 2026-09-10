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
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Awaitable


logger = logging.getLogger(__name__)

from src.discovery import PnPServer
from src.client import PnPRegistry, CommandResult
from src.resolver import resolve
from src.settings import get_settings, normalize_timeout


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
    retry_count: Optional[int] = None  # None = use error_handling.max_retries from config
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
                retry_count=step_data.get("RetryCount", None),
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

#: Bumped when the on-disk workflow format changes in a way a reader must
#: notice. Files without it are pre-versioned and read with the same rules.
SCHEMA_VERSION = "1.0"

#: Keys the Workflow Builder attaches to a step that are not SiLA2 parameters.
#: The client drops them before dispatch; validation must not call them typos.
_WORKFLOW_ONLY_PARAM_KEYS = frozenset({
    "hal_config", "HALConfig", "UseHAL", "seconds", "rack_type", "notes",
})


def topological_waves(steps: List["WorkflowStep"]) -> List[List["WorkflowStep"]]:
    """Group steps into dependency waves.

    Wave 0 holds every step whose dependencies are already satisfied (usually:
    that has none), wave 1 everything that depends only on wave 0, and so on.
    Steps inside a wave are independent of each other, so they may run
    concurrently, and they keep the order they were written in so sequential
    execution stays predictable.

    This is the single ordering used by both execution modes. It exists because
    sequential execution used to walk the file top to bottom and mark a step
    SKIPPED when its dependency happened to be written further down - a valid
    workflow silently lost steps because of how it was typed.

    A dependency on a step number that does not exist is ignored here (workflow
    validation reports it as an error before execution). Steps left over after
    no further wave can be formed are in a cycle; they are returned as a final
    wave so nothing vanishes, and validation reports the cycle.
    """
    remaining = {s.step_number: s for s in steps}
    order = {s.step_number: i for i, s in enumerate(steps)}
    known = set(remaining)
    satisfied: Set[int] = set()
    waves: List[List[WorkflowStep]] = []

    while remaining:
        wave = [
            step for step in remaining.values()
            if all(dep in satisfied or dep not in known for dep in step.depends_on)
        ]
        if not wave:
            # Cycle: emit whatever is left, in file order, so the caller still
            # sees every step. validate_workflow() names the cycle.
            waves.append(sorted(remaining.values(), key=lambda s: order[s.step_number]))
            break
        wave.sort(key=lambda s: order[s.step_number])
        waves.append(wave)
        for step in wave:
            satisfied.add(step.step_number)
            del remaining[step.step_number]

    return waves


def find_dependency_cycles(steps: List["WorkflowStep"]) -> List[List[int]]:
    """Return one representative cycle per strongly-connected dependency loop.

    Without this a cycle is not an error at all: the wave scheduler simply
    never finds those steps runnable and the run ends early with them marked
    skipped, which reads like a scheduling bug rather than a broken file.
    """
    edges = {s.step_number: [d for d in s.depends_on] for s in steps}
    cycles: List[List[int]] = []
    seen_signatures = set()
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {number: WHITE for number in edges}

    def walk(node: int, path: List[int]) -> None:
        colour[node] = GREY
        path.append(node)
        for dep in edges.get(node, []):
            if dep not in colour:
                continue
            if colour[dep] is GREY:
                cycle = path[path.index(dep):] + [dep]
                signature = frozenset(cycle)
                if signature not in seen_signatures:
                    seen_signatures.add(signature)
                    cycles.append(cycle)
            elif colour[dep] is WHITE:
                walk(dep, path)
        path.pop()
        colour[node] = BLACK

    for number in list(edges):
        if colour[number] is WHITE:
            walk(number, [])
    return cycles


def is_locally_handled(instrument: str, action: str = "") -> bool:
    """True for steps that have no SiLA2 server behind them.

    Delay, manual-operator and tip-refill steps are executed by the
    orchestrator itself, so they must not be resolved against discovered
    instruments and must not fail a pre-flight instrument check for being
    absent. Shared with the webapp so both agree on what "local" means.
    """
    instrument_lower = (instrument or "").lower()
    action_lower = (action or "").lower()
    if "manual" in instrument_lower:
        return True
    if "delay" in instrument_lower or action_lower == "wait":
        return True
    if "refill" in instrument_lower:
        return True
    if action_lower in ("refilltiprack", "refill") or "refill" in action_lower:
        return True
    return False


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
        #: step_number -> (step, task) for steps currently awaiting an instrument.
        #: Abort needs this: without it there is nothing to interrupt and nothing
        #: to tell the instrument about.
        self._inflight: Dict[int, Tuple["WorkflowStep", "asyncio.Task"]] = {}
        #: What the last abort actually managed to do, surfaced to the operator.
        self.last_abort_report: List[str] = []

        # One shared settings snapshot instead of re-reading lab_config.yaml.
        # Note the behaviour change this fixes: the old loader turned a
        # default_timeout of null into a hard 300 s, silently contradicting the
        # documented "0 or null means unlimited" and capping long protocols.
        _settings = get_settings(self.base_dir)
        self._error_config = _settings.error_handling
        self._default_step_timeout = _settings.workflow.default_timeout

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
    
    #: Command identifiers that mean "stop what you are doing", most specific
    #: first. Never hardcoded per instrument: the executor asks each server
    #: which of these it declares in its own FDL, so a new instrument only has
    #: to expose one of them to become abortable.
    ABORT_COMMANDS = ("AbortRun", "CancelTask", "Abort", "Cancel", "Stop", "Halt", "EmergencyStop")

    #: An abort must not itself hang. Bounded even though normal commands are
    #: not: if the instrument cannot acknowledge a stop in this long, the
    #: operator needs to be told to intervene physically, not kept waiting.
    ABORT_TIMEOUT_S = 30.0

    def request_abort(self):
        """Request workflow abortion.

        Sets the flag every loop checks, and — the part that was missing —
        schedules the work that actually stops the hardware. Previously this
        set a boolean read *between* steps, so during a two-hour ExecuteRecipe
        it did nothing at all and the robot was never told anything: the
        operator pressed Abort, the UI said "aborted", and the run carried on.
        """
        self._abort_requested = True
        # Release any pause so the abort check is reached.
        if self._resume_event:
            self._resume_event.set()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop (a synchronous caller): the flag alone still stops the
            # run at the next step boundary.
            return
        loop.create_task(self._perform_abort())

    async def _perform_abort(self) -> List[str]:
        """Tell every instrument mid-step to stop, then stop waiting on it."""
        report: List[str] = []
        inflight = list(self._inflight.values())
        if not inflight:
            report.append("No step was executing")

        for step, _task in inflight:
            command = self._find_abort_command(step.instrument)
            if command is None:
                report.append(
                    f"{step.instrument}: no abort command exposed — "
                    f"stop it at the instrument"
                )
                continue
            try:
                result = await self.registry.execute(
                    step.instrument, command, {}, timeout=self.ABORT_TIMEOUT_S
                )
                if result.success:
                    report.append(f"{step.instrument}: {command} sent")
                else:
                    report.append(f"{step.instrument}: {command} FAILED ({result.error}) — verify manually")
            except Exception as exc:
                report.append(f"{step.instrument}: {command} raised {exc} — verify manually")

        # Only now stop awaiting: the instrument has been told first, so a
        # cancelled wait cannot leave hardware running with nobody watching.
        for _step, task in inflight:
            if task is not None and not task.done():
                task.cancel()

        self.last_abort_report = report
        for line in report:
            logger.warning("Abort: %s", line)
        return report

    def _find_abort_command(self, instrument: str) -> Optional[str]:
        """The instrument's own stop command, from its FDL. None if it has none."""
        server = self.registry.get_server_by_name(instrument)
        if server is None:
            return None
        available = {cmd_id for _fid, cmd_id, _cmd in server.get_all_commands()}
        return next((c for c in self.ABORT_COMMANDS if c in available), None)

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
        return self._error_config.retry_delay(attempt)

    def _is_locally_handled_step(self, step: WorkflowStep) -> bool:
        """Return True for steps the orchestrator executes itself."""
        return is_locally_handled(step.instrument, step.action)

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

        # Tecan RunMeasurement: normalize parameter name aliases.
        if action == "runmeasurement":
            if "ProtocolFile" not in normalized:
                for key in ("protocol", "Protocol", "protocol_file", "ProtocolPath", "ProtocolName", "analysis"):
                    if key in normalized and normalized.get(key):
                        normalized["ProtocolFile"] = normalized[key]
                        break
            # PlateID is auto-injected from Opentrons context at execution time;
            # SampleSetID and PlateType default to empty string when not provided.
            normalized.setdefault("PlateID", "")
            normalized.setdefault("SampleSetID", "")
            normalized.setdefault("PlateType", "")

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

        # Duplicate StepNumbers make DependsOn ambiguous and silently drop steps
        # from the dependency map, so catch them before anything else.
        seen_numbers: Dict[int, int] = {}
        for step in workflow.steps:
            seen_numbers[step.step_number] = seen_numbers.get(step.step_number, 0) + 1
        for number, count in seen_numbers.items():
            if count > 1:
                errors.append(ValidationError(
                    step_number=number,
                    field="StepNumber",
                    message=f"StepNumber {number} is used by {count} steps; each must be unique",
                ))

        # A dependency cycle would otherwise present at runtime as steps that
        # simply never become runnable — a silent hang, not an error.
        for cycle in find_dependency_cycles(workflow.steps):
            errors.append(ValidationError(
                step_number=cycle[0],
                field="DependsOn",
                message=f"Dependency cycle: {' -> '.join(str(n) for n in cycle)}",
            ))

        known_numbers = {s.step_number for s in workflow.steps}
        for step in workflow.steps:
            # Validate dependencies for all steps first.
            for dep in step.depends_on:
                if dep not in known_numbers:
                    errors.append(ValidationError(
                        step_number=step.step_number,
                        field="DependsOn",
                        message=f"Dependency step {dep} does not exist"
                    ))
                elif dep == step.step_number:
                    errors.append(ValidationError(
                        step_number=step.step_number,
                        field="DependsOn",
                        message="A step cannot depend on itself",
                    ))

            # Some steps are handled by workflow orchestration logic, not by instrument RPC.
            if self._is_locally_handled_step(step):
                continue

            if not step.action:
                errors.append(ValidationError(
                    step_number=step.step_number,
                    field="Action",
                    message="No Action given",
                ))
                continue

            # Resolve the instrument through the shared resolver, so an
            # ambiguous name is reported rather than silently decided.
            match = resolve(
                step.instrument,
                [(key, srv.name, srv) for key, srv in self.registry.servers.items()],
            )
            if not match:
                errors.append(ValidationError(
                    step_number=step.step_number,
                    field="Instrument",
                    message=match.error or f"Instrument '{step.instrument}' not found",
                ))
                continue
            server = match.target
            
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
            
            # Check required parameters — only flag parameters that are completely
            # absent (key not in params at all). Empty string is a valid value for
            # String-type params (e.g. PlateID auto-injected at runtime, SampleSetID
            # optional), so we do not reject params where the value is "".
            for param in command.parameters:
                if param.required:
                    value = normalized_params.get(param.identifier)
                    if value is None:
                        errors.append(ValidationError(
                            step_number=step.step_number,
                            field=f"Parameters.{param.identifier}",
                            message=f"Required parameter '{param.identifier}' not provided"
                        ))

            declared = {p.identifier for p in command.parameters}
            if declared:
                unknown = [
                    key for key in normalized_params
                    if key not in declared and key not in _WORKFLOW_ONLY_PARAM_KEYS
                ]
                if unknown:
                    errors.append(ValidationError(
                        step_number=step.step_number,
                        field="Parameters",
                        message=(
                            f"Unknown parameter(s) {', '.join(sorted(unknown))} for "
                            f"'{step.action}'. Declared: {', '.join(sorted(declared)) or 'none'}"
                        ),
                    ))

            # Well-capacity check for ExecuteRecipe steps
            action_lower = (step.action or "").lower()
            if action_lower in ("executerecipe", "executerecipebyname"):
                cap_errors = self._validate_recipe_capacity(step, normalized_params)
                errors.extend(cap_errors)

        return errors

    def _validate_recipe_capacity(
        self, step: "WorkflowStep", params: Dict[str, Any]
    ) -> List["ValidationError"]:
        """
        Check that the recipe does not target more wells than the plate can hold.
        Returns a list of ValidationErrors (empty = OK).
        """
        errors: List[ValidationError] = []
        recipe_name = params.get("RecipeName", "")
        if not recipe_name:
            return errors
        try:
            import json as _json

            recipe_path = self.base_dir / "Library" / "Recipes" / f"{recipe_name}.json"
            if not recipe_path.exists():
                return errors
            recipe = _json.loads(recipe_path.read_text(encoding="utf-8-sig"))

            # Collect all distinct destination wells per target labware
            wells_by_labware: Dict[str, set] = {}
            _cmds = {"Transfer", "TransferWithLiquidClass", "Distribute", "Dispense"}
            for s in recipe.get("Steps", []):
                if s.get("Command") not in _cmds:
                    continue
                dests = s.get("Destinations") or s.get("Destination") or s.get("Dest") or []
                if isinstance(dests, str):
                    dests = [dests]
                for dest in dests:
                    if ":" not in dest:
                        continue
                    labware, well = dest.split(":", 1)
                    wells_by_labware.setdefault(labware, set()).add(well)

            # Resolve plate capacity: check HAL first, then recipe's own Labware section
            plate_total_wells: Dict[str, int] = {}

            # Check recipe's inline Labware definitions
            for logical, lw_def in recipe.get("Labware", {}).items():
                load_name = lw_def.get("LoadName", "")
                if "wellplate" in load_name.lower():
                    pj = (self.base_dir / "Library" / "Labware" / "Plates"
                          / f"{load_name}.plate.json")
                    if pj.exists():
                        meta = _json.loads(pj.read_text(encoding="utf-8-sig"))
                        plate_total_wells[logical] = int(meta.get("total_wells", 0))

            # Fall back to active HAL config
            if not plate_total_wells:
                active_file = self.base_dir / "Library" / "HardwareConfig" / ".active"
                if active_file.exists():
                    hal_name = active_file.read_text(encoding="utf-8-sig").strip()
                    hal_path = self.base_dir / "Library" / "HardwareConfig" / hal_name
                    if not hal_path.exists():
                        hal_path = hal_path.with_suffix(".json")
                    if hal_path.exists():
                        hal = _json.loads(hal_path.read_text(encoding="utf-8-sig"))
                        for logical, lw_def in hal.get("Labware", {}).items():
                            load_name = lw_def.get("LoadName", "")
                            if "wellplate" in load_name.lower():
                                pj = (self.base_dir / "Library" / "Labware" / "Plates"
                                      / f"{load_name}.plate.json")
                                if pj.exists():
                                    meta = _json.loads(pj.read_text(encoding="utf-8-sig"))
                                    plate_total_wells[logical] = int(meta.get("total_wells", 0))

            for labware, well_set in wells_by_labware.items():
                capacity = plate_total_wells.get(labware)
                if capacity and len(well_set) > capacity:
                    errors.append(ValidationError(
                        step_number=step.step_number,
                        field="Parameters.RecipeName",
                        message=(
                            f"Recipe '{recipe_name}' targets {len(well_set)} wells "
                            f"on '{labware}' but plate capacity is {capacity}"
                        ),
                    ))
        except Exception:
            pass  # capacity check is advisory — never block on parse errors
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
    
    async def _execute_sequential(self, workflow: Workflow, context: WorkflowContext) -> tuple:
        """Run one step at a time, in dependency order."""
        return await self._execute_waves(workflow, context, parallel=False)

    async def _execute_parallel(self, workflow: Workflow, context: WorkflowContext) -> tuple:
        """Run each dependency wave concurrently."""
        return await self._execute_waves(workflow, context, parallel=True)

    async def _execute_waves(
        self,
        workflow: Workflow,
        context: WorkflowContext,
        parallel: bool,
    ) -> tuple:
        """Execute a workflow in topological waves.

        Both execution modes share this one ordering. They used to be separate
        implementations, and the sequential one simply iterated the steps in
        file order: a step whose dependency appeared *later* in the file was
        marked SKIPPED rather than reordered, so a perfectly valid workflow
        silently lost steps depending on the order they happened to be written
        in. Wave 0 is every step with no dependencies, wave 1 is everything
        whose dependencies are all in wave 0, and so on; within a wave, file
        order is preserved so two independent steps still run in the order the
        author wrote them.

        `parallel` only changes how a single wave is dispatched - concurrently,
        or one step after another - never the ordering.
        """
        completed = failed = skipped = 0
        step_results: Dict[int, CommandResult] = {}
        errors: List[str] = []

        waves = topological_waves(workflow.steps)
        done: Set[int] = set()
        broken: Set[int] = set()

        def _elapsed() -> float:
            if not workflow.start_time:
                return 0.0
            return (datetime.now() - workflow.start_time).total_seconds()

        def _notify(step: WorkflowStep, status: StepStatus, message: str) -> None:
            self._notify_progress(WorkflowProgress(
                workflow_name=workflow.name,
                current_step=step.step_number,
                total_steps=len(workflow.steps),
                step_instrument=step.instrument,
                step_action=step.action,
                step_status=status,
                elapsed_seconds=_elapsed(),
                message=message,
            ))

        def _skip(step: WorkflowStep, reason: str) -> None:
            nonlocal skipped
            step.status = StepStatus.SKIPPED
            skipped += 1
            broken.add(step.step_number)
            errors.append(reason)
            _notify(step, StepStatus.SKIPPED, reason)

        halt_reason: Optional[str] = None
        for wave in waves:
            if halt_reason or self._abort_requested:
                break

            runnable: List[WorkflowStep] = []
            for step in wave:
                unmet = [d for d in step.depends_on if d in broken or d not in done]
                if unmet:
                    # A dependency failed or was skipped. "stop"/"skip" both mean
                    # this step must not run; "continue" is the author explicitly
                    # saying the step does not really need its predecessor.
                    if step.on_failure == "continue":
                        runnable.append(step)
                    else:
                        _skip(step, f"Step {step.step_number} skipped: "
                                    f"dependencies not satisfied ({unmet})")
                else:
                    runnable.append(step)

            if not runnable:
                continue

            if parallel and len(runnable) > 1:
                for step in runnable:
                    _notify(step, StepStatus.RUNNING,
                            f"Starting {step.action} on {step.instrument}")
                outcomes = await asyncio.gather(
                    *[self._run_one(step, workflow, context) for step in runnable],
                    return_exceptions=True,
                )
            else:
                outcomes = []
                for step in runnable:
                    if self._abort_requested:
                        outcomes.append(None)
                        continue
                    _notify(step, StepStatus.RUNNING,
                            f"Starting {step.action} on {step.instrument}")
                    try:
                        outcomes.append(await self._run_one(step, workflow, context))
                    except asyncio.CancelledError:
                        outcomes.append(CommandResult(success=False, error="Aborted by operator"))
                    except Exception as exc:  # pragma: no cover - defensive
                        outcomes.append(exc)
                    # Pause takes effect between steps; a wave dispatched
                    # concurrently cannot be interrupted mid-wave.
                    if self._pause_requested and self._resume_event and not self._abort_requested:
                        await self._resume_event.wait()

            for step, outcome in zip(runnable, outcomes):
                if outcome is None:  # aborted before it started
                    _skip(step, f"Step {step.step_number} skipped: workflow aborted")
                    continue
                if isinstance(outcome, BaseException):
                    outcome = CommandResult(success=False, error=str(outcome))

                step_results[step.step_number] = outcome

                if outcome.success:
                    completed += 1
                    done.add(step.step_number)
                    step.status = StepStatus.SUCCESS
                    _notify(step, StepStatus.SUCCESS,
                            f"Completed {step.action} on {step.instrument}")
                    continue

                step.status = StepStatus.FAILED
                broken.add(step.step_number)
                error_text = outcome.error or "unknown error"
                errors.append(f"Step {step.step_number}: {error_text}")
                _notify(step, StepStatus.FAILED,
                        f"Failed {step.action} on {step.instrument}: {error_text}")

                if step.on_failure == "skip":
                    skipped += 1
                elif step.on_failure == "continue":
                    failed += 1
                else:  # "stop"
                    failed += 1
                    halt_reason = (
                        f"workflow halted after step {step.step_number} "
                        f"({step.action} on {step.instrument}) failed"
                    )

        # Anything never reached: aborted, or downstream of a halting failure.
        # Always say why. A step marked "skipped" with no reason attached is one
        # of the things that makes a failed run hard to read after the fact.
        if self._abort_requested:
            not_reached = "workflow aborted by operator"
        else:
            not_reached = halt_reason or "not reached"
        for step in workflow.steps:
            if step.status == StepStatus.PENDING:
                step.status = StepStatus.SKIPPED
                skipped += 1
                reason = f"Step {step.step_number} skipped: {not_reached}"
                errors.append(reason)
                _notify(step, StepStatus.SKIPPED, reason)

        return completed, failed, skipped, step_results, errors

    async def _run_one(
        self,
        step: WorkflowStep,
        workflow: Workflow,
        context: WorkflowContext,
    ) -> CommandResult:
        """Resolve a step's parameters against the run context, execute it, and
        feed the result back into the context.

        Context propagation (a plate id generated by an Opentrons run being
        picked up by a later Tecan read) used to exist only in the sequential
        path, so the same workflow tracked plates or not depending on a config
        flag. It now happens for both.
        """
        resolved = context.resolve_parameters(step.parameters)

        # Carry a known plate id into a measurement that did not name one.
        if "tecan" in step.instrument.lower() and step.action in ("RunMeasurement", "RunAnalysis"):
            if not resolved.get("plate_id") and context.plate_id:
                resolved["plate_id"] = context.plate_id

        original = step.parameters
        step.parameters = resolved
        task = asyncio.current_task()
        self._inflight[step.step_number] = (step, task)
        try:
            result = await self._execute_step_with_retry(step, workflow.name, context)
        except asyncio.CancelledError:
            # Cancelled by _perform_abort() after the instrument was told to
            # stop. Report it as a failed step rather than letting the
            # cancellation tear down the whole executor.
            if not self._abort_requested:
                raise
            return CommandResult(success=False, error="Aborted by operator")
        finally:
            step.parameters = original
            self._inflight.pop(step.step_number, None)

        if result.success:
            if ("opentrons" in step.instrument.lower()
                    and step.action in ("ExecuteRecipe", "RunRecipe", "run_recipe")
                    and not context.plate_id):
                recipe_name = resolved.get("RecipeName") or resolved.get("recipe") or ""
                context.generate_plate_id(recipe_name)
                logger.info(f"Auto-generated plate_id: {context.plate_id}")

            result_data = result.data if isinstance(result.data, dict) else {}
            result_data["plate_id"] = context.plate_id
            context.step_results[step.step_number] = result_data

        return result

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
        retry_count = step.retry_count if step.retry_count is not None else self._error_config.max_retries
        max_attempts = retry_count + 1
        # A step's own TimeoutSeconds wins when present, including 0, which
        # means unlimited (normalize_timeout turns it into None). `or` would be
        # wrong here: 0 is falsy, so it would fall through to the default.
        step_timeout = (
            normalize_timeout(step.timeout_seconds)
            if step.timeout_seconds is not None
            else self._default_step_timeout
        )
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
                        timeout=step_timeout
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
            self._error_config.enable_human_intervention and
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
                    if self._step_executor_fn:
                        result = await self._step_executor_fn(step, wf_context)
                    else:
                        result = await self.registry.execute(
                            step.instrument,
                            step.action,
                            step.parameters,
                            timeout=step_timeout
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
