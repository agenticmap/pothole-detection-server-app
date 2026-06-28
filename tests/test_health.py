"""Tests for the health check endpoint."""

import pytest


class TestHealthEndpoint:
    """Test GET /health."""

    @pytest.mark.asyncio
    async def test_health_returns_200(self, client):
        """Health endpoint should always return 200 (even if DB is down)."""
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "version" in data
        assert data["version"] == "2.0.0"
