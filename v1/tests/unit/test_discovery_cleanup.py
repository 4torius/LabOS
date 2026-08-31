"""Tests that mDNS/gRPC discovery resources are released even when discovery
fails partway through — previously only closed on the success path, leaking
a Zeroconf socket/thread (or gRPC channel) on every failed scan.
"""
import pytest
from unittest.mock import MagicMock, patch

from src.discovery import PnPDiscovery


class TestMdnsCleanupOnFailure:
    @pytest.mark.asyncio
    async def test_zeroconf_and_browser_closed_when_browser_creation_raises(self, tmp_path):
        discovery = PnPDiscovery(base_dir=tmp_path)

        mock_zc_instance = MagicMock()
        with patch("src.discovery.Zeroconf", return_value=mock_zc_instance), \
             patch("src.discovery.ServiceBrowser", side_effect=RuntimeError("boom")):
            await discovery._discover_via_mdns(timeout=0.01)

        mock_zc_instance.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_browser_cancelled_and_zc_closed_on_success(self, tmp_path):
        discovery = PnPDiscovery(base_dir=tmp_path)

        mock_zc_instance = MagicMock()
        mock_browser_instance = MagicMock()
        with patch("src.discovery.Zeroconf", return_value=mock_zc_instance), \
             patch("src.discovery.ServiceBrowser", return_value=mock_browser_instance):
            await discovery._discover_via_mdns(timeout=0.01)

        mock_browser_instance.cancel.assert_called_once()
        mock_zc_instance.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_error_itself_does_not_propagate(self, tmp_path):
        """Even if zc.close() itself raises, _discover_via_mdns must not raise."""
        discovery = PnPDiscovery(base_dir=tmp_path)

        mock_zc_instance = MagicMock()
        mock_zc_instance.close.side_effect = RuntimeError("close failed")
        with patch("src.discovery.Zeroconf", return_value=mock_zc_instance), \
             patch("src.discovery.ServiceBrowser", side_effect=RuntimeError("boom")):
            await discovery._discover_via_mdns(timeout=0.01)  # must not raise


class TestGrpcChannelCleanup:
    def test_channel_closed_when_ready_future_raises_unexpectedly(self, tmp_path):
        discovery = PnPDiscovery(base_dir=tmp_path)
        mock_channel = MagicMock()

        with patch("src.discovery.GRPC_AVAILABLE", True), \
             patch("grpc.insecure_channel", return_value=mock_channel), \
             patch("grpc.channel_ready_future", side_effect=RuntimeError("unexpected")):
            result = discovery._is_grpc_server("localhost", 12345, timeout=0.1)

        assert result is False
        mock_channel.close.assert_called_once()

    def test_channel_closed_on_success(self, tmp_path):
        discovery = PnPDiscovery(base_dir=tmp_path)
        mock_channel = MagicMock()
        mock_future = MagicMock()
        mock_future.result.return_value = None

        with patch("src.discovery.GRPC_AVAILABLE", True), \
             patch("grpc.insecure_channel", return_value=mock_channel), \
             patch("grpc.channel_ready_future", return_value=mock_future):
            result = discovery._is_grpc_server("localhost", 12345, timeout=0.1)

        assert result is True
        mock_channel.close.assert_called_once()
