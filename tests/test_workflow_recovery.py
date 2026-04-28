"""Tests for workflow recovery functionality (P1)."""
import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

from hermes.core.remediation_manager import RemediationManager
from hermes.config import Config, RemediationConfig, AlertConfig, AlertRemediationConfig
from hermes.core.state_store import InMemoryStateStore, RemediationWorkflow, RemediationState


class TestWorkflowRecovery:
    """Tests for recover_active_workflows method."""
    
    @pytest.fixture
    def mock_config(self):
        """Create mock config."""
        config = MagicMock(spec=Config)
        config.remediation = RemediationConfig(
            poll_interval_seconds=1,
            resolution_wait_minutes=1,
            max_job_wait_minutes=5
        )
        config.alertmanager = None
        config.jira = None
        config.slack = None
        config.get_alert_config.return_value = MagicMock(
            remediation=AlertRemediationConfig()
        )
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
    async def test_recover_no_active_workflows(self, mock_config, state_store, mock_rundeck_client):
        """Should return 0 when no active workflows exist."""
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        recovered = await manager.recover_active_workflows()
        
        assert recovered == 0
    
    @pytest.mark.asyncio
    async def test_recover_active_workflow_job_running(self, mock_config, state_store, mock_rundeck_client):
        """Should recover workflow in JOB_RUNNING state."""
        # Create active workflow
        workflow = RemediationWorkflow(
            id="wf-123",
            alert_name="TestAlert",
            alert_labels={"cluster": "prod"},
            state=RemediationState.JOB_RUNNING,
            rundeck_execution_id="exec-456",
            created_at=datetime.utcnow() - timedelta(minutes=5)  # Recent
        )
        await state_store.save(workflow)
        
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        recovered = await manager.recover_active_workflows()
        
        assert recovered == 1
        assert "wf-123" in manager._running_tasks
        assert "wf-123" in manager._resolution_events
    
    @pytest.mark.asyncio
    async def test_recover_workflow_waiting_resolution(self, mock_config, state_store, mock_rundeck_client):
        """Should recover workflow in WAITING_RESOLUTION state."""
        workflow = RemediationWorkflow(
            id="wf-456",
            alert_name="TestAlert",
            alert_labels={"cluster": "prod"},
            state=RemediationState.WAITING_RESOLUTION,
            rundeck_execution_id="exec-789",
            created_at=datetime.utcnow() - timedelta(minutes=2)
        )
        await state_store.save(workflow)
        
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        recovered = await manager.recover_active_workflows()
        
        assert recovered == 1
    
    @pytest.mark.asyncio
    async def test_skip_stale_workflows(self, mock_config, state_store, mock_rundeck_client):
        """Should skip workflows older than 24 hours."""
        workflow = RemediationWorkflow(
            id="wf-old",
            alert_name="TestAlert",
            alert_labels={"cluster": "prod"},
            state=RemediationState.JOB_RUNNING,
            rundeck_execution_id="exec-old",
            created_at=datetime.utcnow() - timedelta(hours=25)  # Stale
        )
        await state_store.save(workflow)
        
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        recovered = await manager.recover_active_workflows()
        
        assert recovered == 0
        # Workflow should be marked as escalated
        updated = await state_store.get("wf-old")
        assert updated.state == RemediationState.ESCALATED
    
    @pytest.mark.asyncio
    async def test_skip_completed_workflows(self, mock_config, state_store, mock_rundeck_client):
        """Should not recover workflows in terminal states."""
        workflow = RemediationWorkflow(
            id="wf-done",
            alert_name="TestAlert",
            alert_labels={"cluster": "prod"},
            state=RemediationState.COMPLETED,
            rundeck_execution_id="exec-done",
            created_at=datetime.utcnow() - timedelta(minutes=5)
        )
        await state_store.save(workflow)
        
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        recovered = await manager.recover_active_workflows()
        
        # Completed workflows shouldn't be returned by list_active()
        assert recovered == 0
    
    @pytest.mark.asyncio
    async def test_recover_multiple_workflows(self, mock_config, state_store, mock_rundeck_client):
        """Should recover multiple active workflows."""
        workflow1 = RemediationWorkflow(
            id="wf-1",
            alert_name="Alert1",
            alert_labels={"cluster": "prod"},
            state=RemediationState.JOB_RUNNING,
            rundeck_execution_id="exec-1",
            created_at=datetime.utcnow() - timedelta(minutes=5)
        )
        workflow2 = RemediationWorkflow(
            id="wf-2",
            alert_name="Alert2",
            alert_labels={"cluster": "staging"},
            state=RemediationState.WAITING_RESOLUTION,
            rundeck_execution_id="exec-2",
            created_at=datetime.utcnow() - timedelta(minutes=3)
        )
        await state_store.save(workflow1)
        await state_store.save(workflow2)
        
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        recovered = await manager.recover_active_workflows()
        
        assert recovered == 2
    
    @pytest.mark.asyncio
    async def test_cooldown_restored_on_recovery(self, mock_config, state_store, mock_rundeck_client):
        """Should restore cooldown tracking when recovering workflow."""
        workflow = RemediationWorkflow(
            id="wf-cooldown",
            alert_name="TestAlert",
            alert_labels={"cluster": "prod"},
            state=RemediationState.JOB_RUNNING,
            rundeck_execution_id="exec-123",
            created_at=datetime.utcnow() - timedelta(minutes=5)
        )
        await state_store.save(workflow)
        
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        await manager.recover_active_workflows()
        
        # Check cooldown was restored
        fingerprint = manager._get_alert_fingerprint("TestAlert", {"cluster": "prod"})
        assert fingerprint in manager._alert_cooldowns
        assert manager._alert_cooldowns[fingerprint].workflow_id == "wf-cooldown"


