"""Tests for retrigger-cooldown behaviour when the alert clears while we wait."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes.config import AlertRemediationConfig, Config, RemediationConfig
from hermes.core.remediation_manager import RemediationManager, ResolutionSource
from hermes.core.state_store import (
    InMemoryStateStore,
    RemediationState,
    RemediationWorkflow,
)


def _config(cooldown_minutes, max_attempts=2):
    config = MagicMock(spec=Config)
    config.remediation = RemediationConfig(
        poll_interval_seconds=1,
        max_job_wait_minutes=5,
        alert_check_interval_seconds=1,
        max_attempts=max_attempts,
    )
    config.alertmanager = None
    config.jira = None
    config.slack = None
    config.get_alert_config.return_value = MagicMock(
        job_id="job-abc",
        remediation=AlertRemediationConfig(
            job_retrigger_cooldown_minutes=cooldown_minutes,
            max_attempts=max_attempts,
        ),
    )
    return config


def _workflow(last_triggered_at):
    return RemediationWorkflow(
        id="wf-1",
        alert_name="Stalled Data Ingestion",
        alert_labels={"alertname": "Stalled Data Ingestion", "dataflow_id": "df-1"},
        state=RemediationState.WAITING_RESOLUTION,
        rundeck_execution_id="1059502",
        alertmanager_url="http://alertmanager.example.com",
        attempts=1,
        last_triggered_at=last_triggered_at,
        rundeck_options={"dataflow_id": "df-1"},
    )


def _manager(config):
    rundeck_client = MagicMock()
    rundeck_client.run_job = AsyncMock(return_value={"id": "1060433"})
    manager = RemediationManager(config, InMemoryStateStore(), rundeck_client)
    return manager, rundeck_client


class TestRetriggerCooldown:
    """RND-192491: a day-long cooldown must not outlive the alert it is waiting on."""

    @pytest.mark.asyncio
    async def test_alert_resolved_during_cooldown_does_not_retrigger(self):
        """The whole bug: the alert cleared while we waited, so there is nothing to remediate."""
        config = _config(cooldown_minutes=1440)
        manager, rundeck_client = _manager(config)
        workflow = _workflow(last_triggered_at=datetime.utcnow())
        await manager.state_store.save(workflow)

        # Alertmanager reports the alert gone on the first poll of the cooldown wait.
        with patch.object(manager, "_check_alert_resolved", AsyncMock(return_value=True)), \
                patch("hermes.core.remediation_manager.asyncio.sleep", AsyncMock()):
            retried = await manager._handle_alert_still_firing(workflow)

        assert retried is False
        rundeck_client.run_job.assert_not_called()
        assert workflow.attempts == 1
        assert workflow.state == RemediationState.COMPLETED

    @pytest.mark.asyncio
    async def test_alert_still_firing_after_cooldown_retriggers(self):
        """Cooldown elapsed with the alert still up: retrigger, as before."""
        config = _config(cooldown_minutes=1440)
        manager, rundeck_client = _manager(config)
        workflow = _workflow(last_triggered_at=datetime.utcnow())
        await manager.state_store.save(workflow)

        # asyncio.sleep is stubbed so that reintroducing a blind cooldown sleep fails
        # this test in milliseconds instead of hanging CI for a day.
        wait = AsyncMock(return_value=(False, ResolutionSource.TIMEOUT))
        with patch.object(manager, "_wait_for_alert_resolution", wait), \
                patch("hermes.core.remediation_manager.asyncio.sleep", AsyncMock()):
            retried = await manager._handle_alert_still_firing(workflow)

        assert retried is True
        wait.assert_awaited_once()
        rundeck_client.run_job.assert_awaited_once()
        assert workflow.attempts == 2
        assert workflow.state == RemediationState.JOB_TRIGGERED
        # The cooldown budget is what we wait on, minus what already elapsed.
        assert wait.await_args.args[2] == pytest.approx(1440, abs=1)

    @pytest.mark.asyncio
    async def test_cooldown_already_elapsed_retriggers_without_waiting(self):
        """No cooldown left means no extra wait — the caller just checked resolution."""
        config = _config(cooldown_minutes=5)
        manager, rundeck_client = _manager(config)
        workflow = _workflow(last_triggered_at=datetime.utcnow() - timedelta(hours=2))
        await manager.state_store.save(workflow)

        wait = AsyncMock()
        with patch.object(manager, "_wait_for_alert_resolution", wait):
            retried = await manager._handle_alert_still_firing(workflow)

        assert retried is True
        wait.assert_not_awaited()
        rundeck_client.run_job.assert_awaited_once()
        assert workflow.attempts == 2
