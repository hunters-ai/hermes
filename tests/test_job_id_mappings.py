"""Tests for conditional job_id routing based on alert field values."""
import pytest
from unittest.mock import MagicMock, AsyncMock
from hermes.api.app import AlertProcessor
from hermes.config import Config, AlertConfig, AlertRemediationConfig


@pytest.fixture
def mock_config_with_job_id_mappings():
    """Config with job_id_mappings configured."""
    config = MagicMock(spec=Config)
    
    # Alert config with job_id_mappings
    alert_config = AlertConfig(
        job_id="default-job-uuid",  # Fallback
        required_fields=["dataflow_id", "cluster", "error_message"],
        fields_location="commonLabels",
        remediation=AlertRemediationConfig(enabled=True),
        job_id_mappings={
            "error_message": {
                "Data-collection-vendor-is-down": "07262d42-6883-40be-8af2-69a19ed0744d",
                "Data-collection-got-rate-limit-error": "07262d42-6883-40be-8af2-69a19ed0744d",
                "Active-dataflow-is-not-running-or-failed": "c215a485-b533-4441-a722-1073be2a7cb8"
            }
        }
    )
    
    config.get_alert_config.return_value = alert_config
    return config


@pytest.fixture
def mock_config_without_job_id_mappings():
    """Config without job_id_mappings (standard config)."""
    config = MagicMock(spec=Config)
    
    alert_config = AlertConfig(
        job_id="standard-job-uuid",
        required_fields=["dataflow_id", "cluster"],
        fields_location="commonLabels",
        remediation=AlertRemediationConfig(enabled=True)
    )
    
    config.get_alert_config.return_value = alert_config
    return config


@pytest.fixture
def mock_rundeck_client():
    """Mock Rundeck client."""
    mock = MagicMock()
    mock.run_job = AsyncMock(return_value={"id": "12345", "permalink": "http://rundeck/execution/12345"})
    return mock


def test_job_id_mapping_vendor_down(mock_config_with_job_id_mappings, mock_rundeck_client):
    """Test job_id mapping for Data-collection-vendor-is-down error."""
    processor = AlertProcessor(
        config=mock_config_with_job_id_mappings,
        rundeck=mock_rundeck_client,
        jira=None
    )
    
    # Alert payload with error_message=Data-collection-vendor-is-down
    alert_payload = {
        "commonLabels": {
            "alertname": "FN Dataflow Internal Issues",
            "dataflow_id": "df-12345",
            "cluster": "prod-cluster",
            "error_message": "Data-collection-vendor-is-down"
        },
        "alerts": []
    }
    
    result, alert_name, missing_fields = processor.process_alert(alert_payload)
    
    # Verify the fields were extracted
    assert result["dataflow_id"] == "df-12345"
    assert result["cluster"] == "prod-cluster"
    assert result["error_message"] == "Data-collection-vendor-is-down"
    assert missing_fields == []
    
    # Verify the correct job_id is resolved
    source_map = alert_payload["commonLabels"]
    alerts_labels = {}
    resolved_job_id = processor._resolve_job_id(
        alert_name="FN Dataflow Internal Issues",
        alert_config=mock_config_with_job_id_mappings.get_alert_config.return_value,
        payload=result,
        source_map=source_map,
        alerts_labels=alerts_labels
    )
    
    assert resolved_job_id == "07262d42-6883-40be-8af2-69a19ed0744d"


def test_job_id_mapping_rate_limit(mock_config_with_job_id_mappings, mock_rundeck_client):
    """Test job_id mapping for Data-collection-got-rate-limit-error (same job as vendor-down)."""
    processor = AlertProcessor(
        config=mock_config_with_job_id_mappings,
        rundeck=mock_rundeck_client,
        jira=None
    )
    
    # Alert payload with error_message=Data-collection-got-rate-limit-error
    alert_payload = {
        "commonLabels": {
            "alertname": "FN Dataflow Internal Issues",
            "dataflow_id": "df-67890",
            "cluster": "prod-cluster",
            "error_message": "Data-collection-got-rate-limit-error"
        },
        "alerts": []
    }
    
    result, alert_name, missing_fields = processor.process_alert(alert_payload)
    
    # Verify the correct job_id is resolved
    source_map = alert_payload["commonLabels"]
    alerts_labels = {}
    resolved_job_id = processor._resolve_job_id(
        alert_name="FN Dataflow Internal Issues",
        alert_config=mock_config_with_job_id_mappings.get_alert_config.return_value,
        payload=result,
        source_map=source_map,
        alerts_labels=alerts_labels
    )
    
    assert resolved_job_id == "07262d42-6883-40be-8af2-69a19ed0744d"


