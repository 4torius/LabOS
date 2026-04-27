# LabOS API-Only Roadmap

## Objective

Transition to a robust API-first operating model, simplify configuration, and prepare for physical machine refinement.

## Current Status

Completed in this cycle:

- Runtime hardcoded critical ports removed from WebApp routes.
- Manual Station acknowledgment now resolves endpoint from configuration.
- Opentrons refill route now resolves endpoint from configuration.
- Emergency stop now propagates through LabCore to discovered instruments.
- Launcher migrated to API-only behavior (no CLI option/menu path).
- Launcher now handles remote servers safely (no local start attempt).
- Legacy refill test converted into a proper pytest integration test.
- Full test baseline stabilized: 24 passed, 23 skipped.

## Phase 1: Configuration Simplification (P0)

Duration: 1-2 days

Goals:

- Reduce lab_config.yaml to required runtime keys for orchestration.
- Move UI convenience options to optional profile/config block.
- Enforce schema validation at startup.

Tasks:

1. Define required keys for:
   - system
   - servers
   - discovery
   - workflow
   - error_handling
2. Mark non-critical sections as optional.
3. Add startup validation errors for missing/invalid fields.
4. Add warning when unknown keys are present.

Exit criteria:

- Startup fails fast on invalid critical config.
- No runtime module depends on ambiguous optional keys.

## Phase 2: API Contract Hardening (P0)

Duration: 2-3 days

Goals:

- Make terminal automation fully reliable via API.
- Add consistency and observability to command execution.

Tasks:

1. Define canonical response structure for command endpoints.
2. Add request correlation id in logs and responses.
3. Add endpoint-level timeout and retry defaults.
4. Add clear error categories in API payloads.

Exit criteria:

- Automation scripts can rely on stable status fields.
- Failures are diagnosable from API logs without manual tracing.

## Phase 3: Network and Port Governance (P0)

Duration: 1 day

Goals:

- Freeze a clear port map and network model before physical runs.

Tasks:

1. Publish service port table and host ownership.
2. Validate connectivity pre-flight for each enabled server.
3. Document firewall rules for orchestrator and remote hosts.

Exit criteria:

- A single network checklist can validate session readiness.

## Phase 4: Safety and Runtime Robustness (P1)

Duration: 2-4 days

Goals:

- Improve resilience during real hardware operation.

Tasks:

1. Extend emergency handling with per-device report persistence.
2. Tune retry strategy by command category.
3. Add cooldown and backoff policy for unstable links.
4. Add explicit degraded-mode behavior for missing servers.

Exit criteria:

- Emergency and recovery behavior is deterministic and logged.

## Phase 5: Documentation and Operational Runbooks (P1)

Duration: 1-2 days

Goals:

- Keep docs aligned with API-only and remote server model.

Tasks:

1. Remove remaining CLI-centric instructions.
2. Add terminal examples for all common API operations.
3. Add pre-run and post-run checklists.

Exit criteria:

- New operator can run a full session from runbook only.

## Phase 6: Physical Refinement Loop (P1/P2)

Duration: ongoing

Goals:

- Iterate based on observed machine behavior.

Tasks:

1. Collect timing and failure telemetry per command.
2. Track root causes by error category.
3. Apply targeted timeout/retry adjustments.

Exit criteria:

- Failure rate and intervention count trend down session-by-session.

## Suggested Priority Queue

1. Config schema and simplification.
2. API contract hardening.
3. Network pre-flight and port governance.
4. Safety robustness and emergency reporting.
5. Documentation/runbook finalization.

## Risks to Monitor

- Drift between server-side and orchestrator-side defaults.
- Undocumented remote deployment differences.
- Silent failures due to optional config blocks.
- Long-running commands without bounded timeouts.
