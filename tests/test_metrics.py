"""Tests for Prometheus metrics."""
import asyncio
import os
from unittest.mock import MagicMock

import httpx
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

    def test_record_outcome_requires_attempts(self):
        """
        ``attempts`` is required and must be an ``int``. A defensive ``None``
        / ``"unknown"`` fallback would create a confusing hole in attempts
        distribution charts (``attempts="0"`` doesn't mean "zero attempts",
        it would silently mean "we forgot to pass the value"). The single
        in-tree caller (:meth:`_record_terminal`) already substitutes ``1``
        when ``workflow.attempts`` is missing, so passing ``None`` here is a
        programming error and should fail loudly.
        """
        manager = _make_manager()

        with pytest.raises(TypeError):
            manager._record_outcome("UnitTestAlert", "success")  # type: ignore[call-arg]

    def test_record_outcome_buckets_high_attempt_count(self):
        """A misconfigured per-alert ``max_attempts=100`` MUST NOT create
        100 distinct ``attempts`` label series.

        ``AlertRemediationConfig.max_attempts`` is an ``Optional[int]`` with
        no upper bound in the config model — review can miss a
        ``max_attempts: 50`` slipping into ``config.yaml`` for a single
        flapping alert. Without bucketing, every distinct integer became
        a new label series on ``REMEDIATION_OUTCOMES`` (and on
        ``REMEDIATION_RETRIES``), and a Prometheus tsdb has no defense
        against that — once those series exist they persist for the
        retention window. This test pins that the call site goes through
        :func:`bucket_attempts` so the same misconfiguration only ever
        produces the ``"4+"`` bucket.
        """
        manager = _make_manager()
        # ``42`` and ``99`` should both fold into the same ``"4+"`` series.
        before = _counter_value(
            m.REMEDIATION_OUTCOMES,
            alert_type="BucketAlert",
            outcome="success",
            attempts="4+",
        )

        manager._record_outcome("BucketAlert", "success", attempts=42)
        manager._record_outcome("BucketAlert", "success", attempts=99)

        after = _counter_value(
            m.REMEDIATION_OUTCOMES,
            alert_type="BucketAlert",
            outcome="success",
            attempts="4+",
        )
        assert after == before + 2, (
            "Both high-attempt observations should land in the '4+' bucket; "
            "if this fails the call site is bypassing `bucket_attempts` and "
            "we're back to unbounded cardinality on REMEDIATION_OUTCOMES."
        )

        # And confirm no per-integer series were created.
        for n in (42, 99):
            integer_series = _counter_value(
                m.REMEDIATION_OUTCOMES,
                alert_type="BucketAlert",
                outcome="success",
                attempts=str(n),
            )
            assert integer_series == 0.0, (
                f"Unbounded series attempts={n!r} was created. "
                f"`_record_outcome` must always pass through `bucket_attempts`."
            )


