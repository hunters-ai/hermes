"""Tests for Prometheus metrics."""
import asyncio
import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

# Set config path before importing app
os.environ.setdefault("CONFIG_PATH", "config/config.yaml")

from hermes.api.app import _classify_dedup_reason, app
from hermes.config import Config, RemediationConfig
from hermes.core.remediation_manager import RemediationManager
from hermes.core.state_store import (
    InMemoryStateStore,
    RemediationState,
    RemediationWorkflow,
)
from hermes.utils import metrics as m


# --- Endpoint smoke ---------------------------------------------------------

EXPECTED_METRIC_FAMILIES = [
    # HTTP
    "hermes_incoming_requests_total",
    "hermes_processing_duration_seconds",
    # Intake / dedup
    "hermes_alerts_received_total",
    "hermes_processing_errors_total",
    "hermes_alerts_deduplicated_total",
    "hermes_concurrency_limit_hit_total",
    "hermes_rate_limited_requests_total",
    # Lifecycle
    "hermes_remediation_workflows_total",
    "hermes_remediation_outcomes_total",
    "hermes_remediation_retries_total",
    "hermes_resolution_source_total",
    "hermes_workflow_recovery_total",
    "hermes_active_workflows",
    # Durations
    "hermes_remediation_duration_seconds",
    "hermes_job_execution_duration_seconds",
    "hermes_alert_resolution_wait_seconds",
    # External
    "hermes_external_call_duration_seconds",
    "hermes_external_call_errors_total",
    "hermes_rundeck_job_triggers_total",
    "hermes_jira_operations_total",
    "hermes_jira_ticket_fetch_total",
    # Reliability
    "hermes_circuit_breaker_trips_total",
    "hermes_circuit_breaker_state",
    # Notifications
    "hermes_escalations_sent_total",
    "hermes_slack_notifications_total",
    # Build info
    "hermes_build_info",
]


DEAD_METRICS = [
    "hermes_webhook_requests_total",
    "hermes_webhook_errors_total",
]


@pytest.fixture
def client():
    return TestClient(app)


class TestMetricsEndpoint:
    def test_metrics_endpoint_exists(self, client):
        response = client.get("/metrics")
        assert response.status_code == 200

    @pytest.mark.parametrize("metric", EXPECTED_METRIC_FAMILIES)
    def test_metric_family_exposed(self, client, metric):
        response = client.get("/metrics")
        assert metric in response.text, f"missing metric {metric}"

    @pytest.mark.parametrize("metric", DEAD_METRICS)
    def test_dead_metrics_are_gone(self, client, metric):
        response = client.get("/metrics")
        assert metric not in response.text, f"unexpected dead metric {metric}"


# --- Outcome bookkeeping ----------------------------------------------------

def _counter_value(counter, **labels) -> float:
    sample = counter.labels(**labels)
    return sample._value.get()  # type: ignore[attr-defined]


def _make_manager() -> RemediationManager:
    config = MagicMock(spec=Config)
    config.remediation = RemediationConfig()
    config.alertmanager = None
    config.jira = None
    config.slack = None
    config.get_alert_config.return_value = None
    return RemediationManager(config, InMemoryStateStore(), rundeck_client=None)


class TestOutcomeRecording:
    def test_record_outcome_increments_counter(self):
        manager = _make_manager()
        before = _counter_value(
            m.REMEDIATION_OUTCOMES,
            alert_type="UnitTestAlert",
            outcome="success",
            attempts="1",
        )

        manager._record_outcome("UnitTestAlert", "success", attempts=1)

        after = _counter_value(
            m.REMEDIATION_OUTCOMES,
            alert_type="UnitTestAlert",
            outcome="success",
            attempts="1",
        )
        assert after == before + 1

    def test_record_outcome_swallows_metric_failure(self, monkeypatch):
        """A broken metric backend must never break the workflow."""
        manager = _make_manager()

        class _BoomCounter:
            def labels(self, **_):
                raise RuntimeError("metrics backend down")

        monkeypatch.setattr(m, "REMEDIATION_OUTCOMES", _BoomCounter())

        # No exception should escape.
        manager._record_outcome("UnitTestAlert", "success", attempts=1)


