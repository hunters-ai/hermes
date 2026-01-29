"""Tests for audit logging functionality (P3)."""
import pytest
import json
import logging
from unittest.mock import patch, MagicMock
from datetime import datetime

from hermes.utils.audit_logger import (
    AuditEventType,
    AuditEvent,
    AuditLogger,
    get_audit_logger
)


class TestAuditEventType:
    """Tests for AuditEventType enum."""
    
    def test_all_event_types_defined(self):
        """Should have all required event types."""
        expected_types = [
            "alert_received",
            "alert_deduplicated",
            "workflow_started",
            "job_triggered",
            "job_completed",
            "job_failed",
            "alert_resolved",
            "alert_still_firing",
            "escalation_sent",
            "circuit_breaker_opened",
            "circuit_breaker_closed",
            "workflow_recovered"
        ]
        
        actual_types = [e.value for e in AuditEventType]
        
        for expected in expected_types:
            assert expected in actual_types


class TestAuditEvent:
    """Tests for AuditEvent dataclass."""
    
    def test_to_dict_excludes_none(self):
        """Should exclude None values from dict."""
        event = AuditEvent(
            timestamp="2024-01-01T00:00:00Z",
            event_type="alert_received",
            workflow_id=None,
            alert_name="TestAlert",
            alert_labels={"cluster": "prod"},
            details={"action": "processing"}
        )
        
        result = event.to_dict()
        
        assert "workflow_id" not in result
        assert result["alert_name"] == "TestAlert"
    
    def test_to_json(self):
        """Should convert to valid JSON."""
        event = AuditEvent(
            timestamp="2024-01-01T00:00:00Z",
            event_type="alert_received",
            workflow_id="wf-123",
            alert_name="TestAlert",
            alert_labels={"cluster": "prod"},
            details={}
        )
        
        json_str = event.to_json()
        parsed = json.loads(json_str)
        
        assert parsed["event_type"] == "alert_received"
        assert parsed["workflow_id"] == "wf-123"


