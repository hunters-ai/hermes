"""
Hermes - Swift messenger for automated alert remediation.

Named after the Greek god known for speed and as the messenger of the gods.
Hermes automates the remediation workflow:

1. Receives alerts from Alertmanager
2. Triggers Rundeck remediation jobs
3. Monitors job completion
4. Verifies alert resolution
5. Updates JIRA and Slack as needed

Features:
- Circuit breakers for external service resilience
- Alert deduplication with cooldown periods
- Workflow recovery after restarts
- Rate limiting per Alertmanager source
- Comprehensive audit logging
- Prometheus metrics
"""

__version__ = "1.0.0"
__author__ = "SRE Team"

# Convenience imports
from hermes.config import Config, load_alert_config
from hermes.core.remediation_manager import RemediationManager
from hermes.core.state_store import (
    StateStore,
    InMemoryStateStore,
    DynamoDBStateStore,
    RemediationWorkflow,
    RemediationState,
)
from hermes.core.job_monitor import JobMonitor

__all__ = [
    "Config",
    "load_alert_config",
    "RemediationManager",
    "StateStore",
    "InMemoryStateStore",
    "DynamoDBStateStore",
    "RemediationWorkflow",
    "RemediationState",
    "JobMonitor",
]
