"""Tests for PnPClient timeout normalization (src/client.py execute()).

Without this, a caller passing timeout=0/None to mean "wait indefinitely" for
a multi-day protocol would silently get capped at 4h (see execute_command
timeout wiring in src/lab_core.py and src/workflow.py) — see
`C:\\Users\\Bicocca\\.claude\\plans\\ok-top-analizza-il-bubbly-piglet.md`.

Note: the "poll forever" branch inside PnPClient._execute_via_sila2_client is
not covered here with a full sila2 ClientObservableCommandInstance double —
sila2 registers its protobuf descriptors in a process-global pool on first
import, and importing/patching sila2.client fresh from a test module can
collide with an earlier import elsewhere in the suite ("duplicate file name
SiLAFramework.proto"), depending on pytest's collection order. The change
itself is a 3-line, direct copy of the same None-means-forever pattern
already used (and already tested in production) by
OpentronsSiLA2Server/src/robot_client.py's wait_for_run_completion.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.client import PnPClient
from src.discovery import PnPCommand, PnPFeature


def _make_server():
    server = MagicMock()
    server.server_online = True
    server.host = "127.0.0.1"
    server.port = 12345
    server.name = "TestServer"
    return server


class TestExecuteTimeoutNormalization:
    """execute() must normalize timeout<=0 to None before dispatching, and
    leave a positive timeout untouched."""

    @pytest.mark.asyncio
    async def test_zero_timeout_is_normalized_to_none(self):
        client = PnPClient()
        server = _make_server()
        feature = PnPFeature(identifier="TestFeature")
        command = PnPCommand(identifier="DoThing", observable=False)
        server.find_command.return_value = (feature, command)

        captured = {}

        async def _fake_sila2(srv, feat, cmd, params, timeout, on_progress):
            captured["timeout"] = timeout
            return MagicMock(success=True)

        with patch.object(client, "_execute_via_sila2_client", side_effect=_fake_sila2):
            await client.execute(server, "DoThing", timeout=0)

        assert captured["timeout"] is None

    @pytest.mark.asyncio
    async def test_negative_timeout_is_normalized_to_none(self):
        client = PnPClient()
        server = _make_server()
        feature = PnPFeature(identifier="TestFeature")
        command = PnPCommand(identifier="DoThing", observable=False)
        server.find_command.return_value = (feature, command)

        captured = {}

        async def _fake_sila2(srv, feat, cmd, params, timeout, on_progress):
            captured["timeout"] = timeout
            return MagicMock(success=True)

        with patch.object(client, "_execute_via_sila2_client", side_effect=_fake_sila2):
            await client.execute(server, "DoThing", timeout=-5)

        assert captured["timeout"] is None

    @pytest.mark.asyncio
    async def test_none_timeout_stays_none(self):
        client = PnPClient()
        server = _make_server()
        feature = PnPFeature(identifier="TestFeature")
        command = PnPCommand(identifier="DoThing", observable=False)
        server.find_command.return_value = (feature, command)

        captured = {}

        async def _fake_sila2(srv, feat, cmd, params, timeout, on_progress):
            captured["timeout"] = timeout
            return MagicMock(success=True)

        with patch.object(client, "_execute_via_sila2_client", side_effect=_fake_sila2):
            await client.execute(server, "DoThing", timeout=None)

        assert captured["timeout"] is None

    @pytest.mark.asyncio
    async def test_positive_timeout_is_left_untouched(self):
        client = PnPClient()
        server = _make_server()
        feature = PnPFeature(identifier="TestFeature")
        command = PnPCommand(identifier="DoThing", observable=False)
        server.find_command.return_value = (feature, command)

        captured = {}

        async def _fake_sila2(srv, feat, cmd, params, timeout, on_progress):
            captured["timeout"] = timeout
            return MagicMock(success=True)

        with patch.object(client, "_execute_via_sila2_client", side_effect=_fake_sila2):
            await client.execute(server, "DoThing", timeout=42.0)

        assert captured["timeout"] == 42.0
