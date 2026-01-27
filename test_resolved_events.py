"""Unit tests for resolved event handling and alert resolution monitoring."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from state_store import InMemoryStateStore, RemediationWorkflow, RemediationState
from remediation_manager import RemediationManager
from config import Config, RemediationConfig, AlertConfig, AlertRemediationConfig


@pytest.fixture
def sample_workflow():
    """Create a sample workflow for testing."""
    return RemediationWorkflow(
        id="test-workflow-123",
        alert_name="TestAlert",
        alert_labels={"alertname": "TestAlert", "cluster": "prod", "namespace": "default"},
        state=RemediationState.WAITING_RESOLUTION,
        rundeck_execution_id="exec-456",
        alertmanager_url="http://alertmanager.example.com"
    )


@pytest.fixture
def state_store():
    """Create in-memory state store for testing."""
    return InMemoryStateStore()


class TestInMemoryStateStoreGetByAlertLabels:
    """Tests for InMemoryStateStore.get_by_alert_labels() method."""
    
    @pytest.mark.asyncio
    async def test_find_workflow_by_exact_labels(self, state_store, sample_workflow):
        """Should find workflow when labels match exactly."""
        await state_store.save(sample_workflow)
        
        result = await state_store.get_by_alert_labels(
            "TestAlert",
            {"alertname": "TestAlert", "cluster": "prod", "namespace": "default"}
        )
        
        assert result is not None
        assert result.id == "test-workflow-123"
    
    @pytest.mark.asyncio
    async def test_find_workflow_by_subset_labels(self, state_store, sample_workflow):
        """Should find workflow when query labels are a subset of stored labels."""
        await state_store.save(sample_workflow)
        
        # Query with fewer labels - should still match
        result = await state_store.get_by_alert_labels(
            "TestAlert",
            {"alertname": "TestAlert", "cluster": "prod"}
        )
        
        assert result is not None
        assert result.id == "test-workflow-123"
    
    @pytest.mark.asyncio
    async def test_no_match_wrong_alertname(self, state_store, sample_workflow):
        """Should not find workflow when alert name doesn't match."""
        await state_store.save(sample_workflow)
        
        result = await state_store.get_by_alert_labels(
            "DifferentAlert",
            {"alertname": "DifferentAlert", "cluster": "prod"}
        )
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_no_match_wrong_labels(self, state_store, sample_workflow):
        """Should not find workflow when labels don't match."""
        await state_store.save(sample_workflow)
        
        result = await state_store.get_by_alert_labels(
            "TestAlert",
            {"alertname": "TestAlert", "cluster": "staging"}  # Different cluster
        )
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_skip_completed_workflows(self, state_store, sample_workflow):
        """Should not return workflows in completed states."""
        sample_workflow.state = RemediationState.COMPLETED
        await state_store.save(sample_workflow)
        
        result = await state_store.get_by_alert_labels(
            "TestAlert",
            {"alertname": "TestAlert", "cluster": "prod"}
        )
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_skip_escalated_workflows(self, state_store, sample_workflow):
        """Should not return workflows in escalated state."""
        sample_workflow.state = RemediationState.ESCALATED
        await state_store.save(sample_workflow)
        
        result = await state_store.get_by_alert_labels(
            "TestAlert",
            {"alertname": "TestAlert", "cluster": "prod"}
        )
        
        assert result is None


class TestRemediationManagerHandleResolvedEvent:
    """Tests for RemediationManager.handle_resolved_event() method."""
    
    @pytest.fixture
    def mock_config(self):
        """Create mock config for testing."""
        config = MagicMock(spec=Config)
        config.remediation = RemediationConfig()
        config.alertmanager = None
        config.jira = None
        config.slack = None
        config.create_rundeck_client.return_value = MagicMock()
        config.get_alert_config.return_value = MagicMock(
            remediation=AlertRemediationConfig()
        )
        return config
    
    @pytest.mark.asyncio
    async def test_signal_resolution_event(self, mock_config, state_store, sample_workflow):
        """Should set resolution event when matching workflow found."""
        await state_store.save(sample_workflow)
        
        manager = RemediationManager(mock_config, state_store)
        
        # Simulate that workflow is being monitored
        resolution_event = asyncio.Event()
        manager._resolution_events[sample_workflow.id] = resolution_event
        
        # Handle resolved event
        result = await manager.handle_resolved_event(
            "TestAlert",
            {"alertname": "TestAlert", "cluster": "prod"}
        )
        
        assert result == sample_workflow.id
        assert resolution_event.is_set()
    
    @pytest.mark.asyncio
    async def test_no_matching_workflow(self, mock_config, state_store):
        """Should return None when no matching workflow found."""
        manager = RemediationManager(mock_config, state_store)
        
        result = await manager.handle_resolved_event(
            "NonExistentAlert",
            {"alertname": "NonExistentAlert"}
        )
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_workflow_not_being_monitored(self, mock_config, state_store, sample_workflow):
        """Should return None when workflow exists but not actively monitored."""
        await state_store.save(sample_workflow)
        
        manager = RemediationManager(mock_config, state_store)
        # Don't add to _resolution_events - simulating workflow not monitored
        
        result = await manager.handle_resolved_event(
            "TestAlert",
            {"alertname": "TestAlert", "cluster": "prod"}
        )
        
        assert result is None