class TestResumeWorkflow:
    """Tests for _resume_workflow method."""
    
    @pytest.fixture
    def mock_config(self):
        """Create mock config."""
        config = MagicMock(spec=Config)
        config.remediation = RemediationConfig(
            poll_interval_seconds=1,
            resolution_wait_minutes=1,  # Short for tests
            max_job_wait_minutes=1
        )
        config.alertmanager = None
        config.jira = None
        config.slack = None
        config.get_alert_config.return_value = MagicMock(
            remediation=AlertRemediationConfig(resolution_wait_minutes=None)
        )
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
    async def test_resume_job_running_to_success(self, mock_config, state_store, mock_rundeck_client):
        """Should resume JOB_RUNNING workflow and handle success."""
        workflow = RemediationWorkflow(
            id="wf-resume",
            alert_name="TestAlert",
            alert_labels={"cluster": "prod"},
            state=RemediationState.JOB_RUNNING,
            rundeck_execution_id="exec-123"
        )
        await state_store.save(workflow)
        
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        # Mock job completion and alert resolution
        manager._wait_for_job_completion = AsyncMock(return_value=True)
        manager._wait_for_alert_resolution = AsyncMock(return_value=(True, "webhook"))
        manager._handle_success = AsyncMock()
        
        await manager._resume_workflow(workflow)
        
        manager._wait_for_job_completion.assert_called_once()
        manager._handle_success.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_resume_job_running_to_failure(self, mock_config, state_store, mock_rundeck_client):
        """Should resume JOB_RUNNING workflow and handle job failure."""
        workflow = RemediationWorkflow(
            id="wf-resume-fail",
            alert_name="TestAlert",
            alert_labels={"cluster": "prod"},
            state=RemediationState.JOB_RUNNING,
            rundeck_execution_id="exec-123"
        )
        await state_store.save(workflow)
        
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        manager._wait_for_job_completion = AsyncMock(return_value=False)
        manager._handle_job_failure = AsyncMock()
        
        await manager._resume_workflow(workflow)
        
        manager._handle_job_failure.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_resume_waiting_resolution(self, mock_config, state_store, mock_rundeck_client):
        """Should resume WAITING_RESOLUTION workflow."""
        workflow = RemediationWorkflow(
            id="wf-waiting",
            alert_name="TestAlert",
            alert_labels={"cluster": "prod"},
            state=RemediationState.WAITING_RESOLUTION,
            rundeck_execution_id="exec-123"
        )
        await state_store.save(workflow)
        
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        manager._wait_for_alert_resolution = AsyncMock(return_value=(True, "webhook"))
        manager._handle_success = AsyncMock()
        
        await manager._resume_workflow(workflow)
        
        manager._wait_for_alert_resolution.assert_called_once()
        manager._handle_success.assert_called_once()

    @pytest.mark.asyncio
    async def test_resume_workflow_is_thin_delegator(
        self, mock_config, state_store, mock_rundeck_client
    ):
        """
        ``_resume_workflow`` must just delegate to ``_monitor_workflow``.
        ``_monitor_workflow`` owns the lifecycle (resolution event setup,
        outcome recording, ``_running_tasks`` / ``_resolution_events`` /
        terminal-sentinel cleanup); duplicating any of that here used to
        cause a second ``escalated`` sample on certain failure paths.
        """
        workflow = RemediationWorkflow(
            id="wf-delegator",
            alert_name="TestAlert",
            alert_labels={},
            state=RemediationState.JOB_TRIGGERED,
        )
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        manager._monitor_workflow = AsyncMock()

        await manager._resume_workflow(workflow)

        manager._monitor_workflow.assert_awaited_once_with(workflow.id)

    @pytest.mark.asyncio
    async def test_monitor_workflow_outer_handler_swallows_fallback_save_failure(
        self, mock_config, state_store, mock_rundeck_client
    ):
        """
        The hardened outer ``except Exception`` in ``_monitor_workflow`` must
        not propagate, even if the fallback ``state_store.save`` fails. If it
        propagated, ``asyncio`` would log
        ``Task exception was never retrieved`` and any caller awaiting the
        coroutine (e.g. the previous ``_resume_workflow`` wrapper) would
        attempt redundant cleanup and risk double-recording metrics.
        """
        workflow = RemediationWorkflow(
            id="wf-fallback-fail",
            alert_name="TestAlert",
            alert_labels={},
            state=RemediationState.JOB_TRIGGERED,
        )
        await state_store.save(workflow)

        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)

        # Force the inner monitor loop to blow up on its first iteration.
        async def _raise(*_args, **_kwargs):
            raise RuntimeError("inner monitor exploded")

        manager._wait_for_job_completion = _raise

        # And make the fallback save inside the outer except handler ALSO
        # fail — this is the path the previous redundant ``_resume_workflow``
        # handler was trying (and failing) to protect against.
        original_save = state_store.save
        save_calls: list = []

        async def _flaky_save(wf):
            save_calls.append(wf.state)
            if wf.state == RemediationState.ESCALATED:
                raise RuntimeError("dynamo down during fallback save")
            await original_save(wf)

        state_store.save = _flaky_save  # type: ignore[assignment]

        # Must not raise.
        await manager._monitor_workflow(workflow.id)

        # Cleanup happened despite the fallback save failing.
        assert workflow.id not in manager._running_tasks
        assert workflow.id not in manager._resolution_events
        assert workflow.id not in manager._terminal_recorded
        # Fallback ESCALATED save was attempted (and raised, but was swallowed).
        assert RemediationState.ESCALATED in save_calls
