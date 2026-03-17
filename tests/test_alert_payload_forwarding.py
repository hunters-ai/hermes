"""Tests for alert payload forwarding to Rundeck jobs."""
import pytest
import json
from unittest.mock import AsyncMock, patch, MagicMock
from hermes.api.app import AlertProcessor
from hermes.config import Config, AlertConfig, AlertRemediationConfig


@pytest.fixture
def mock_config_with_payload_forwarding():
    """Config with alert payload forwarding enabled."""
    config = MagicMock(spec=Config)
    
    # Alert config with payload forwarding enabled
    alert_config = AlertConfig(
        job_id="test-job-123",
        required_fields=["cluster", "node", "region"],
        fields_location="commonLabels",
        remediation=AlertRemediationConfig(
            enabled=True,
            send_alert_payload=True,
            alert_payload_option_name="alert_payload"
        )
    )
    
    config.get_alert_config.return_value = alert_config
    return config


@pytest.fixture
def mock_config_without_payload_forwarding():
    """Config with alert payload forwarding disabled."""
    config = MagicMock(spec=Config)
    
    # Alert config with payload forwarding disabled
    alert_config = AlertConfig(
        job_id="test-job-123",
        required_fields=["cluster", "node", "region"],
        fields_location="commonLabels",
        remediation=AlertRemediationConfig(
            enabled=True,
            send_alert_payload=False,
            alert_payload_option_name="alert_payload"
        )
    )
    
    config.get_alert_config.return_value = alert_config
    return config


@pytest.mark.asyncio
async def test_send_alert_payload_when_enabled(mock_config_with_payload_forwarding):
    """Test that alert payload is sent to Rundeck when enabled."""
    # Mock Rundeck client
    mock_rundeck = AsyncMock()
    mock_rundeck.run_job.return_value = {
        "id": "12345",
        "permalink": "https://rundeck.example.com/project/ops/execution/show/12345"
    }
    
    processor = AlertProcessor(
        config=mock_config_with_payload_forwarding,
        rundeck=mock_rundeck,
        jira=None
    )
    
    # Sample alert context
    alert_name = "NodeNotReady"
    processed_payload = {
        "cluster": "us-west-2-prod",
        "node": "ip-10-0-1-50",
        "region": "us-west-2"
    }
    alert_time = "2026-01-29T12:00:00Z"
    
    full_alert_context = {
        "alert_name": alert_name,
        "alert_labels": {
            "alertname": alert_name,
            "cluster": "us-west-2-prod",
            "node": "ip-10-0-1-50",
            "region": "us-west-2",
            "severity": "warning"
        },
        "alert_time": alert_time,
        "source_alertmanager": "https://alertmanager.us-west-2.example.com",
        "processed_options": processed_payload
    }
    
    # Call send_to_webhook
    result = await processor.send_to_webhook(
        alert_name=alert_name,
        payload=processed_payload.copy(),
        alert_time=alert_time,
        full_alert_context=full_alert_context
    )
    
    # Verify Rundeck was called with the payload
    mock_rundeck.run_job.assert_called_once()
    call_args = mock_rundeck.run_job.call_args
    
    # Check job options contain the alert payload as JSON string
    job_options = call_args.args[1]
    assert "alert_payload" in job_options
    
    # Verify it's a JSON string
    payload_json = job_options["alert_payload"]
    assert isinstance(payload_json, str)
    
    # Verify it can be parsed back to dict
    parsed_payload = json.loads(payload_json)
    assert parsed_payload["alert_name"] == alert_name
    assert parsed_payload["alert_labels"]["cluster"] == "us-west-2-prod"
    assert parsed_payload["source_alertmanager"] == "https://alertmanager.us-west-2.example.com"
    
    # Verify execution info is returned
    assert result["execution_id"] == "12345"
    assert "rundeck.example.com" in result["execution_url"]


@pytest.mark.asyncio
async def test_no_alert_payload_when_disabled(mock_config_without_payload_forwarding):
    """Test that alert payload is NOT sent when disabled."""
    # Mock Rundeck client
    mock_rundeck = AsyncMock()
    mock_rundeck.run_job.return_value = {
        "id": "12345",
        "permalink": "https://rundeck.example.com/project/ops/execution/show/12345"
    }
    
    processor = AlertProcessor(
        config=mock_config_without_payload_forwarding,
        rundeck=mock_rundeck,
        jira=None
    )
    
    # Sample alert context
    alert_name = "NodeNotReady"
    processed_payload = {
        "cluster": "us-west-2-prod",
        "node": "ip-10-0-1-50",
        "region": "us-west-2"
    }
    alert_time = "2026-01-29T12:00:00Z"
    
    full_alert_context = {
        "alert_name": alert_name,
        "alert_labels": {"cluster": "us-west-2-prod"},
        "alert_time": alert_time,
        "source_alertmanager": "https://alertmanager.us-west-2.example.com"
    }
    
    # Call send_to_webhook
    result = await processor.send_to_webhook(
        alert_name=alert_name,
        payload=processed_payload.copy(),
        alert_time=alert_time,
        full_alert_context=full_alert_context
    )
    
    # Verify Rundeck was called
    mock_rundeck.run_job.assert_called_once()
    call_args = mock_rundeck.run_job.call_args
    
    # Check job options do NOT contain the alert payload
    job_options = call_args.args[1]
    assert "alert_payload" not in job_options
    
    # Only required fields should be present
    assert "cluster" in job_options
    assert "node" in job_options
    assert "region" in job_options


@pytest.mark.asyncio
async def test_custom_payload_option_name(mock_config_with_payload_forwarding):
    """Test that custom option name for alert payload is respected."""
    # Modify config to use custom option name
    alert_config = mock_config_with_payload_forwarding.get_alert_config()
    alert_config.remediation.alert_payload_option_name = "custom_alert_data"
    
    # Mock Rundeck client
    mock_rundeck = AsyncMock()
    mock_rundeck.run_job.return_value = {
        "id": "12345",
        "permalink": "https://rundeck.example.com/project/ops/execution/show/12345"
    }
    
    processor = AlertProcessor(
        config=mock_config_with_payload_forwarding,
        rundeck=mock_rundeck,
        jira=None
    )
    
    # Sample data
    alert_name = "NodeNotReady"
    processed_payload = {"cluster": "us-west-2-prod"}
    full_alert_context = {
        "alert_name": alert_name,
        "alert_labels": {"cluster": "us-west-2-prod"}
    }
    
    # Call send_to_webhook
    await processor.send_to_webhook(
        alert_name=alert_name,
        payload=processed_payload.copy(),
        alert_time="2026-01-29T12:00:00Z",
        full_alert_context=full_alert_context
    )
    
    # Verify custom option name is used
    call_args = mock_rundeck.run_job.call_args
    job_options = call_args.args[1]
    
    assert "custom_alert_data" in job_options
    assert "alert_payload" not in job_options
    
    # Verify it's valid JSON
    parsed = json.loads(job_options["custom_alert_data"])
    assert parsed["alert_name"] == alert_name