class TestTerminalRecording:
    def test_terminal_records_outcome_and_duration(self):
        manager = _make_manager()
        wf = RemediationWorkflow(
            id="wf-1",
            alert_name="DurationAlert",
            alert_labels={},
            state=RemediationState.JOB_SUCCEEDED,
            attempts=2,
        )

        before = _counter_value(
            m.REMEDIATION_OUTCOMES,
            alert_type="DurationAlert",
            outcome="success",
            attempts="2",
        )
        hist_before = m.REMEDIATION_DURATION.labels(
            alert_type="DurationAlert", outcome="success"
        )._sum.get()  # type: ignore[attr-defined]

        manager._record_terminal(wf, "success")

        after = _counter_value(
            m.REMEDIATION_OUTCOMES,
            alert_type="DurationAlert",
            outcome="success",
            attempts="2",
        )
        hist_after = m.REMEDIATION_DURATION.labels(
            alert_type="DurationAlert", outcome="success"
        )._sum.get()  # type: ignore[attr-defined]

        assert after == before + 1
        assert hist_after >= hist_before  # duration sum monotonically grows

    def test_terminal_is_idempotent_per_workflow(self):
        """
        Once a workflow has a terminal outcome recorded, a follow-up call
        (e.g. from the outer ``_monitor_workflow`` exception handler after
        ``_escalate`` raises) must not contribute a second sample to
        ``hermes_remediation_outcomes_total`` or
        ``hermes_remediation_duration_seconds``.
        """
        manager = _make_manager()
        wf = RemediationWorkflow(
            id="wf-idem",
            alert_name="IdemAlert",
            alert_labels={},
            state=RemediationState.JOB_TRIGGERED,
            attempts=1,
        )

        primary_before = _counter_value(
            m.REMEDIATION_OUTCOMES,
            alert_type="IdemAlert",
            outcome="retrigger_failed",
            attempts="1",
        )
        escalated_before = _counter_value(
            m.REMEDIATION_OUTCOMES,
            alert_type="IdemAlert",
            outcome="escalated",
            attempts="1",
        )
        # The escalated-labelled histogram series must stay flat — a second
        # _record_terminal call would add an observation to it.
        escalated_hist_sum_before = m.REMEDIATION_DURATION.labels(
            alert_type="IdemAlert", outcome="escalated"
        )._sum.get()  # type: ignore[attr-defined]

        manager._record_terminal(wf, "retrigger_failed")
        # Simulates the outer handler firing after _escalate raised.
        manager._record_terminal(wf, "escalated")

        primary_after = _counter_value(
            m.REMEDIATION_OUTCOMES,
            alert_type="IdemAlert",
            outcome="retrigger_failed",
            attempts="1",
        )
        escalated_after = _counter_value(
            m.REMEDIATION_OUTCOMES,
            alert_type="IdemAlert",
            outcome="escalated",
            attempts="1",
        )
        escalated_hist_sum_after = m.REMEDIATION_DURATION.labels(
            alert_type="IdemAlert", outcome="escalated"
        )._sum.get()  # type: ignore[attr-defined]

        assert primary_after == primary_before + 1
        assert escalated_after == escalated_before  # second call was a no-op
        assert escalated_hist_sum_after == escalated_hist_sum_before

    def test_terminal_can_record_again_after_cleanup(self):
        """
        The idempotency sentinel is cleared in the monitor task's ``finally``
        block via ``self._terminal_recorded.discard``. After cleanup, the
        same workflow id (e.g. a recovered workflow on restart) must be
        recordable again.
        """
        manager = _make_manager()
        wf = RemediationWorkflow(
            id="wf-cleanup",
            alert_name="CleanupAlert",
            alert_labels={},
            state=RemediationState.JOB_TRIGGERED,
            attempts=1,
        )

        manager._record_terminal(wf, "success")
        manager._terminal_recorded.discard(wf.id)

        before = _counter_value(
            m.REMEDIATION_OUTCOMES,
            alert_type="CleanupAlert",
            outcome="escalated",
            attempts="1",
        )
        manager._record_terminal(wf, "escalated")
        after = _counter_value(
            m.REMEDIATION_OUTCOMES,
            alert_type="CleanupAlert",
            outcome="escalated",
            attempts="1",
        )
        assert after == before + 1


class TestCircuitBreakerGauge:
    def test_gauge_flips_on_open_and_close(self):
        manager = _make_manager()
        # Force threshold to 1 so a single failure trips the breaker.
        manager.circuit_failure_threshold = 1

        manager.record_circuit_failure(m.ExternalService.RUNDECK)
        gauge_open = m.CIRCUIT_BREAKER_STATE.labels(
            service=m.ExternalService.RUNDECK
        )._value.get()  # type: ignore[attr-defined]
        assert gauge_open == 1

        manager.record_circuit_success(m.ExternalService.RUNDECK)
        gauge_closed = m.CIRCUIT_BREAKER_STATE.labels(
            service=m.ExternalService.RUNDECK
        )._value.get()  # type: ignore[attr-defined]
        assert gauge_closed == 0


