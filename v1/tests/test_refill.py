"""Integration test for the refill-tips API endpoint."""

import socket

import httpx
import pytest


def _is_webapp_available(host: str = "localhost", port: int = 5000, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.mark.integration
@pytest.mark.opentrons
@pytest.mark.asyncio
async def test_refill_tiprack_endpoint():
    """`/api/opentrons/refill-tips` should return a structured JSON response."""
    if not _is_webapp_available():
        pytest.skip("WebApp not running on localhost:5000")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "http://localhost:5000/api/opentrons/refill-tips",
            json={"rack_type": "all"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    assert "success" in payload
