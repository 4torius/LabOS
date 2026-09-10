"""Tests for src/property_watch.py — pushed property values instead of polling.

The dashboard used to ask four instruments for six values every few seconds, to
learn that nothing had happened, and still show a stale value in between.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

V1_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V1_DIR))

from src.property_watch import PropertyValue, PropertyWatcher  # noqa: E402


def _prop(name, feature="WorkflowAPI", observable=True):
    prop = MagicMock()
    prop.id = name
    prop.feature = feature
    prop.observable = observable
    return prop


def _instrument(instrument_id, properties, status="online"):
    inst = MagicMock()
    inst.id = instrument_id
    inst.name = instrument_id.title()
    inst.status = status
    inst.properties = properties
    inst._server = MagicMock()
    return inst


class _Handle:
    """Stands in for a sila2 Subscription."""

    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def _core(instruments, *, observable_ok=True, poll_value="polled"):
    core = MagicMock()
    core.list_instruments.return_value = instruments
    core._client = MagicMock()

    handles = []
    captured = {}

    async def _subscribe(server, feature, name, on_value):
        if not observable_ok:
            return None
        handle = _Handle()
        handles.append(handle)
        captured[(feature, name)] = on_value
        return handle

    core._client.subscribe_property = AsyncMock(side_effect=_subscribe)
    core.get_property = AsyncMock(return_value=MagicMock(success=True, data=poll_value))
    core._handles = handles
    core._callbacks = captured
    return core


class TestSubscribing:

    @pytest.mark.asyncio
    async def test_observable_properties_are_subscribed_not_polled(self):
        core = _core([_instrument("opentrons", [_prop("Status"), _prop("CurrentRunId")])])
        watcher = PropertyWatcher(core, lambda entry: None)
        await watcher._reconcile()

        assert core._client.subscribe_property.await_count == 2
        # a live subscription means nothing has to be asked for
        assert core.get_property.await_count == 0

    @pytest.mark.asyncio
    async def test_unobservable_properties_are_polled(self):
        core = _core([_instrument("opentrons", [_prop("RobotInfo", observable=False)])])
        watcher = PropertyWatcher(core, lambda entry: None)
        await watcher._reconcile()

        assert core._client.subscribe_property.await_count == 0
        assert core.get_property.await_count == 1

    @pytest.mark.asyncio
    async def test_a_server_that_refuses_a_subscription_falls_back_to_polling(self):
        core = _core([_instrument("opentrons", [_prop("Status")])], observable_ok=False)
        watcher = PropertyWatcher(core, lambda entry: None)
        await watcher._reconcile()
        assert core.get_property.await_count == 1

    @pytest.mark.asyncio
    async def test_offline_instruments_are_left_alone(self):
        core = _core([_instrument("tecan", [_prop("Status")], status="offline")])
        watcher = PropertyWatcher(core, lambda entry: None)
        await watcher._reconcile()
        assert core._client.subscribe_property.await_count == 0

    @pytest.mark.asyncio
    async def test_a_live_subscription_is_not_established_twice(self):
        core = _core([_instrument("opentrons", [_prop("Status")])])
        watcher = PropertyWatcher(core, lambda entry: None)
        await watcher._reconcile()
        await watcher._reconcile()
        assert core._client.subscribe_property.await_count == 1

    @pytest.mark.asyncio
    async def test_a_dead_subscription_is_re_established(self):
        """Subscriptions die when a server restarts, and they die silently."""
        core = _core([_instrument("opentrons", [_prop("Status")])])
        watcher = PropertyWatcher(core, lambda entry: None)
        await watcher._reconcile()
        core._handles[0].cancelled = True          # the server went away
        await watcher._reconcile()
        assert core._client.subscribe_property.await_count == 2


class TestReporting:

    @pytest.mark.asyncio
    async def test_a_pushed_value_reaches_the_callback(self):
        seen = []
        core = _core([_instrument("opentrons", [_prop("Status")])])
        watcher = PropertyWatcher(core, seen.append)
        watcher._loop = asyncio.get_running_loop()
        await watcher._reconcile()

        core._callbacks[("WorkflowAPI", "Status")]('{"state": "running"}')
        await asyncio.sleep(0)                      # let the loop hop run

        assert len(seen) == 1
        assert seen[0].instrument_id == "opentrons"
        assert seen[0].name == "Status"
        assert seen[0].source == "subscription"

    @pytest.mark.asyncio
    async def test_an_unchanged_value_is_not_reported_again(self):
        """Otherwise every poll cycle wakes every connected browser."""
        seen = []
        core = _core([_instrument("opentrons", [_prop("Status")])])
        watcher = PropertyWatcher(core, seen.append)
        watcher._loop = asyncio.get_running_loop()
        await watcher._reconcile()

        push = core._callbacks[("WorkflowAPI", "Status")]
        push("idle"); await asyncio.sleep(0)
        push("idle"); await asyncio.sleep(0)
        push("running"); await asyncio.sleep(0)

        assert [entry.value for entry in seen] == ["idle", "running"]

    @pytest.mark.asyncio
    async def test_a_failing_callback_does_not_stop_the_watcher(self):
        def explode(entry):
            raise RuntimeError("the page blew up")

        core = _core([_instrument("opentrons", [_prop("Status")])])
        watcher = PropertyWatcher(core, explode)
        watcher._loop = asyncio.get_running_loop()
        await watcher._reconcile()
        core._callbacks[("WorkflowAPI", "Status")]("idle")
        await asyncio.sleep(0)                      # must not raise

    @pytest.mark.asyncio
    async def test_the_snapshot_is_what_a_page_reads_on_load(self):
        core = _core([_instrument("opentrons", [_prop("RobotInfo", observable=False)])])
        watcher = PropertyWatcher(core, lambda entry: None)
        await watcher._reconcile()

        snapshot = watcher.snapshot()
        assert len(snapshot) == 1
        assert snapshot[0]["property"] == "RobotInfo"
        assert snapshot[0]["source"] == "poll"

    @pytest.mark.asyncio
    async def test_the_snapshot_can_be_narrowed_to_one_instrument(self):
        core = _core([
            _instrument("opentrons", [_prop("A", observable=False)]),
            _instrument("tecan", [_prop("B", observable=False)]),
        ])
        watcher = PropertyWatcher(core, lambda entry: None)
        await watcher._reconcile()
        assert len(watcher.snapshot("tecan")) == 1


class TestLifecycle:

    @pytest.mark.asyncio
    async def test_an_instrument_going_away_releases_its_subscriptions(self):
        instrument = _instrument("opentrons", [_prop("Status")])
        core = _core([instrument])
        watcher = PropertyWatcher(core, lambda entry: None)
        await watcher._reconcile()
        handle = core._handles[0]

        core.list_instruments.return_value = []     # the server disconnected
        await watcher._reconcile()

        assert handle.cancelled, "subscription leaked for a departed instrument"
        assert watcher.snapshot() == []

    @pytest.mark.asyncio
    async def test_stop_cancels_everything(self):
        core = _core([_instrument("opentrons", [_prop("Status"), _prop("CurrentRunId")])])
        watcher = PropertyWatcher(core, lambda entry: None)
        await watcher.start()
        await asyncio.sleep(0.05)
        await watcher.stop()
        assert all(handle.cancelled for handle in core._handles)

    @pytest.mark.asyncio
    async def test_starting_twice_is_harmless(self):
        core = _core([])
        watcher = PropertyWatcher(core, lambda entry: None)
        await watcher.start()
        await watcher.start()
        await watcher.stop()


class TestPropertyValue:

    def test_serialises_for_the_websocket(self):
        entry = PropertyValue("opentrons", "WorkflowAPI", "Status", "idle")
        assert entry.to_dict() == {
            "instrument_id": "opentrons", "feature": "WorkflowAPI",
            "property": "Status", "value": "idle", "source": "subscription",
        }

    def test_key_identifies_one_property_on_one_instrument(self):
        entry = PropertyValue("opentrons", "WorkflowAPI", "Status", "idle")
        assert entry.key == ("opentrons", "WorkflowAPI", "Status")