class TestTrackCall:
    @pytest.mark.asyncio
    async def test_track_call_records_success(self):
        before = m.EXTERNAL_CALL_DURATION.labels(
            service="rundeck", operation="unit_test", status="success"
        )._sum.get()  # type: ignore[attr-defined]

        async with m.track_call("rundeck", "unit_test"):
            pass

        after = m.EXTERNAL_CALL_DURATION.labels(
            service="rundeck", operation="unit_test", status="success"
        )._sum.get()  # type: ignore[attr-defined]
        assert after >= before

    @pytest.mark.asyncio
    async def test_track_call_records_error(self):
        before = _counter_value(
            m.EXTERNAL_CALL_ERRORS,
            service="rundeck",
            operation="unit_test_error",
            error_type="ValueError",
        )

        with pytest.raises(ValueError):
            async with m.track_call("rundeck", "unit_test_error"):
                raise ValueError("boom")

        after = _counter_value(
            m.EXTERNAL_CALL_ERRORS,
            service="rundeck",
            operation="unit_test_error",
            error_type="ValueError",
        )
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_track_call_does_not_record_cancellation(self):
        """
        ``asyncio.CancelledError`` is a control-flow signal raised when
        ``RemediationManager.shutdown`` cancels in-flight workflow tasks.
        It must propagate untouched: no error counter, no latency
        observation. Otherwise every graceful shutdown would spike
        ``hermes_external_call_errors_total{error_type="CancelledError"}``.
        """
        err_before = _counter_value(
            m.EXTERNAL_CALL_ERRORS,
            service="rundeck",
            operation="unit_test_cancel",
            error_type="CancelledError",
        )
        success_sum_before = m.EXTERNAL_CALL_DURATION.labels(
            service="rundeck", operation="unit_test_cancel", status="success"
        )._sum.get()  # type: ignore[attr-defined]
        error_sum_before = m.EXTERNAL_CALL_DURATION.labels(
            service="rundeck", operation="unit_test_cancel", status="error"
        )._sum.get()  # type: ignore[attr-defined]

        with pytest.raises(asyncio.CancelledError):
            async with m.track_call("rundeck", "unit_test_cancel"):
                raise asyncio.CancelledError()

        err_after = _counter_value(
            m.EXTERNAL_CALL_ERRORS,
            service="rundeck",
            operation="unit_test_cancel",
            error_type="CancelledError",
        )
        success_sum_after = m.EXTERNAL_CALL_DURATION.labels(
            service="rundeck", operation="unit_test_cancel", status="success"
        )._sum.get()  # type: ignore[attr-defined]
        error_sum_after = m.EXTERNAL_CALL_DURATION.labels(
            service="rundeck", operation="unit_test_cancel", status="error"
        )._sum.get()  # type: ignore[attr-defined]

        assert err_after == err_before
        assert success_sum_after == success_sum_before
        assert error_sum_after == error_sum_before

    @pytest.mark.asyncio
    async def test_track_call_does_not_record_keyboard_interrupt(self):
        """
        Other ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``,
        ``GeneratorExit``) are interpreter shutdown / generator-cleanup
        signals. They must propagate untouched, same as cancellation.
        """
        err_before = _counter_value(
            m.EXTERNAL_CALL_ERRORS,
            service="rundeck",
            operation="unit_test_kbd",
            error_type="KeyboardInterrupt",
        )

        with pytest.raises(KeyboardInterrupt):
            async with m.track_call("rundeck", "unit_test_kbd"):
                raise KeyboardInterrupt()

        err_after = _counter_value(
            m.EXTERNAL_CALL_ERRORS,
            service="rundeck",
            operation="unit_test_kbd",
            error_type="KeyboardInterrupt",
        )
        assert err_after == err_before


