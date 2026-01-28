"""Tests for health check endpoints (P2)."""
import os
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi.testclient import TestClient

# Set config path before importing app
os.environ.setdefault("CONFIG_PATH", "config/config.yaml")

from hermes.api.app import app, remediation_manager


class TestHealthEndpoints:
    """Tests for health check endpoints."""
    
    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)
    
    def test_health_basic(self, client):
        """Basic health endpoint should return healthy."""
        response = client.get("/health")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
    
    def test_health_live(self, client):
        """Liveness probe should return alive."""
        response = client.get("/health/live")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"
    
    def test_health_ready_returns_status(self, client):
        """Readiness probe should return health status."""
        response = client.get("/health/ready")
        
        # Could be 200 or 503 depending on dependencies
        assert response.status_code in [200, 503]
        data = response.json()
        assert "status" in data
        assert "dependencies" in data


class TestHealthReadyDetails:
    """Detailed tests for readiness probe."""
    
    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)
    
    def test_ready_includes_config_status(self, client):
        """Should include config loaded status."""
        response = client.get("/health/ready")
        data = response.json()
        
        assert "config_loaded" in data
    
    def test_ready_includes_circuit_breaker_states(self, client):
        """Should include circuit breaker states when remediation manager exists."""
        if remediation_manager is None:
            pytest.skip("Remediation manager not configured")
        
        response = client.get("/health/ready")
        data = response.json()
        
        assert "circuit_breakers" in data
    
    def test_ready_includes_workflow_count(self, client):
        """Should include active workflow count."""
        if remediation_manager is None:
            pytest.skip("Remediation manager not configured")
        
        response = client.get("/health/ready")
        data = response.json()
        
        assert "active_workflows" in data
        assert "max_workflows" in data


class TestRateLimitsEndpoint:
    """Tests for rate limits endpoint."""
    
    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)
    
    def test_rate_limits_endpoint(self, client):
        """Rate limits endpoint should return stats."""
        response = client.get("/api/v1/rate-limits")
        
        assert response.status_code == 200
        data = response.json()
        assert "enabled" in data