class TestAttemptBucket:
    """Pin the boundary semantics of :func:`bucket_attempts` directly.

    The helper is the single source of truth for the ``attempts`` /
    ``attempt_number`` labels — ``REMEDIATION_OUTCOMES`` has it on every
    sample, and ``REMEDIATION_RETRIES`` calls it from the retrigger path.
    Both are unbounded without it (per-alert ``max_attempts`` overrides
    have no cap in the config model). These tests pin the bucket
    boundaries because the SLO recording rules and dashboards filter on
    the resulting strings.
    """

    @pytest.mark.parametrize(
        "attempts,expected",
        [
            # The dashboards rely on attempts="1" being the literal first
            # attempt — preserved exactly.
            (1, "1"),
            (2, "2"),
            (3, "3"),
            # 4 is the first value to roll up; everything above it shares
            # the same series so misconfigurations stay bounded.
            (4, "4+"),
            (5, "4+"),
            (10, "4+"),
            (100, "4+"),
        ],
    )
    def test_bucket_returns_expected_value(self, attempts, expected):
        assert m.bucket_attempts(attempts) == expected

    @pytest.mark.parametrize("attempts", [0, -1, -100])
    def test_bucket_clamps_non_positive_to_one(self, attempts):
        """``0`` / negative values should never reach the metric (every
        terminal workflow has run at least one attempt), but if they do,
        clamp to ``"1"`` rather than producing a separate ``"0"`` /
        ``"-1"`` series. Defensive against future callers that forget the
        ``or 1`` guard."""
        assert m.bucket_attempts(attempts) == "1"

    def test_bucket_output_is_a_subset_of_attempt_bucket_constants(self):
        """No matter what integer goes in, the output is one of the four
        constants. This is what makes the label cardinality bounded by
        construction."""
        allowed = {
            m.AttemptBucket.ONE,
            m.AttemptBucket.TWO,
            m.AttemptBucket.THREE,
            m.AttemptBucket.FOUR_PLUS,
        }
        # Sweep a generous range including pathological values.
        for n in [-5, 0, 1, 2, 3, 4, 5, 10, 100, 10_000]:
            assert m.bucket_attempts(n) in allowed, (
                f"bucket_attempts({n}) returned a value outside the closed "
                f"AttemptBucket set; this would re-introduce unbounded "
                f"cardinality on REMEDIATION_OUTCOMES / REMEDIATION_RETRIES."
            )


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

    def test_attempt_bucket_constants(self):
        """The closed set for the ``attempts`` / ``attempt_number`` labels.

        These exact strings are part of the metric contract: the SLO
        dashboards filter on ``attempts="1"`` for first-attempt success
        rate, and recording rules / alerting rules have been authored
        against this set. Renaming any of these breaks dashboards.
        """
        assert m.AttemptBucket.ONE == "1"
        assert m.AttemptBucket.TWO == "2"
        assert m.AttemptBucket.THREE == "3"
        assert m.AttemptBucket.FOUR_PLUS == "4+"

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
            # ``attempts`` / ``attempt_number`` MUST go through
            # ``bucket_attempts(...)`` (or use an ``AttemptBucket.*`` constant
            # / a literal already in the closed set) so that a per-alert
            # ``max_attempts`` override can never blow up cardinality. A
            # raw ``str(n)`` at a call site would silently re-introduce the
            # unbounded behavior, so the regex below explicitly captures
            # that case as the "literal" group and then validates against
            # the closed set — ``str(...)`` is not a member, so it fails
            # loudly.
            (
                repo_root / "src/hermes/core/remediation_manager.py",
                "REMEDIATION_OUTCOMES",
                r"attempts=(?:[\"'](?P<lit>[^\"']+)[\"']|bucket_attempts\([^)]+\)|AttemptBucket\.[A-Z_]+)",
                _allowed_values(m.AttemptBucket),
            ),
            (
                repo_root / "src/hermes/core/remediation_manager.py",
                "REMEDIATION_RETRIES",
                r"attempt_number=(?:[\"'](?P<lit>[^\"']+)[\"']|bucket_attempts\([^)]+\)|AttemptBucket\.[A-Z_]+)",
                _allowed_values(m.AttemptBucket),
            ),
        ]

        for path, scope, pattern, allowed in cases:
            text = path.read_text()
            blocks = _scope_blocks(text, scope) if scope else [text]
            matched_any = False
            for block in blocks:
                for match in re.finditer(pattern, block):
                    matched_any = True
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
            # Bucketing-related call sites: if the scope exists and contains
            # the label kw at all, *something* should have matched. A
            # mismatch (e.g. someone replacing ``bucket_attempts(...)`` with
            # ``str(n)``) silently drops out of the union pattern instead of
            # failing here, so spot-check by searching for the bare kw.
            if scope in ("REMEDIATION_OUTCOMES", "REMEDIATION_RETRIES"):
                kw = "attempts=" if scope == "REMEDIATION_OUTCOMES" else "attempt_number="
                kw_present = any(kw in b for b in blocks)
                if kw_present:
                    assert matched_any, (
                        f"{path.name}: a `.labels(..., {kw}...)` call on "
                        f"{scope} did not match the allowed pattern. The "
                        f"label MUST be one of: literal in {sorted(allowed)!r}, "
                        f"`bucket_attempts(...)`, or `AttemptBucket.*`. "
                        f"Anything else (notably `str(n)`) reintroduces the "
                        f"unbounded-cardinality bug `bucket_attempts` exists "
                        f"to prevent."
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


# --- External client error attribution --------------------------------------
#
# Regression coverage for the silent-success bug: clients that swallowed
# ``httpx.HTTPError`` internally and returned ``False`` caused
# ``track_call`` to fall through to its ``else`` branch and record
# ``status="success"`` for every failed Slack / Rundeck-login call. These
# tests pin the contract that those clients now propagate exceptions, so
# the ``track_call`` error path runs and ``EXTERNAL_CALL_ERRORS`` is
# attributed correctly. The pattern follows ``JiraClient`` /
# ``AlertmanagerClient``, which already re-raise.


class TestSlackClientErrorAttribution:
    """``SlackClient`` must surface failures to :func:`track_call`.

    Two failure modes are covered:

    - HTTP-level failure inside ``_send_via_webhook`` / ``_send_via_bot``
      (the original bug the user reported).
    - Slack API-level failure (``ok=false`` on a 200 response) inside
      ``_send_via_bot``. Same class of bug — the call wire-succeeded but
      Slack refused to deliver — and equally invisible in metrics until we
      raised :class:`hermes.clients.slack.SlackApiError`.
    """

    @pytest.mark.asyncio
    async def test_send_via_webhook_records_error_on_http_failure(self, monkeypatch):
        from hermes.clients import slack as slack_module
        from hermes.clients.slack import SlackClient

        err_before = _counter_value(
            m.EXTERNAL_CALL_ERRORS,
            service="slack",
            operation="webhook_post",
            error_type="HTTPError",
        )
        success_sum_before = m.EXTERNAL_CALL_DURATION.labels(
            service="slack", operation="webhook_post", status="success"
        )._sum.get()  # type: ignore[attr-defined]

        class _FailingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, *args, **kwargs):
                raise httpx.HTTPError("connection refused")

        monkeypatch.setattr(slack_module.httpx, "AsyncClient", lambda *a, **k: _FailingClient())

        client = SlackClient(webhook_url="https://hooks.slack.example/AAA/BBB/CCC")

        with pytest.raises(httpx.HTTPError):
            await client._send_via_webhook("hello")

        err_after = _counter_value(
            m.EXTERNAL_CALL_ERRORS,
            service="slack",
            operation="webhook_post",
            error_type="HTTPError",
        )
        success_sum_after = m.EXTERNAL_CALL_DURATION.labels(
            service="slack", operation="webhook_post", status="success"
        )._sum.get()  # type: ignore[attr-defined]

        assert err_after == err_before + 1
        assert success_sum_after == success_sum_before, (
            "Failed Slack webhook posts must NOT show up as success latency. "
            "If this fires, _send_via_webhook is swallowing the exception again "
            "and track_call is falling through to its else branch."
        )

    @pytest.mark.asyncio
    async def test_send_via_bot_records_error_on_slack_api_failure(self, monkeypatch):
        """``ok=false`` on a 200 must propagate as :class:`SlackApiError`."""
        from hermes.clients import slack as slack_module
        from hermes.clients.slack import SlackApiError, SlackClient

        err_before = _counter_value(
            m.EXTERNAL_CALL_ERRORS,
            service="slack",
            operation="chat_post_message",
            error_type="SlackApiError",
        )
        success_sum_before = m.EXTERNAL_CALL_DURATION.labels(
            service="slack", operation="chat_post_message", status="success"
        )._sum.get()  # type: ignore[attr-defined]

        class _OkFalseResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": False, "error": "channel_not_found"}

        class _OkFalseClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, *args, **kwargs):
                return _OkFalseResponse()

        monkeypatch.setattr(slack_module.httpx, "AsyncClient", lambda *a, **k: _OkFalseClient())

        client = SlackClient(bot_token="xoxb-test-token")

        with pytest.raises(SlackApiError):
            await client._send_via_bot("hello", channel="#nonexistent")

        err_after = _counter_value(
            m.EXTERNAL_CALL_ERRORS,
            service="slack",
            operation="chat_post_message",
            error_type="SlackApiError",
        )
        success_sum_after = m.EXTERNAL_CALL_DURATION.labels(
            service="slack", operation="chat_post_message", status="success"
        )._sum.get()  # type: ignore[attr-defined]

        assert err_after == err_before + 1
        assert success_sum_after == success_sum_before


