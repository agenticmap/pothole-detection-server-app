"""Tests for the health check endpoint."""

import pytest


class TestHealthEndpoint:
    """Test GET /health."""

    @pytest.mark.asyncio
    async def test_health_returns_200_when_db_reachable(self, client):
        """The happy path: pool acquires, SELECT 1 succeeds."""
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["db"] == "connected"
        assert data["version"] == "2.0.0"

    @pytest.mark.asyncio
    async def test_health_returns_503_when_db_unreachable(self, client, monkeypatch):
        """Regression: the unhealthy branch used to `return` a dict, i.e. HTTP 200.

        A status-code-only uptime check (and the compose healthcheck) therefore
        reported green against a dead database. The body still carries `status`
        so anything parsing that keeps working.
        """
        from app.routes import health

        def _boom():
            raise RuntimeError("pool is gone")

        # health.py imports get_pool into its own namespace, so patch it there.
        monkeypatch.setattr(health, "get_pool", _boom)

        response = await client.get("/health")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["db"] == "disconnected"
        assert data["version"] == "2.0.0"
        assert "pool is gone" in data["error"]
