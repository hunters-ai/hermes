"""Tests for Prometheus metrics (P2)."""
import os
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

# Set config path before importing app
os.environ.setdefault("CONFIG_PATH", "config/config.yaml")

from hermes.api.app import app
from hermes.core.remediation_manager import RemediationManager
from hermes.config import Config, RemediationConfig
from hermes.core.state_store import InMemoryStateStore


class TestPrometheusMetrics:
    """Tests for Prometheus metrics endpoint."""
    
    @pytest.fixture
    def client(self):
        """Create test client."""
        return TestClient(app)
    
    def test_metrics_endpoint_exists(self, client):
        """Metrics endpoint should be accessible."""
        response = client.get("/metrics")
        assert response.status_code == 200
    
    def test_metrics_contains_incoming_requests(self, client):
        """Should contain incoming requests counter."""
        response = client.get("/metrics")
        assert "hermes_incoming_requests_total" in response.text
    
    def test_metrics_contains_webhook_requests(self, client):
        """Should contain webhook requests counter."""
        response = client.get("/metrics")
        assert "hermes_webhook_requests_total" in response.text
    
    def test_metrics_contains_processing_duration(self, client):
        """Should contain processing duration histogram."""
        response = client.get("/metrics")
        assert "hermes_processing_duration_seconds" in response.text
    
    def test_metrics_contains_rundeck_job_triggers(self, client):
        """Should contain Rundeck job triggers counter."""
        response = client.get("/metrics")
        assert "hermes_rundeck_job_triggers_total" in response.text
    
    def test_metrics_contains_remediation_workflows(self, client):
        """Should contain remediation workflows counter."""
        response = client.get("/metrics")
        assert "hermes_remediation_workflows_total" in response.text
    
    def test_metrics_contains_deduplicated_counter(self, client):
        """Should contain alerts deduplicated counter (P0)."""
        response = client.get("/metrics")
        assert "hermes_alerts_deduplicated_total" in response.text
    
    def test_metrics_contains_circuit_breaker_counter(self, client):
        """Should contain circuit breaker trips counter (P0)."""
        response = client.get("/metrics")
        assert "hermes_circuit_breaker_trips_total" in response.text
    
    def test_metrics_contains_remediation_outcomes(self, client):
        """Should contain remediation outcomes counter (P2)."""
        response = client.get("/metrics")
        assert "hermes_remediation_outcomes_total" in response.text
    
    def test_metrics_contains_rate_limited_counter(self, client):
        """Should contain rate limited requests counter (P3)."""
        response = client.get("/metrics")
        assert "hermes_rate_limited_requests_total" in response.text


class TestMetricsCallback:
    """Tests for metrics callback in RemediationManager."""
    
    def test_record_outcome_calls_callback(self):
        """Should call metrics callback when recording outcome."""
        mock_callback = MagicMock()
        
        config = MagicMock(spec=Config)
        config.remediation = RemediationConfig()
        config.alertmanager = None
        config.jira = None
        config.slack = None
        
        manager = RemediationManager(
            config,
            InMemoryStateStore(),
            MagicMock(),
            metrics_callback=mock_callback
        )
        
        manager._record_outcome("TestAlert", "success")
        
        mock_callback.assert_called_once_with("TestAlert", "success")
    
    def test_record_outcome_handles_callback_exception(self):
        """Should handle callback exceptions gracefully."""
        def failing_callback(alert_name, outcome):
            raise Exception("Callback failed")
        
        config = MagicMock(spec=Config)
        config.remediation = RemediationConfig()
        config.alertmanager = None
        config.jira = None
        config.slack = None
        
        manager = RemediationManager(
            config,
            InMemoryStateStore(),
            MagicMock(),
            metrics_callback=failing_callback
        )
        
        # Should not raise exception
        manager._record_outcome("TestAlert", "failure")
