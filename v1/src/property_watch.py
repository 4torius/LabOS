"""Keeping instrument property values current, without polling for them.

A SiLA2 observable property is *pushed*: the server sends a new value the
moment it changes. The orchestrator could only ever `get()` one, so the
dashboard polled — four instruments times six properties every few seconds, to
learn that nothing had happened, and still showing a stale value in between.
Something as consequential as "is a run in progress" was up to several seconds
out of date on the screen the operator watches.

`PropertyWatcher` subscribes to every observable property of every discovered
instrument and calls one callback whenever any of them changes. Properties that
are *not* observable are polled on a slow timer, because a value nobody can
push still has to arrive somehow — but that is now the exception rather than
how everything works.

Two things this has to get right, because both are easy to get wrong and
neither shows up until it has been running for a while:

* **Threads.** The sila2 library delivers values on its own executor thread.
  Every callback here is marshalled back onto the asyncio loop before it
  touches anything shared.
* **Reconnection.** A subscription dies when its server restarts, and it dies
  silently. The watcher notices a dead subscription and re-establishes it,
  rather than going quiet for the rest of the session.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: How often to re-read properties that cannot be subscribed to, and to check
#: that live subscriptions are still alive. Deliberately slow: this is the
#: fallback path, not the main one.
DEFAULT_POLL_INTERVAL_S = 15.0

#: Key identifying one property on one instrument.
PropertyKey = Tuple[str, str, str]      # (instrument_id, feature, property)


@dataclass
class PropertyValue:
    instrument_id: str
    feature: str
    name: str
    value: Any
    #: "subscription" when the server pushed it, "poll" when we asked.
    source: str = "subscription"

    @property
    def key(self) -> PropertyKey:
        return (self.instrument_id, self.feature, self.name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "feature": self.feature,
            "property": self.name,
            "value": self.value,
            "source": self.source,
        }


@dataclass
class _Watch:
    instrument_id: str
    feature: str
    name: str
    observable: bool
    handle: Any = None          # sila2 Subscription, when subscribed
    failures: int = 0

    @property
    def key(self) -> PropertyKey:
        return (self.instrument_id, self.feature, self.name)

    @property
    def live(self) -> bool:
        """Whether the subscription is still delivering.

        A cancelled or errored sila2 Subscription reports it; anything that
        does not expose that is assumed live and caught by the next read.
        """
        if self.handle is None:
            return False
        for attribute in ("is_cancelled", "cancelled"):
            flag = getattr(self.handle, attribute, None)
            if isinstance(flag, bool) and flag:
                return False
        return True


class PropertyWatcher:
    """Watches every property of every instrument and reports changes."""

    def __init__(
        self,
        core,
        on_change: Callable[[PropertyValue], Any],
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL_S,
    ):
        self._core = core
        self._on_change = on_change
        self._poll_interval = poll_interval
        self._watches: Dict[PropertyKey, _Watch] = {}
        self._values: Dict[PropertyKey, PropertyValue] = {}
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopping = asyncio.Event()

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="property-watcher")
        logger.info("Property watcher started")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for watch in self._watches.values():
            self._cancel(watch)
        self._watches.clear()
        logger.info("Property watcher stopped")

    # ── Reading ─────────────────────────────────────────────────────────────

    def snapshot(self, instrument_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every value currently known, optionally for one instrument.

        This is what a page loading mid-session reads, so it does not have to
        wait for the next change to show anything.
        """
        values = self._values.values()
        if instrument_id:
            values = [v for v in values if v.instrument_id == instrument_id]
        return [v.to_dict() for v in values]

    # ── Internals ───────────────────────────────────────────────────────────

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Property watcher cycle failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _reconcile(self) -> None:
        """Subscribe to anything new, re-subscribe to anything dead, poll the rest."""
        instruments = {i.id: i for i in self._core.list_instruments()}

        # Drop watches for instruments that went away, so a disconnected server
        # does not leave a subscription leaking behind it.
        for key in [k for k in self._watches if k[0] not in instruments]:
            self._cancel(self._watches.pop(key))
            self._values.pop(key, None)

        for instrument in instruments.values():
            if instrument.status != "online":
                continue
            for prop in getattr(instrument, "properties", []):
                key = (instrument.id, prop.feature, prop.id)
                watch = self._watches.get(key)
                if watch is None:
                    watch = _Watch(instrument.id, prop.feature, prop.id, prop.observable)
                    self._watches[key] = watch

                if watch.observable and not watch.live:
                    await self._subscribe(instrument, watch)
                if not watch.observable or not watch.live:
                    # Either it cannot be pushed, or the subscription is not up:
                    # read it so the value is at least this fresh.
                    await self._poll_once(instrument, watch)

    async def _subscribe(self, instrument, watch: _Watch) -> None:
        server = getattr(instrument, "_server", None)
        if server is None or self._core._client is None:
            return

        loop = self._loop

        def _on_value(value: Any) -> None:
            # Called on the sila2 executor thread. Hop back onto the loop
            # before touching anything shared.
            if loop is None or loop.is_closed():
                return
            loop.call_soon_threadsafe(self._record, watch.key, value, "subscription")

        handle = await self._core._client.subscribe_property(
            server, watch.feature, watch.name, _on_value
        )
        if handle is None:
            # Not observable after all, or the server is not answering. Poll it.
            watch.observable = False
            watch.failures += 1
            return
        watch.handle = handle
        watch.failures = 0
        logger.debug("Subscribed to %s.%s on %s", watch.feature, watch.name, instrument.name)

    async def _poll_once(self, instrument, watch: _Watch) -> None:
        try:
            result = await self._core.get_property(instrument.id, watch.feature, watch.name)
        except Exception as exc:
            logger.debug("Poll of %s.%s failed: %s", watch.feature, watch.name, exc)
            return
        if result.success:
            self._record(watch.key, result.data, "poll")

    def _record(self, key: PropertyKey, value: Any, source: str) -> None:
        previous = self._values.get(key)
        if previous is not None and previous.value == value:
            return                          # unchanged: nothing to tell anyone
        entry = PropertyValue(key[0], key[1], key[2], value, source)
        self._values[key] = entry
        try:
            outcome = self._on_change(entry)
            if asyncio.iscoroutine(outcome):
                asyncio.create_task(outcome)
        except Exception:
            logger.exception("Property change callback failed for %s", key)

    @staticmethod
    def _cancel(watch: _Watch) -> None:
        if watch.handle is None:
            return
        try:
            watch.handle.cancel()
        except Exception:
            pass
        watch.handle = None


__all__ = ["DEFAULT_POLL_INTERVAL_S", "PropertyValue", "PropertyWatcher"]
