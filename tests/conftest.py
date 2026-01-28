"""Pytest configuration and fixtures for Hermes tests."""
import asyncio
import os
from typing import Generator
from unittest.mock import MagicMock

import pytest

from hermes.config import Config, RemediationConfig
from hermes.clients.rundeck import RundeckClient
from hermes.core.state_store import InMemoryStateStore, RemediationWorkflow, RemediationState


# Configure asyncio for pytest
@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_config():
    """Create a mock Config object for testing."""
    config = MagicMock(spec=Config)
    config.remediation = RemediationConfig(
        poll_interval_seconds=1,
        resolution_wait_minutes=1,
        max_job_wait_minutes=5,
        cooldown_minutes=5,
        max_concurrent_workflows=100,
        circuit_breaker_failure_threshold=5,
        circuit_breaker_recovery_seconds=60,
    )
    config.alertmanager = None
    config.jira = None
    config.slack = None
    config.get_alert_config.return_value = None
    return config


@pytest.fixture
def mock_rundeck_client():
    """Create a mock RundeckClient for testing."""
    client = MagicMock(spec=RundeckClient)
    client.run_job = MagicMock()
    client.get_execution = MagicMock()
    return client


@pytest.fixture
def in_memory_state_store():
    """Create an InMemoryStateStore for testing."""
    return InMemoryStateStore()


@pytest.fixture
def sample_workflow():
    """Create a sample RemediationWorkflow for testing."""
    return RemediationWorkflow(
        id="test-workflow-123",
        alert_name="TestAlert",
        alert_labels={"alertname": "TestAlert", "cluster": "prod", "namespace": "default"},
        state=RemediationState.WAITING_RESOLUTION,
        rundeck_execution_id="exec-456",
        alertmanager_url="http://alertmanager.example.com"
    )


@pytest.fixture
def sample_alert_payload():
    """Create a sample Alertmanager webhook payload."""
    return {
        "receiver": "hermes",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "TestAlert",
                    "severity": "critical",
                    "cluster": "prod"
                },
                "startsAt": "2024-01-01T00:00:00Z"
            }
        ],
        "commonLabels": {
            "alertname": "TestAlert",
            "severity": "critical",
            "cluster": "prod"
        },
        "externalURL": "http://alertmanager.example.com"
    }


# Environment setup
@pytest.fixture(autouse=True)
def setup_test_environment(tmp_path):
    """Set up test environment variables."""
    os.environ["CONFIG_PATH"] = str(tmp_path / "config.yaml")
    os.environ["DEBUG"] = "false"
    yield
    # Cleanup
    for key in ["CONFIG_PATH", "DEBUG"]:
        os.environ.pop(key, None)
