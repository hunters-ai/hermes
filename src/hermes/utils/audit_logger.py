"""
Audit logging for remediation workflows.

Provides structured audit logging that can be sent to:
- CloudWatch Logs (default)
- S3 (for long-term storage)
- External logging services (Coralogix, Datadog, etc.)

Audit events are separate from operational logs and have longer retention.
"""
import json
import logging
from datetime import datetime
from typing import Dict, Any, Optional
from enum import Enum
from dataclasses import dataclass, asdict


logger = logging.getLogger(__name__)


class AuditEventType(str, Enum):
    """Types of audit events."""
    ALERT_RECEIVED = "alert_received"
    ALERT_DEDUPLICATED = "alert_deduplicated"
    WORKFLOW_STARTED = "workflow_started"
    JOB_TRIGGERED = "job_triggered"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    ALERT_RESOLVED = "alert_resolved"
    ALERT_STILL_FIRING = "alert_still_firing"
    ESCALATION_SENT = "escalation_sent"
    CIRCUIT_BREAKER_OPENED = "circuit_breaker_opened"
    CIRCUIT_BREAKER_CLOSED = "circuit_breaker_closed"
    WORKFLOW_RECOVERED = "workflow_recovered"


@dataclass
class AuditEvent:
    """Structured audit event."""
    timestamp: str
    event_type: str
    workflow_id: Optional[str]
    alert_name: Optional[str]
    alert_labels: Optional[Dict[str, str]]
    details: Dict[str, Any]
    source_alertmanager: Optional[str] = None
    rundeck_execution_id: Optional[str] = None
    jira_ticket_id: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


class AuditLogger:
    """
    Structured audit logger for remediation events.
    
    Outputs structured JSON logs that can be picked up by log aggregators.
    """
    
    def __init__(self, service_name: str = "hermes"):
        self.service_name = service_name
        self._audit_logger = logging.getLogger(f"{service_name}.audit")
        
        # Configure audit logger with JSON format
        if not self._audit_logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter('%(message)s'))
            self._audit_logger.addHandler(handler)
            self._audit_logger.setLevel(logging.INFO)
            self._audit_logger.propagate = False  # Don't propagate to root logger
    
    def _create_event(
        self,
        event_type: AuditEventType,
        workflow_id: Optional[str] = None,
        alert_name: Optional[str] = None,
        alert_labels: Optional[Dict[str, str]] = None,
        details: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> AuditEvent:
        return AuditEvent(
            timestamp=datetime.utcnow().isoformat() + "Z",
            event_type=event_type.value,
            workflow_id=workflow_id,
            alert_name=alert_name,
            alert_labels=alert_labels,
            details=details or {},
            **{k: v for k, v in kwargs.items() if v is not None}
        )
    
    def _log_event(self, event: AuditEvent):
        """Log the audit event as structured JSON."""
        self._audit_logger.info(event.to_json())
    
    def log_alert_received(
        self,
        alert_name: str,
        alert_labels: Dict[str, str],
        source_alertmanager: Optional[str] = None
    ):
        """Log when an alert is received."""
        event = self._create_event(
            AuditEventType.ALERT_RECEIVED,
            alert_name=alert_name,
            alert_labels=alert_labels,
            source_alertmanager=source_alertmanager,
            details={"action": "processing"}
        )
        self._log_event(event)
    
    def log_alert_deduplicated(
        self,
        alert_name: str,
        alert_labels: Dict[str, str],
        reason: str,
        existing_workflow_id: Optional[str] = None
    ):
        """Log when an alert is deduplicated (skipped)."""
        event = self._create_event(
            AuditEventType.ALERT_DEDUPLICATED,
            workflow_id=existing_workflow_id,
            alert_name=alert_name,
            alert_labels=alert_labels,
            details={"reason": reason}
        )
        self._log_event(event)
    
    def log_workflow_started(
        self,
        workflow_id: str,
        alert_name: str,
        alert_labels: Dict[str, str],
        rundeck_execution_id: str,
        source_alertmanager: Optional[str] = None
    ):
        """Log when a remediation workflow is started."""
        event = self._create_event(
            AuditEventType.WORKFLOW_STARTED,
            workflow_id=workflow_id,
            alert_name=alert_name,
            alert_labels=alert_labels,
            rundeck_execution_id=rundeck_execution_id,
            source_alertmanager=source_alertmanager,
            details={"state": "job_triggered"}
        )
        self._log_event(event)
    
    def log_job_completed(
        self,
        workflow_id: str,
        alert_name: str,
        rundeck_execution_id: str,
        status: str,
        duration_seconds: Optional[float] = None
    ):
        """Log when a Rundeck job completes."""
        event = self._create_event(
            AuditEventType.JOB_COMPLETED if status == "succeeded" else AuditEventType.JOB_FAILED,
            workflow_id=workflow_id,
            alert_name=alert_name,
            rundeck_execution_id=rundeck_execution_id,
            details={
                "status": status,
                "duration_seconds": duration_seconds
            }
        )
        self._log_event(event)
    
    def log_remediation_outcome(
        self,
        workflow_id: str,
        alert_name: str,
        outcome: str,
        jira_ticket_id: Optional[str] = None,
        reason: Optional[str] = None
    ):
        """Log the final outcome of a remediation workflow."""
        event_type = (
            AuditEventType.ALERT_RESOLVED 
            if outcome == "success" 
            else AuditEventType.ALERT_STILL_FIRING
        )
        event = self._create_event(
            event_type,
            workflow_id=workflow_id,
            alert_name=alert_name,
            jira_ticket_id=jira_ticket_id,
            details={
                "outcome": outcome,
                "reason": reason
            }
        )
        self._log_event(event)
    
    def log_escalation(
        self,
        workflow_id: str,
        alert_name: str,
        reason: str,
        channels: list,
        jira_ticket_id: Optional[str] = None
    ):
        """Log when an escalation is sent."""
        event = self._create_event(
            AuditEventType.ESCALATION_SENT,
            workflow_id=workflow_id,
            alert_name=alert_name,
            jira_ticket_id=jira_ticket_id,
            details={
                "reason": reason,
                "channels": channels
            }
        )
        self._log_event(event)
    
    def log_circuit_breaker_change(
        self,
        service: str,
        is_open: bool,
        failure_count: int
    ):
        """Log circuit breaker state changes."""
        event_type = (
            AuditEventType.CIRCUIT_BREAKER_OPENED 
            if is_open 
            else AuditEventType.CIRCUIT_BREAKER_CLOSED
        )
        event = self._create_event(
            event_type,
            details={
                "service": service,
                "is_open": is_open,
                "failure_count": failure_count
            }
        )
        self._log_event(event)
    
    def log_workflow_recovered(
        self,
        workflow_id: str,
        alert_name: str,
        previous_state: str
    ):
        """Log when a workflow is recovered after restart."""
        event = self._create_event(
            AuditEventType.WORKFLOW_RECOVERED,
            workflow_id=workflow_id,
            alert_name=alert_name,
            details={
                "previous_state": previous_state,
                "action": "resumed_monitoring"
            }
        )
        self._log_event(event)


# Singleton instance
_audit_logger: Optional[AuditLogger] = None


def get_audit_logger() -> AuditLogger:
    """Get or create the singleton audit logger."""
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger()
    return _audit_logger