class TestClosedSetLabelEnums:
    """The module docstring promises ``operation`` / ``type`` / ``outcome``
    labels come from a closed set. These tests pin the constant classes that
    represent those sets and assert the in-tree call sites only emit values
    from them — so a typo at a call site is caught here instead of silently
    creating a new Prometheus series.
    """

    def test_jira_operation_constants(self):
        assert m.JiraOperation.ADD_COMMENT == "add_comment"
        assert m.JiraOperation.GET_TICKET == "get_ticket"
        assert m.JiraOperation.SEARCH_TICKETS == "search_tickets"
        assert (
            m.JiraOperation.ADD_REMEDIATION_SUCCESS_COMMENT
            == "add_remediation_success_comment"
        )
        assert (
            m.JiraOperation.ADD_REMEDIATION_FAILURE_COMMENT
            == "add_remediation_failure_comment"
        )
        assert m.JiraOperation.ADD_JOB_FAILURE_COMMENT == "add_job_failure_comment"

    def test_slack_notification_type_constants(self):
        assert m.SlackNotificationType.ESCALATION == "escalation"

    def test_workflow_recovery_outcome_constants(self):
        assert m.WorkflowRecoveryOutcome.RECOVERED == "recovered"
        assert m.WorkflowRecoveryOutcome.STALE_SKIPPED == "stale_skipped"
        assert m.WorkflowRecoveryOutcome.TERMINAL_SKIPPED == "terminal_skipped"
        assert m.WorkflowRecoveryOutcome.FAILED == "failed"

    def test_call_sites_use_closed_set_label_values(self):
        """
        Source-level guard: the in-tree files that emit ``JIRA_OPERATIONS``,
        ``SLACK_NOTIFICATIONS`` and ``WORKFLOW_RECOVERY`` must not pass
        unknown raw string literals to ``.labels(...)``. Catches typos like
        ``"add_remediaton_success_comment"`` and any new free-form value
        added without extending the constant class.
        """
        import re
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]

        cases = [
            (
                repo_root / "src/hermes/core/remediation_manager.py",
                "JIRA_OPERATIONS",
                r"operation=(?:[\"'](?P<lit>[^\"']+)[\"']|JiraOperation\.[A-Z_]+|jira_op)",
                _allowed_values(m.JiraOperation),
            ),
            (
                repo_root / "src/hermes/clients/jira.py",
                None,  # track_call call site, not JIRA_OPERATIONS
                r"track_call\(ExternalService\.JIRA,\s*(?:[\"'](?P<lit>[^\"']+)[\"']|JiraOperation\.[A-Z_]+)",
                _allowed_values(m.JiraOperation),
            ),
            (
                repo_root / "src/hermes/core/remediation_manager.py",
                "SLACK_NOTIFICATIONS",
                r"type=(?:[\"'](?P<lit>[^\"']+)[\"']|SlackNotificationType\.[A-Z_]+)",
                _allowed_values(m.SlackNotificationType),
            ),
            (
                repo_root / "src/hermes/core/remediation_manager.py",
                "WORKFLOW_RECOVERY",
                r"outcome=(?:[\"'](?P<lit>[^\"']+)[\"']|WorkflowRecoveryOutcome\.[A-Z_]+)",
                _allowed_values(m.WorkflowRecoveryOutcome),
            ),
        ]

        for path, scope, pattern, allowed in cases:
            text = path.read_text()
            blocks = _scope_blocks(text, scope) if scope else [text]
            for block in blocks:
                for match in re.finditer(pattern, block):
                    literal = match.group("lit")
                    if literal is None:
                        # Matched a constant-class reference; closed-set safe.
                        continue
                    assert literal in allowed, (
                        f"{path.name}: raw string label value {literal!r} is "
                        f"not a member of the closed set {sorted(allowed)!r}. "
                        f"Add it to the corresponding constant class or fix "
                        f"the typo at the call site."
                    )


def _allowed_values(cls) -> set:
    return {
        getattr(cls, name)
        for name in vars(cls)
        if not name.startswith("_") and isinstance(getattr(cls, name), str)
    }


def _scope_blocks(source: str, metric_name: str) -> list:
    """Return the per-call-site text blocks for a given metric name.

    We grab the line containing ``metric_name.labels(`` plus the next 3 lines
    so we capture the kwargs even when the call is split across lines.
    """
    blocks: list = []
    lines = source.splitlines()
    needle = f"{metric_name}.labels("
    for i, line in enumerate(lines):
        if needle in line:
            blocks.append("\n".join(lines[i : i + 4]))
    return blocks


class TestClassifyDedupReason:
    """Reasons returned by ``RemediationManager.check_deduplication``.

    The cooldown branch returns the previous workflow id as the third tuple
    element, so the classifier must inspect the reason text first — branching
    on ``existing_workflow_id`` would otherwise misclassify cooldowns as
    ``active_workflow`` and conflate concurrency with flapping.
    """

    def test_active_workflow_skip(self):
        reason = "Active workflow wf-123 already exists for this alert"
        assert _classify_dedup_reason(reason, "wf-123") == "active_workflow"

    def test_cooldown_skip_with_workflow_id(self):
        reason = "Alert in cooldown (1.2/5 minutes), last workflow: wf-prev"
        assert _classify_dedup_reason(reason, "wf-prev") == "cooldown"

    def test_concurrency_limit_skip(self):
        reason = "Max concurrent workflows (10) reached"
        assert _classify_dedup_reason(reason, None) == "concurrency_limit"

    def test_unknown_reason_with_workflow_id_falls_back_to_active(self):
        assert _classify_dedup_reason("something custom", "wf-x") == "active_workflow"

    def test_unknown_reason_without_workflow_id(self):
        assert _classify_dedup_reason("something custom", None) == "other"

    def test_empty_inputs(self):
        assert _classify_dedup_reason(None, None) == "other"
