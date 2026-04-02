"""Tests for conditional value mappings in alert processing."""
import pytest
from unittest.mock import MagicMock
from hermes.api.app import AlertProcessor
from hermes.config import Config, AlertConfig, AlertRemediationConfig


@pytest.fixture
def mock_config_with_value_mappings():
    """Config with value mappings configured."""
    config = MagicMock(spec=Config)
    
    # Alert config with value mappings
    alert_config = AlertConfig(
        job_id="test-job-123",
        required_fields=["region", "instance"],
        fields_location="commonLabels",
        remediation=AlertRemediationConfig(enabled=True),
        value_mappings={
            "region": {
                "us-west-2": {
                    "cluster": "us-west-2-prod-new",
                    "env": "production"
                },
                "eu-west-1": {
                    "cluster_name": "eu-west-1-prod",
                    "env": "production"
                },
                "ap-south-1": {
                    "cluster": "ap-south-1-prod",
                    "env": "production"
                }
            }
        }
    )
    
    config.get_alert_config.return_value = alert_config
    return config


@pytest.fixture
def mock_config_without_value_mappings():
    """Config without value mappings."""
    config = MagicMock(spec=Config)
    
    alert_config = AlertConfig(
        job_id="test-job-123",
        required_fields=["region", "instance"],
        fields_location="commonLabels",
        remediation=AlertRemediationConfig(enabled=True)
    )
    
    config.get_alert_config.return_value = alert_config
    return config


@pytest.fixture
def mock_rundeck_client():
    """Mock Rundeck client."""
    return MagicMock()


def test_value_mapping_us_west_2(mock_config_with_value_mappings, mock_rundeck_client):
    """Test value mapping for us-west-2 region."""
    processor = AlertProcessor(
        config=mock_config_with_value_mappings,
        rundeck=mock_rundeck_client,
        jira=None
    )
    
    # Alert payload with region=us-west-2
    alert_payload = {
        "commonLabels": {
            "alertname": "TestAlert",
            "region": "us-west-2",
            "instance": "i-12345"
        },
        "alerts": []
    }
    
    result, alert_name, missing_fields = processor.process_alert(alert_payload)
    
    # Verify the value mapping was applied
    assert result["region"] == "us-west-2"
    assert result["instance"] == "i-12345"
    assert result["cluster"] == "us-west-2-prod-new"
    assert result["env"] == "production"
    assert missing_fields == []


def test_value_mapping_eu_west_1(mock_config_with_value_mappings, mock_rundeck_client):
    """Test value mapping for eu-west-1 region."""
    processor = AlertProcessor(
        config=mock_config_with_value_mappings,
        rundeck=mock_rundeck_client,
        jira=None
    )
    
    # Alert payload with region=eu-west-1
    alert_payload = {
        "commonLabels": {
            "alertname": "TestAlert",
            "region": "eu-west-1",
            "instance": "i-67890"
        },
        "alerts": []
    }
    
    result, alert_name, missing_fields = processor.process_alert(alert_payload)
    
    # Verify the value mapping was applied - note different option name
    assert result["region"] == "eu-west-1"
    assert result["instance"] == "i-67890"
    assert result["cluster_name"] == "eu-west-1-prod"  # Different option name
    assert result["env"] == "production"
    assert "cluster" not in result  # Should not have "cluster" key
    assert missing_fields == []


def test_value_mapping_ap_south_1(mock_config_with_value_mappings, mock_rundeck_client):
    """Test value mapping for ap-south-1 region."""
    processor = AlertProcessor(
        config=mock_config_with_value_mappings,
        rundeck=mock_rundeck_client,
        jira=None
    )
    
    # Alert payload with region=ap-south-1
    alert_payload = {
        "commonLabels": {
            "alertname": "TestAlert",
            "region": "ap-south-1",
            "instance": "i-99999"
        },
        "alerts": []
    }
    
    result, alert_name, missing_fields = processor.process_alert(alert_payload)
    
    # Verify the value mapping was applied
    assert result["region"] == "ap-south-1"
    assert result["instance"] == "i-99999"
    assert result["cluster"] == "ap-south-1-prod"
    assert result["env"] == "production"
    assert missing_fields == []


