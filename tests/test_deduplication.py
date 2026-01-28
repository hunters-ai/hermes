"""Tests for alert deduplication and cooldown functionality (P0, P1)."""
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

from hermes.core.remediation_manager import RemediationManager, AlertCooldown
from hermes.config import Config, RemediationConfig
from hermes.core.state_store import InMemoryStateStore, RemediationWorkflow, RemediationState


class TestAlertFingerprint:
    """Tests for alert fingerprint generation."""
    
    @pytest.fixture
    def manager(self):
        """Create RemediationManager for testing."""
        config = MagicMock(spec=Config)
        config.remediation = RemediationConfig()
        config.alertmanager = None
        config.jira = None
        config.slack = None
        
        state_store = InMemoryStateStore()
        rundeck_client = MagicMock()
        
        return RemediationManager(config, state_store, rundeck_client)
    
    def test_fingerprint_generation(self, manager):
        """Should generate consistent fingerprint for same alert."""
        fp1 = manager._get_alert_fingerprint(
            "TestAlert",
            {"cluster": "prod", "namespace": "default"}
        )
        fp2 = manager._get_alert_fingerprint(
            "TestAlert",
            {"cluster": "prod", "namespace": "default"}
        )
        
        assert fp1 == fp2
    
    def test_fingerprint_different_for_different_labels(self, manager):
        """Should generate different fingerprint for different labels."""
        fp1 = manager._get_alert_fingerprint(
            "TestAlert",
            {"cluster": "prod"}
        )
        fp2 = manager._get_alert_fingerprint(
            "TestAlert",
            {"cluster": "staging"}
        )
        
        assert fp1 != fp2
    
    def test_fingerprint_different_for_different_alert_names(self, manager):
        """Should generate different fingerprint for different alert names."""
        fp1 = manager._get_alert_fingerprint(
            "Alert1",
            {"cluster": "prod"}
        )
        fp2 = manager._get_alert_fingerprint(
            "Alert2",
            {"cluster": "prod"}
        )
        
        assert fp1 != fp2
    
    def test_fingerprint_label_order_independent(self, manager):
        """Fingerprint should be the same regardless of label order."""
        fp1 = manager._get_alert_fingerprint(
            "TestAlert",
            {"a": "1", "b": "2", "c": "3"}
        )
        fp2 = manager._get_alert_fingerprint(
            "TestAlert",
            {"c": "3", "a": "1", "b": "2"}
        )
        
        assert fp1 == fp2


class TestDeduplication:
    """Tests for check_deduplication method."""
    
    @pytest.fixture
    def mock_config(self):
        """Create mock config."""
        config = MagicMock(spec=Config)
        config.remediation = RemediationConfig(
            cooldown_minutes=5,
            max_concurrent_workflows=100
        )
        config.alertmanager = None
        config.jira = None
        config.slack = None
        return config
    
    @pytest.fixture
    def state_store(self):
        """Create in-memory state store."""
        return InMemoryStateStore()
    
    @pytest.fixture
    def mock_rundeck_client(self):
        """Create mock Rundeck client."""
        return MagicMock()
    
    @pytest.mark.asyncio
    async def test_allow_new_alert(self, mock_config, state_store, mock_rundeck_client):
        """Should allow new alerts with no active workflow."""
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        should_skip, reason, workflow_id = await manager.check_deduplication(
            "NewAlert",
            {"cluster": "prod"}
        )
        
        assert should_skip is False
        assert reason is None
        assert workflow_id is None
    
    @pytest.mark.asyncio
    async def test_skip_duplicate_active_workflow(self, mock_config, state_store, mock_rundeck_client):
        """Should skip alerts with an active workflow."""
        # Create active workflow
        workflow = RemediationWorkflow(
            id="existing-workflow",
            alert_name="TestAlert",
            alert_labels={"cluster": "prod"},
            state=RemediationState.JOB_RUNNING,
            rundeck_execution_id="exec-123"
        )
        await state_store.save(workflow)
        
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        should_skip, reason, workflow_id = await manager.check_deduplication(
            "TestAlert",
            {"cluster": "prod"}
        )
        
        assert should_skip is True
        assert "Active workflow" in reason
        assert workflow_id == "existing-workflow"
    
    @pytest.mark.asyncio
    async def test_skip_alert_in_cooldown(self, mock_config, state_store, mock_rundeck_client):
        """Should skip alerts that are in cooldown period."""
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        # Simulate cooldown
        fingerprint = manager._get_alert_fingerprint("TestAlert", {"cluster": "prod"})
        manager._alert_cooldowns[fingerprint] = AlertCooldown(
            last_triggered=datetime.utcnow(),  # Just triggered
            workflow_id="previous-workflow"
        )
        
        should_skip, reason, workflow_id = await manager.check_deduplication(
            "TestAlert",
            {"cluster": "prod"}
        )
        
        assert should_skip is True
        assert "cooldown" in reason.lower()
        assert workflow_id == "previous-workflow"
    
    @pytest.mark.asyncio
    async def test_allow_alert_after_cooldown_expires(self, mock_config, state_store, mock_rundeck_client):
        """Should allow alerts after cooldown period expires."""
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        # Simulate expired cooldown (10 minutes ago, cooldown is 5 minutes)
        fingerprint = manager._get_alert_fingerprint("TestAlert", {"cluster": "prod"})
        manager._alert_cooldowns[fingerprint] = AlertCooldown(
            last_triggered=datetime.utcnow() - timedelta(minutes=10),
            workflow_id="previous-workflow"
        )
        
        should_skip, reason, workflow_id = await manager.check_deduplication(
            "TestAlert",
            {"cluster": "prod"}
        )
        
        assert should_skip is False
    
    @pytest.mark.asyncio
    async def test_skip_when_max_concurrent_reached(self, mock_config, state_store, mock_rundeck_client):
        """Should skip when max concurrent workflows is reached."""
        mock_config.remediation.max_concurrent_workflows = 2
        
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        # Simulate running tasks
        manager._running_tasks["task1"] = MagicMock()
        manager._running_tasks["task2"] = MagicMock()
        
        should_skip, reason, workflow_id = await manager.check_deduplication(
            "NewAlert",
            {"cluster": "prod"}
        )
        
        assert should_skip is True
        assert "Max concurrent workflows" in reason


class TestCooldownTracking:
    """Tests for cooldown tracking during workflow lifecycle."""
    
    @pytest.fixture
    def mock_config(self):
        """Create mock config."""
        config = MagicMock(spec=Config)
        config.remediation = RemediationConfig(cooldown_minutes=5)
        config.alertmanager = None
        config.jira = None
        config.slack = None
        config.get_alert_config.return_value = None
        return config
    
    @pytest.fixture
    def state_store(self):
        """Create in-memory state store."""
        return InMemoryStateStore()
    
    @pytest.fixture
    def mock_rundeck_client(self):
        """Create mock Rundeck client."""
        return MagicMock()
    
    @pytest.mark.asyncio
    async def test_cooldown_recorded_on_start_remediation(self, mock_config, state_store, mock_rundeck_client):
        """Should record cooldown when starting remediation."""
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        # Start remediation
        workflow_id = await manager.start_remediation(
            alert_name="TestAlert",
            alert_labels={"cluster": "prod"},
            rundeck_execution_id="exec-123"
        )
        
        # Check cooldown was recorded
        fingerprint = manager._get_alert_fingerprint("TestAlert", {"cluster": "prod"})
        assert fingerprint in manager._alert_cooldowns
        assert manager._alert_cooldowns[fingerprint].workflow_id == workflow_id
