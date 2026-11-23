"""The emergency stop has to be fast, concurrent, and honest about its result.

`POST /api/emergency/stop` had three faults that only show up when something is
already wrong — which is the only time the endpoint is ever called:

* It ran `await lab_core.discover()` first, adding a network scan to the one
  request that cannot afford one.
* It sent the stop commands in a `for` loop with `await` inside, each call
  bounded by `workflow.default_timeout` (600 s in the shipped config). One
  unresponsive instrument therefore delayed the stop reaching every instrument
  after it — potentially by ten minutes, per instrument.
* It returned `{"status": "stopped"}` unconditionally: with no instruments, with
  every call failing, with an exception mid-loop. The operator page read that
  field and showed "System Stopped", so the UI asserted the lab was safe on the
  strength of a constant.

These tests pin the behaviour, not the implementation: elapsed time for
concurrency, and `status` for honesty.
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
if str(V1_DIR) not in sys.path:
    sys.path.insert(0, str(V1_DIR))

from webapp.routes.operator import EMERGENCY_STOP_TIMEOUT_S, create_operator_router  # noqa: E402


# ── Test doubles ───────────────────────────────────────────────────────────────

class FakeState:
    def __init__(self):
        self.logs = []

    def add_log(self, level, message, source=""):
        self.logs.append((level, message, source))


class FakeWsManager:
    def __init__(self):
        self.broadcasts = []

    async def broadcast(self, payload):
        self.broadcasts.append(payload)


def _instrument(instrument_id, name, commands):
    return SimpleNamespace(
        id=instrument_id,
        name=name,
        commands=[SimpleNamespace(id=c) for c in commands],
    )


class FakeLabCore:
    """Records what was asked of it, and how each call should behave.

    `behaviour` maps instrument id -> ("ok", None) | ("fail", msg) | ("hang", secs)
    """

    def __init__(self, instruments, behaviour=None):
        self._instruments = instruments
        self._behaviour = behaviour or {}
        self.discover_calls = 0
        self.calls = []
        self.concurrent_peak = 0
        self._in_flight = 0

    async def discover(self, timeout=2.0):
        self.discover_calls += 1
        return list(self._instruments)

    def list_instruments(self):
        return list(self._instruments)

    async def execute_command(self, instrument_id, command_id, parameters):
        self.calls.append((instrument_id, command_id))
        self._in_flight += 1
        self.concurrent_peak = max(self.concurrent_peak, self._in_flight)
        try:
            kind, detail = self._behaviour.get(instrument_id, ("ok", None))
            if kind in ("hang", "slow"):
                await asyncio.sleep(detail)
            else:
                # A real gRPC call always yields. Without an await here the
                # quick calls would finish before the next task is even
                # scheduled, and `concurrent_peak` would read 1 however
                # concurrent the endpoint is.
                await asyncio.sleep(0.05)
            if kind == "fail":
                return SimpleNamespace(success=False, error=detail)
            return SimpleNamespace(success=True, error=None)
        finally:
            self._in_flight -= 1


def _endpoint(lab_core):
    """The bare `emergency_stop` coroutine, lifted out of the router."""
    router = create_operator_router(
        FakeState(), FakeWsManager(), lab_core, [], asyncio.Lock(), V1_DIR
    )
    for route in router.routes:
        if getattr(route, "path", None) == "/api/emergency/stop":
            return route.endpoint
    raise AssertionError("/api/emergency/stop is not registered")


THREE = [
    _instrument("opentrons", "Opentrons Flex", ["ExecuteRecipe", "EmergencyStop"]),
    _instrument("tecan", "Tecan M200 Pro", ["RunMeasurement", "Abort"]),
    _instrument("mobile", "Mobile Robot", ["ExecuteTask", "Stop"]),
]


# ── It must not wait for discovery ─────────────────────────────────────────────

class TestItDoesNotRediscover:

    @pytest.mark.asyncio
    async def test_the_registry_is_used_as_it_stands(self):
        core = FakeLabCore(THREE)
        await _endpoint(core)()
        assert core.discover_calls == 0, (
            "an emergency stop must not run a network scan before acting"
        )

    @pytest.mark.asyncio
    async def test_every_instrument_still_gets_its_own_stop_command(self):
        core = FakeLabCore(THREE)
        await _endpoint(core)()
        assert dict(core.calls) == {
            "opentrons": "EmergencyStop",   # exact name wins
            "tecan": "Abort",               # falls through the candidate list
            "mobile": "Stop",
        }


# ── One slow instrument must not delay the others ──────────────────────────────

class TestItIsConcurrent:

    @pytest.mark.asyncio
    async def test_an_unresponsive_instrument_does_not_hold_up_the_rest(self):
        # The mobile robot never answers; the other two are quick.
        core = FakeLabCore(THREE, {"mobile": ("hang", 60.0)})
        started = time.monotonic()
        result = await _endpoint(core)()
        elapsed = time.monotonic() - started

        # Total cost is one timeout, not one per instrument, and nowhere near
        # the 60 s the hung call would have taken.
        assert elapsed < EMERGENCY_STOP_TIMEOUT_S + 1.0, (
            f"took {elapsed:.1f}s — the calls are not running concurrently"
        )
        assert core.concurrent_peak == 3, "the stops went out one at a time"
        assert result["successful"] == 2
        assert result["status"] == "partial"
        assert any("Mobile Robot" in f for f in result["failures"])

    @pytest.mark.asyncio
    async def test_a_hung_call_is_reported_as_a_timeout_not_a_success(self):
        core = FakeLabCore(THREE, {"mobile": ("hang", 60.0)})
        result = await _endpoint(core)()
        mobile_failure = next(f for f in result["failures"] if "Mobile Robot" in f)
        assert "no answer within" in mobile_failure


# ── `status` must describe what happened ───────────────────────────────────────

class TestItTellsTheTruth:

    @pytest.mark.asyncio
    async def test_all_acknowledged_reads_stopped(self):
        result = await _endpoint(FakeLabCore(THREE))()
        assert result["status"] == "stopped"
        assert (result["attempted"], result["successful"]) == (3, 3)
        assert result["failures"] == []

    @pytest.mark.asyncio
    async def test_some_acknowledged_reads_partial(self):
        core = FakeLabCore(THREE, {"tecan": ("fail", "bridge not connected")})
        result = await _endpoint(core)()
        assert result["status"] == "partial"
        assert "VERIFY" in result["message"].upper()

    @pytest.mark.asyncio
    async def test_none_acknowledged_reads_failed(self):
        core = FakeLabCore(THREE, {
            "opentrons": ("fail", "down"),
            "tecan": ("fail", "down"),
            "mobile": ("fail", "down"),
        })
        result = await _endpoint(core)()
        assert result["status"] == "failed"
        assert "hardware emergency stop" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_no_instrument_exposes_a_stop_command_is_not_stopped(self):
        # Present, discovered, and unable to be stopped through this path. The
        # old code reported "stopped" here, having sent nothing at all.
        none_stoppable = [_instrument("thermo", "Incubator", ["GetTemperature"])]
        result = await _endpoint(FakeLabCore(none_stoppable))()
        assert result["status"] == "failed"
        assert result["attempted"] == 0
        assert result["skipped_instruments"] == ["Incubator"]

    @pytest.mark.asyncio
    async def test_an_empty_registry_is_not_stopped(self):
        result = await _endpoint(FakeLabCore([]))()
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_no_lab_core_at_all_is_not_stopped(self):
        router = create_operator_router(
            FakeState(), FakeWsManager(), None, [], asyncio.Lock(), V1_DIR
        )
        endpoint = next(r.endpoint for r in router.routes
                        if getattr(r, "path", None) == "/api/emergency/stop")
        result = await endpoint()
        assert result["status"] == "failed"
        assert "manually" in result["message"].lower()


# ── The broadcast goes out regardless ──────────────────────────────────────────

class TestTheBroadcast:

    @pytest.mark.asyncio
    async def test_connected_clients_are_told_before_the_commands_are_sent(self):
        ws = FakeWsManager()
        core = FakeLabCore(THREE, {"mobile": ("hang", 60.0)})
        router = create_operator_router(
            FakeState(), ws, core, [], asyncio.Lock(), V1_DIR
        )
        endpoint = next(r.endpoint for r in router.routes
                        if getattr(r, "path", None) == "/api/emergency/stop")
        await endpoint()
        assert ws.broadcasts and ws.broadcasts[0]["type"] == "emergency_stop"
