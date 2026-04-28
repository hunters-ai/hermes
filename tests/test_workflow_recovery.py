"""Tests for workflow recovery functionality (P1)."""
import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

from hermes.core.remediation_manager import RemediationManager, _WorkflowFallback
from hermes.config import Config, RemediationConfig, AlertConfig, AlertRemediationConfig
from hermes.core.state_store import InMemoryStateStore, RemediationWorkflow, RemediationState
from hermes.utils import metrics as m


def _outcomes_count(alert_type: str, outcome: str, attempts: str) -> float:
    return m.REMEDIATION_OUTCOMES.labels(
        alert_type=alert_type, outcome=outcome, attempts=attempts
    )._value.get()  # type: ignore[attr-defined]


def _duration_sum(alert_type: str, outcome: str) -> float:
    return m.REMEDIATION_DURATION.labels(
        alert_type=alert_type, outcome=outcome
    )._sum.get()  # type: ignore[attr-defined]


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


class TestWorkflowFallbackTerminal:
    """The outer exception handler in ``_monitor_workflow`` falls back to
    in-memory metadata (``_workflow_fallback``) when the state store is
    unavailable. Without that fallback a Dynamo outage at the moment the
    handler runs silently drops the terminal sample for the affected
    workflow even though ``REMEDIATION_WORKFLOWS`` was already incremented
    at start, so ``REMEDIATION_OUTCOMES`` permanently drifts low by one
    and the started-vs-terminal invariant breaks. These tests pin that
    contract.
    """

    @pytest.fixture
    def mock_config(self):
        config = MagicMock(spec=Config)
        config.remediation = RemediationConfig(
            poll_interval_seconds=1,
            resolution_wait_minutes=1,
            max_job_wait_minutes=1,
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
        return InMemoryStateStore()

    @pytest.fixture
    def mock_rundeck_client(self):
        return MagicMock()

    @pytest.mark.asyncio
    async def test_fallback_records_terminal_when_state_store_get_raises(
        self, mock_config, state_store, mock_rundeck_client
    ):
        """``state_store.get`` raises in the outer handler — terminal still emitted.

        This is the primary regression. Pre-fix, ``workflow`` stayed
        ``None``, the ``if workflow:`` block was skipped, no terminal
        metric was recorded, and the slot in ``_running_tasks`` was
        released anyway — so ``REMEDIATION_OUTCOMES`` was permanently
        short by one sample for this workflow.
        """
        workflow = RemediationWorkflow(
            id="wf-store-down",
            alert_name="WorkflowFallbackAlert",
            alert_labels={},
            state=RemediationState.JOB_TRIGGERED,
            attempts=2,
        )
        await state_store.save(workflow)

        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)

        # Pre-register the fallback the way ``start_remediation`` /
        # ``recover_active_workflows`` would have done before kicking off
        # the monitor task.
        manager._workflow_fallback[workflow.id] = _WorkflowFallback(
            alert_name=workflow.alert_name,
            created_at=workflow.created_at,
            attempts=workflow.attempts,
        )

        outcome_before = _outcomes_count(
            workflow.alert_name, "escalated", str(workflow.attempts)
        )
        duration_before = _duration_sum(workflow.alert_name, "escalated")

        # Inner monitor blows up on its first iteration.
        async def _raise(*_a, **_kw):
            raise RuntimeError("inner monitor exploded")

        manager._wait_for_job_completion = _raise

        # Outer fallback ``state_store.get`` ALSO fails (Dynamo down).
        async def _get_raises(_id):
            raise RuntimeError("dynamo down during fallback get")

        state_store.get = _get_raises  # type: ignore[assignment]

        await manager._monitor_workflow(workflow.id)

        outcome_after = _outcomes_count(
            workflow.alert_name, "escalated", str(workflow.attempts)
        )
        duration_after = _duration_sum(workflow.alert_name, "escalated")

        assert outcome_after == outcome_before + 1, (
            "Terminal outcome MUST be recorded via fallback metadata when "
            "state_store.get fails. If this fails, the started-vs-terminal "
            "invariant is broken: REMEDIATION_WORKFLOWS got +1 at start but "
            "REMEDIATION_OUTCOMES never got +1 to match."
        )
        assert duration_after >= duration_before, (
            "REMEDIATION_DURATION must also observe the fallback sample, "
            "otherwise the duration histogram is short by one observation "
            "for every state-store-down crash."
        )

        # All cleanup still happens.
        assert workflow.id not in manager._running_tasks
        assert workflow.id not in manager._resolution_events
        assert workflow.id not in manager._terminal_recorded
        assert workflow.id not in manager._workflow_fallback

    @pytest.mark.asyncio
    async def test_fallback_records_terminal_when_state_store_get_returns_none(
        self, mock_config, state_store, mock_rundeck_client
    ):
        """``state_store.get`` returns ``None`` inside the outer handler.

        Same dead branch as the raise-during-get path: ``workflow``
        stays ``None``, the regular ``if workflow:`` recording is
        skipped, and pre-fix no terminal metric was recorded. Note this
        test specifically exercises the *outer* handler — the inner
        loop's own ``state_store.get -> None -> return`` early-exit is a
        separate (milder) path that doesn't go through the fallback and
        is not in scope here.
        """
        workflow = RemediationWorkflow(
            id="wf-outer-none",
            alert_name="WorkflowFallbackAlert",
            alert_labels={},
            state=RemediationState.JOB_TRIGGERED,
            attempts=1,
        )
        await state_store.save(workflow)

        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        manager._workflow_fallback[workflow.id] = _WorkflowFallback(
            alert_name=workflow.alert_name,
            created_at=workflow.created_at,
            attempts=workflow.attempts,
        )

        outcome_before = _outcomes_count(workflow.alert_name, "escalated", "1")

        # Inner monitor blows up after the initial successful get.
        async def _raise(*_a, **_kw):
            raise RuntimeError("inner monitor exploded")

        manager._wait_for_job_completion = _raise

        # Inner loop's first ``state_store.get`` returns the workflow so
        # the loop proceeds into ``_wait_for_job_completion`` (which
        # raises). The OUTER handler's subsequent ``state_store.get``
        # returns ``None`` (row deleted / eventually-consistent miss /
        # clock skew), exercising the dead branch the fallback covers.
        original_get = state_store.get
        get_calls = {"n": 0}

        async def _get_first_then_none(wid):
            get_calls["n"] += 1
            if get_calls["n"] == 1:
                return await original_get(wid)
            return None

        state_store.get = _get_first_then_none  # type: ignore[assignment]

        await manager._monitor_workflow(workflow.id)

        outcome_after = _outcomes_count(workflow.alert_name, "escalated", "1")
        assert get_calls["n"] >= 2, (
            "Test must drive the outer handler's state_store.get path; if "
            "this fails the inner loop short-circuited and we're not "
            "actually exercising the fallback."
        )
        assert outcome_after == outcome_before + 1
        assert workflow.id not in manager._workflow_fallback

    @pytest.mark.asyncio
    async def test_fallback_is_idempotent_with_terminal_recorded_sentinel(
        self, mock_config, state_store, mock_rundeck_client
    ):
        """If an inner handler already recorded a more specific outcome,
        the fallback path must not record a second ``escalated`` sample.

        Same idempotency contract as the regular ``_record_terminal``,
        enforced by the shared ``_terminal_recorded`` sentinel.
        """
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        wf_id = "wf-idem"
        manager._workflow_fallback[wf_id] = _WorkflowFallback(
            alert_name="IdemAlert",
            created_at=datetime.utcnow() - timedelta(seconds=42),
            attempts=1,
        )
        manager._terminal_recorded.add(wf_id)

        before = _outcomes_count("IdemAlert", "escalated", "1")

        result = manager._record_terminal_from_fallback(wf_id, "escalated")

        after = _outcomes_count("IdemAlert", "escalated", "1")
        assert result is True, (
            "Returning False would mislead the caller into logging "
            "'no fallback metadata' even though the entry exists; the "
            "skip path should still be 'success' from the caller's view."
        )
        assert after == before, "Idempotency violated: second sample emitted."

    @pytest.mark.asyncio
    async def test_fallback_returns_false_when_metadata_missing(
        self, mock_config, state_store, mock_rundeck_client
    ):
        """No fallback entry → no recording, returns False so the caller
        can log a programming-error diagnostic."""
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        result = manager._record_terminal_from_fallback("never-registered", "escalated")
        assert result is False

    @pytest.mark.asyncio
    async def test_fallback_attempts_synced_on_retrigger(
        self, mock_config, state_store, mock_rundeck_client
    ):
        """``_handle_alert_still_firing`` must keep ``_workflow_fallback``
        in sync with the persisted attempt count.

        Without this, a state-store-down crash mid-retry would record
        ``attempts="1"`` for a workflow that was actually on attempt N,
        distorting the retry distribution histogram.
        """
        # Configure max_attempts > 1 so we hit the retrigger branch, and
        # zero cooldown so the test does not actually sleep (the prod
        # path uses ``asyncio.sleep`` for any positive cooldown).
        mock_config.remediation = RemediationConfig(
            poll_interval_seconds=1,
            resolution_wait_minutes=1,
            max_job_wait_minutes=1,
            max_attempts=3,
            job_retrigger_cooldown_minutes=0,
        )
        mock_config.get_alert_config.return_value = MagicMock(
            remediation=AlertRemediationConfig(
                max_attempts=3,
                job_retrigger_cooldown_minutes=0,
            ),
            job_id="job-retry",
        )

        workflow = RemediationWorkflow(
            id="wf-retry",
            alert_name="RetryAlert",
            alert_labels={},
            state=RemediationState.WAITING_RESOLUTION,
            rundeck_execution_id="exec-old",
            rundeck_options={"foo": "bar"},
            attempts=1,
            last_triggered_at=datetime.utcnow() - timedelta(hours=1),
        )
        await state_store.save(workflow)

        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        manager._workflow_fallback[workflow.id] = _WorkflowFallback(
            alert_name=workflow.alert_name,
            created_at=workflow.created_at,
            attempts=1,
        )

        manager.rundeck_client.run_job = AsyncMock(
            return_value={"id": "exec-new", "permalink": "https://r/exec-new"}
        )

        retry_initiated = await manager._handle_alert_still_firing(workflow)

        assert retry_initiated is True
        assert workflow.attempts == 2
        assert manager._workflow_fallback[workflow.id].attempts == 2, (
            "Fallback attempt count drifted from workflow.attempts. A "
            "state-store-down crash now would record the wrong attempts "
            "label for this workflow."
        )

    @pytest.mark.asyncio
    async def test_start_remediation_registers_fallback(
        self, mock_config, state_store, mock_rundeck_client
    ):
        """Pin the contract that every started workflow has a fallback
        entry by the time the monitor task could plausibly raise."""
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        # Don't actually run the monitor task; we only care that the
        # fallback was registered before scheduling.
        manager._monitor_workflow = AsyncMock()

        workflow_id = await manager.start_remediation(
            alert_name="StartAlert",
            alert_labels={"cluster": "prod"},
            rundeck_execution_id="exec-0",
        )

        try:
            assert workflow_id in manager._workflow_fallback
            fallback = manager._workflow_fallback[workflow_id]
            assert fallback.alert_name == "StartAlert"
            assert fallback.attempts == 1
        finally:
            # Avoid leaking the running task across tests.
            task = manager._running_tasks.pop(workflow_id, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