class TestAuditLogger:
    """Tests for AuditLogger class."""
    
    @pytest.fixture
    def audit_logger(self):
        """Create AuditLogger for testing."""
        return AuditLogger(service_name="test-hermes")
    
    def test_log_alert_received(self, audit_logger, caplog):
        """Should log alert received event."""
        with patch.object(audit_logger._audit_logger, 'info') as mock_info:
            audit_logger.log_alert_received(
                alert_name="TestAlert",
                alert_labels={"cluster": "prod"},
                source_alertmanager="http://alertmanager.example.com"
            )
            
            mock_info.assert_called_once()
            logged_json = mock_info.call_args[0][0]
            event = json.loads(logged_json)
            
            assert event["event_type"] == "alert_received"
            assert event["alert_name"] == "TestAlert"
            assert event["source_alertmanager"] == "http://alertmanager.example.com"
    
    def test_log_alert_deduplicated(self, audit_logger):
        """Should log alert deduplication event."""
        with patch.object(audit_logger._audit_logger, 'info') as mock_info:
            audit_logger.log_alert_deduplicated(
                alert_name="TestAlert",
                alert_labels={"cluster": "prod"},
                reason="Active workflow exists",
                existing_workflow_id="wf-123"
            )
            
            logged_json = mock_info.call_args[0][0]
            event = json.loads(logged_json)
            
            assert event["event_type"] == "alert_deduplicated"
            assert event["details"]["reason"] == "Active workflow exists"
            assert event["workflow_id"] == "wf-123"
    
    def test_log_workflow_started(self, audit_logger):
        """Should log workflow started event."""
        with patch.object(audit_logger._audit_logger, 'info') as mock_info:
            audit_logger.log_workflow_started(
                workflow_id="wf-123",
                alert_name="TestAlert",
                alert_labels={"cluster": "prod"},
                rundeck_execution_url="https://rundeck.example.com/project/ops/execution/show/456",
                source_alertmanager="http://alertmanager.example.com"
            )
            
            logged_json = mock_info.call_args[0][0]
            event = json.loads(logged_json)
            
            assert event["event_type"] == "workflow_started"
            assert event["workflow_id"] == "wf-123"
            assert event["details"]["rundeck_execution_url"] == "https://rundeck.example.com/project/ops/execution/show/456"
    
    def test_log_job_completed_success(self, audit_logger):
        """Should log job completed with success status."""
        with patch.object(audit_logger._audit_logger, 'info') as mock_info:
            audit_logger.log_job_completed(
                workflow_id="wf-123",
                alert_name="TestAlert",
                rundeck_execution_id="exec-456",
                status="succeeded",
                duration_seconds=45.5
            )
            
            logged_json = mock_info.call_args[0][0]
            event = json.loads(logged_json)
            
            assert event["event_type"] == "job_completed"
            assert event["details"]["status"] == "succeeded"
            assert event["details"]["duration_seconds"] == 45.5
    
    def test_log_job_completed_failure(self, audit_logger):
        """Should log job completed with failed status."""
        with patch.object(audit_logger._audit_logger, 'info') as mock_info:
            audit_logger.log_job_completed(
                workflow_id="wf-123",
                alert_name="TestAlert",
                rundeck_execution_id="exec-456",
                status="failed"
            )
            
            logged_json = mock_info.call_args[0][0]
            event = json.loads(logged_json)
            
            assert event["event_type"] == "job_failed"
    
    def test_log_remediation_outcome_success(self, audit_logger):
        """Should log successful remediation outcome."""
        with patch.object(audit_logger._audit_logger, 'info') as mock_info:
            audit_logger.log_remediation_outcome(
                workflow_id="wf-123",
                alert_name="TestAlert",
                outcome="success",
                jira_ticket_id="JIRA-123"
            )
            
            logged_json = mock_info.call_args[0][0]
            event = json.loads(logged_json)
            
            assert event["event_type"] == "alert_resolved"
            assert event["jira_ticket_id"] == "JIRA-123"
    
    def test_log_remediation_outcome_failure(self, audit_logger):
        """Should log failed remediation outcome."""
        with patch.object(audit_logger._audit_logger, 'info') as mock_info:
            audit_logger.log_remediation_outcome(
                workflow_id="wf-123",
                alert_name="TestAlert",
                outcome="failure",
                reason="Alert still firing after remediation"
            )
            
            logged_json = mock_info.call_args[0][0]
            event = json.loads(logged_json)
            
            assert event["event_type"] == "alert_still_firing"
    
    def test_log_escalation(self, audit_logger):
        """Should log escalation event."""
        with patch.object(audit_logger._audit_logger, 'info') as mock_info:
            audit_logger.log_escalation(
                workflow_id="wf-123",
                alert_name="TestAlert",
                reason="Rundeck job failed",
                channels=["slack", "jira"],
                jira_ticket_id="JIRA-123"
            )
            
            logged_json = mock_info.call_args[0][0]
            event = json.loads(logged_json)
            
            assert event["event_type"] == "escalation_sent"
            assert event["details"]["channels"] == ["slack", "jira"]
    
    def test_log_circuit_breaker_change_opened(self, audit_logger):
        """Should log circuit breaker opened event."""
        with patch.object(audit_logger._audit_logger, 'info') as mock_info:
            audit_logger.log_circuit_breaker_change(
                service="rundeck",
                is_open=True,
                failure_count=5
            )
            
            logged_json = mock_info.call_args[0][0]
            event = json.loads(logged_json)
            
            assert event["event_type"] == "circuit_breaker_opened"
            assert event["details"]["service"] == "rundeck"
            assert event["details"]["failure_count"] == 5
    
    def test_log_circuit_breaker_change_closed(self, audit_logger):
        """Should log circuit breaker closed event."""
        with patch.object(audit_logger._audit_logger, 'info') as mock_info:
            audit_logger.log_circuit_breaker_change(
                service="rundeck",
                is_open=False,
                failure_count=0
            )
            
            logged_json = mock_info.call_args[0][0]
            event = json.loads(logged_json)
            
            assert event["event_type"] == "circuit_breaker_closed"
    
    def test_log_workflow_recovered(self, audit_logger):
        """Should log workflow recovered event."""
        with patch.object(audit_logger._audit_logger, 'info') as mock_info:
            audit_logger.log_workflow_recovered(
                workflow_id="wf-123",
                alert_name="TestAlert",
                previous_state="job_running"
            )
            
            logged_json = mock_info.call_args[0][0]
            event = json.loads(logged_json)
            
            assert event["event_type"] == "workflow_recovered"
            assert event["details"]["previous_state"] == "job_running"


class TestGetAuditLogger:
    """Tests for get_audit_logger singleton."""
    
    def test_returns_same_instance(self):
        """Should return the same instance on multiple calls."""
        from hermes.utils import audit_logger as module
        
        # Reset singleton
        module._audit_logger = None
        
        logger1 = get_audit_logger()
        logger2 = get_audit_logger()
        
        assert logger1 is logger2