class TestRundeckLoginErrorAttribution:
    """``RundeckClient._login`` must surface failures to :func:`track_call`."""

    @pytest.mark.asyncio
    async def test_login_records_error_on_http_failure(self, monkeypatch):
        from hermes.clients import rundeck as rundeck_module
        from hermes.clients.rundeck import RundeckClient

        err_before = _counter_value(
            m.EXTERNAL_CALL_ERRORS,
            service="rundeck",
            operation="login",
            error_type="HTTPError",
        )
        success_sum_before = m.EXTERNAL_CALL_DURATION.labels(
            service="rundeck", operation="login", status="success"
        )._sum.get()  # type: ignore[attr-defined]

        class _FailingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, *args, **kwargs):
                raise httpx.HTTPError("connection refused")

        monkeypatch.setattr(
            rundeck_module.httpx, "AsyncClient", lambda *a, **k: _FailingClient()
        )

        client = RundeckClient(
            base_url="https://rundeck.example/", username="u", password="p"
        )

        with pytest.raises(httpx.HTTPError):
            await client._login()

        err_after = _counter_value(
            m.EXTERNAL_CALL_ERRORS,
            service="rundeck",
            operation="login",
            error_type="HTTPError",
        )
        success_sum_after = m.EXTERNAL_CALL_DURATION.labels(
            service="rundeck", operation="login", status="success"
        )._sum.get()  # type: ignore[attr-defined]

        assert err_after == err_before + 1
        assert success_sum_after == success_sum_before, (
            "Failed Rundeck logins must NOT show up as success latency. "
            "If this fires, _login is swallowing the exception again and "
            "track_call is falling through to its else branch."
        )

    @pytest.mark.asyncio
    async def test_login_records_error_when_session_cookie_is_missing(self, monkeypatch):
        """200 response with no JSESSIONID -> :class:`RundeckLoginError`."""
        from hermes.clients import rundeck as rundeck_module
        from hermes.clients.rundeck import RundeckClient, RundeckLoginError

        err_before = _counter_value(
            m.EXTERNAL_CALL_ERRORS,
            service="rundeck",
            operation="login",
            error_type="RundeckLoginError",
        )

        class _EmptyCookieJar:
            def get(self, _name, default=None):
                return default

        class _Response:
            cookies = _EmptyCookieJar()

        class _NoCookieClient:
            cookies = _EmptyCookieJar()

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, *args, **kwargs):
                return _Response()

        monkeypatch.setattr(
            rundeck_module.httpx, "AsyncClient", lambda *a, **k: _NoCookieClient()
        )

        client = RundeckClient(
            base_url="https://rundeck.example/", username="u", password="wrong"
        )

        with pytest.raises(RundeckLoginError):
            await client._login()

        err_after = _counter_value(
            m.EXTERNAL_CALL_ERRORS,
            service="rundeck",
            operation="login",
            error_type="RundeckLoginError",
        )
        assert err_after == err_before + 1