def test_job_id_mapping_dataflow_failed(mock_config_with_job_id_mappings, mock_rundeck_client):
    """Test job_id mapping for Active-dataflow-is-not-running-or-failed error."""
    processor = AlertProcessor(
        config=mock_config_with_job_id_mappings,
        rundeck=mock_rundeck_client,
        jira=None
    )
    
    # Alert payload with error_message=Active-dataflow-is-not-running-or-failed
    alert_payload = {
        "commonLabels": {
            "alertname": "FN Dataflow Internal Issues",
            "dataflow_id": "df-99999",
            "cluster": "prod-cluster",
            "error_message": "Active-dataflow-is-not-running-or-failed"
        },
        "alerts": []
    }
    
    result, alert_name, missing_fields = processor.process_alert(alert_payload)
    
    # Verify the correct job_id is resolved
    source_map = alert_payload["commonLabels"]
    alerts_labels = {}
    resolved_job_id = processor._resolve_job_id(
        alert_name="FN Dataflow Internal Issues",
        alert_config=mock_config_with_job_id_mappings.get_alert_config.return_value,
        payload=result,
        source_map=source_map,
        alerts_labels=alerts_labels
    )
    
    assert resolved_job_id == "c215a485-b533-4441-a722-1073be2a7cb8"


def test_job_id_mapping_fallback_to_default(mock_config_with_job_id_mappings, mock_rundeck_client):
    """Test fallback to default job_id when error_message doesn't match any mapping."""
    processor = AlertProcessor(
        config=mock_config_with_job_id_mappings,
        rundeck=mock_rundeck_client,
        jira=None
    )
    
    # Alert payload with unmapped error_message
    alert_payload = {
        "commonLabels": {
            "alertname": "FN Dataflow Internal Issues",
            "dataflow_id": "df-11111",
            "cluster": "prod-cluster",
            "error_message": "Unknown-error-type"
        },
        "alerts": []
    }
    
    result, alert_name, missing_fields = processor.process_alert(alert_payload)
    
    # Verify falls back to default job_id
    source_map = alert_payload["commonLabels"]
    alerts_labels = {}
    resolved_job_id = processor._resolve_job_id(
        alert_name="FN Dataflow Internal Issues",
        alert_config=mock_config_with_job_id_mappings.get_alert_config.return_value,
        payload=result,
        source_map=source_map,
        alerts_labels=alerts_labels
    )
    
    assert resolved_job_id == "default-job-uuid"


def test_no_job_id_mappings_uses_default(mock_config_without_job_id_mappings, mock_rundeck_client):
    """Test that alerts without job_id_mappings use the standard job_id."""
    processor = AlertProcessor(
        config=mock_config_without_job_id_mappings,
        rundeck=mock_rundeck_client,
        jira=None
    )
    
    # Alert payload without job_id_mappings configured
    alert_payload = {
        "commonLabels": {
            "alertname": "Standard Alert",
            "dataflow_id": "df-22222",
            "cluster": "prod-cluster"
        },
        "alerts": []
    }
    
    result, alert_name, missing_fields = processor.process_alert(alert_payload)
    
    # Verify uses standard job_id
    source_map = alert_payload["commonLabels"]
    alerts_labels = {}
    resolved_job_id = processor._resolve_job_id(
        alert_name="Standard Alert",
        alert_config=mock_config_without_job_id_mappings.get_alert_config.return_value,
        payload=result,
        source_map=source_map,
        alerts_labels=alerts_labels
    )
    
    assert resolved_job_id == "standard-job-uuid"


def test_job_id_mapping_field_in_alerts_labels(mock_config_with_job_id_mappings, mock_rundeck_client):
    """Test job_id mapping when error_message is in alerts[0].labels instead of commonLabels."""
    processor = AlertProcessor(
        config=mock_config_with_job_id_mappings,
        rundeck=mock_rundeck_client,
        jira=None
    )
    
    # Alert payload with error_message in alerts[0].labels
    alert_payload = {
        "commonLabels": {
            "alertname": "FN Dataflow Internal Issues",
            "dataflow_id": "df-33333",
            "cluster": "prod-cluster"
        },
        "alerts": [
            {
                "labels": {
                    "error_message": "Data-collection-vendor-is-down"
                }
            }
        ]
    }
    
    result, alert_name, missing_fields = processor.process_alert(alert_payload)
    
    # Verify the correct job_id is resolved from alerts[0].labels
    source_map = alert_payload["commonLabels"]
    alerts_labels = alert_payload["alerts"][0]["labels"] if alert_payload.get("alerts") else {}
    resolved_job_id = processor._resolve_job_id(
        alert_name="FN Dataflow Internal Issues",
        alert_config=mock_config_with_job_id_mappings.get_alert_config.return_value,
        payload=result,
        source_map=source_map,
        alerts_labels=alerts_labels
    )
    
    assert resolved_job_id == "07262d42-6883-40be-8af2-69a19ed0744d"