def test_value_mapping_unmapped_region(mock_config_with_value_mappings, mock_rundeck_client):
    """Test that unmapped region values don't break processing."""
    processor = AlertProcessor(
        config=mock_config_with_value_mappings,
        rundeck=mock_rundeck_client,
        jira=None
    )
    
    # Alert payload with unmapped region
    alert_payload = {
        "commonLabels": {
            "alertname": "TestAlert",
            "region": "cn-north-1",  # Not in mappings
            "instance": "i-11111"
        },
        "alerts": []
    }
    
    result, alert_name, missing_fields = processor.process_alert(alert_payload)
    
    # Verify base fields are present but no mappings applied
    assert result["region"] == "cn-north-1"
    assert result["instance"] == "i-11111"
    assert "cluster" not in result
    assert "cluster_name" not in result
    assert "env" not in result
    assert missing_fields == []


def test_no_value_mappings_configured(mock_config_without_value_mappings, mock_rundeck_client):
    """Test normal processing when no value mappings are configured."""
    processor = AlertProcessor(
        config=mock_config_without_value_mappings,
        rundeck=mock_rundeck_client,
        jira=None
    )
    
    # Alert payload
    alert_payload = {
        "commonLabels": {
            "alertname": "TestAlert",
            "region": "us-west-2",
            "instance": "i-12345"
        },
        "alerts": []
    }
    
    result, alert_name, missing_fields = processor.process_alert(alert_payload)
    
    # Verify only base fields are present
    assert result["region"] == "us-west-2"
    assert result["instance"] == "i-12345"
    assert "cluster" not in result
    assert "cluster_name" not in result
    assert missing_fields == []


def test_value_mapping_from_alerts_labels_fallback(mock_config_with_value_mappings, mock_rundeck_client):
    """Test value mapping works when field is in alerts[0].labels (fallback location)."""
    processor = AlertProcessor(
        config=mock_config_with_value_mappings,
        rundeck=mock_rundeck_client,
        jira=None
    )
    
    # Alert payload with region in alerts[0].labels instead of commonLabels
    alert_payload = {
        "commonLabels": {
            "alertname": "TestAlert"
        },
        "alerts": [{
            "labels": {
                "alertname": "TestAlert",
                "region": "us-west-2",
                "instance": "i-12345"
            }
        }]
    }
    
    result, alert_name, missing_fields = processor.process_alert(alert_payload)
    
    # Verify the value mapping was applied from fallback location
    assert result["region"] == "us-west-2"
    assert result["instance"] == "i-12345"
    assert result["cluster"] == "us-west-2-prod-new"
    assert result["env"] == "production"
    assert missing_fields == []


def test_value_mapping_with_field_mappings_combined():
    """Test that value mappings work together with field_mappings."""
    config = MagicMock(spec=Config)
    
    # Alert config with both field_mappings and value_mappings
    alert_config = AlertConfig(
        job_id="test-job-123",
        required_fields=["region", "instance"],
        fields_location="commonLabels",
        remediation=AlertRemediationConfig(enabled=True),
        field_mappings={
            "instance": "instance_id"  # Rename instance -> instance_id
        },
        value_mappings={
            "region": {
                "us-west-2": {
                    "cluster": "us-west-2-prod-new"
                }
            }
        }
    )
    
    config.get_alert_config.return_value = alert_config
    
    processor = AlertProcessor(
        config=config,
        rundeck=MagicMock(),
        jira=None
    )
    
    alert_payload = {
        "commonLabels": {
            "alertname": "TestAlert",
            "region": "us-west-2",
            "instance": "i-12345"
        },
        "alerts": []
    }
    
    result, alert_name, missing_fields = processor.process_alert(alert_payload)
    
    # Verify both field mapping and value mapping are applied
    assert result["region"] == "us-west-2"
    assert result["instance_id"] == "i-12345"  # Field mapping applied
    assert result["cluster"] == "us-west-2-prod-new"  # Value mapping applied
    assert "instance" not in result  # Original field name should be mapped
    assert missing_fields == []