class TestRemediationManagerWaitForAlertResolution:
    """Tests for RemediationManager._wait_for_alert_resolution() method."""
    
    @pytest.fixture
    def mock_config(self):
        """Create mock config for testing."""
        config = MagicMock(spec=Config)
        config.remediation = RemediationConfig(
            alert_check_interval_seconds=1,  # Fast polling for tests
            resolution_wait_minutes=1
        )
        config.alertmanager = None
        config.jira = None
        config.slack = None
        config.create_rundeck_client.return_value = MagicMock()
        config.get_alert_config.return_value = None
        return config
    
    @pytest.mark.asyncio
    async def test_early_resolution_via_event(self, mock_config, state_store, sample_workflow):
        """Should return True immediately when resolution event is set."""
        await state_store.save(sample_workflow)
        
        manager = RemediationManager(mock_config, state_store)
        
        resolution_event = asyncio.Event()
        resolution_event.set()  # Pre-set the event
        
        # Should return immediately without polling
        result = await manager._wait_for_alert_resolution(
            sample_workflow, 
            resolution_event,
            resolution_wait_minutes=5
        )
        
        assert result is True
    
    @pytest.mark.asyncio
    async def test_resolution_via_polling(self, mock_config, state_store, sample_workflow):
        """Should return True when polling confirms alert is resolved."""
        await state_store.save(sample_workflow)
        
        manager = RemediationManager(mock_config, state_store)
        manager._check_alert_resolved = AsyncMock(return_value=True)
        
        resolution_event = asyncio.Event()
        
        result = await manager._wait_for_alert_resolution(
            sample_workflow,
            resolution_event,
            resolution_wait_minutes=1
        )
        
        assert result is True
        manager._check_alert_resolved.assert_called()
    
    @pytest.mark.asyncio
    async def test_timeout_when_alert_still_firing(self, mock_config, state_store, sample_workflow):
        """Should return False when timeout reached and alert still firing."""
        await state_store.save(sample_workflow)
        
        # Very short timeout and interval for testing
        mock_config.remediation.alert_check_interval_seconds = 0.1
        
        manager = RemediationManager(mock_config, state_store)
        manager._check_alert_resolved = AsyncMock(return_value=False)
        
        resolution_event = asyncio.Event()
        
        # Very short wait for testing (0.01 minutes = 0.6 seconds)
        result = await manager._wait_for_alert_resolution(
            sample_workflow,
            resolution_event,
            resolution_wait_minutes=0.01
        )
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_event_set_during_wait(self, mock_config, state_store, sample_workflow):
        """Should return True when event is set during wait interval."""
        await state_store.save(sample_workflow)
        
        mock_config.remediation.alert_check_interval_seconds = 5  # Long interval
        
        manager = RemediationManager(mock_config, state_store)
        manager._check_alert_resolved = AsyncMock(return_value=False)
        
        resolution_event = asyncio.Event()
        
        # Set event after a short delay
        async def set_event_later():
            await asyncio.sleep(0.1)
            resolution_event.set()
        
        asyncio.create_task(set_event_later())
        
        result = await manager._wait_for_alert_resolution(
            sample_workflow,
            resolution_event,
            resolution_wait_minutes=5
        )
        
        assert result is True


# Integration test for the full flow via FastAPI
class TestResolvedWebhookEndpoint:
    """Tests for the /api/v1/alerts endpoint with resolved status."""
    
    def test_resolved_event_processing(self):
        """Test that resolved events are processed correctly."""
        from fastapi.testclient import TestClient
        from main import app, remediation_manager
        
        # Skip if remediation manager is not available
        if remediation_manager is None:
            pytest.skip("Remediation manager not configured")
        
        client = TestClient(app)
        
        resolved_payload = {
            "receiver": "test-receiver",
            "status": "resolved",
            "alerts": [
                {
                    "status": "resolved",
                    "labels": {
                        "alertname": "TestAlert",
                        "cluster": "prod"
                    },
                    "startsAt": "2023-01-01T00:00:00Z",
                    "endsAt": "2023-01-01T00:05:00Z"
                }
            ],
            "commonLabels": {
                "alertname": "TestAlert",
                "cluster": "prod"
            }
        }
        
        response = client.post("/api/v1/alerts", json=resolved_payload)
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["message"] == "Resolved event processed"
        assert "workflows_signaled" in data


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
