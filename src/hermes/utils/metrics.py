"""
Prometheus metrics definitions for Hermes.

Single source of truth for every metric the service exposes. Imported by the
API layer, the remediation manager, and the external clients to instrument
their behavior.

Label cardinality is kept bounded by:
- ``alert_type``: closed set, derived from ``config.yaml``.
- ``outcome``: closed enum (see ``RemediationOutcome``).
- ``service`` / ``operation``: closed enum (one entry per client method we
  care about).
- ``attempts`` / ``attempt_number``: bucketed via ``AttemptBucket`` /
  ``bucket_attempts`` to a fixed closed set. ``remediation.max_attempts``
  is a per-alert override with no upper bound in the config model, so
  using ``str(attempts)`` directly would let a misconfiguration of e.g.
  ``max_attempts=100`` produce 100 distinct label series per
  (alert_type, outcome) pair. The bucketing keeps cardinality bounded
  regardless of config values that slip through review.
- ``endpoint``: closed set of FastAPI routes.
- ``state``: ``RemediationState`` enum.
- ``target`` / ``result`` / ``source`` / ``method`` / ``type`` / ``status`` /
  ``error_type`` are all small closed sets defined in this module.

Never use raw exception messages or user-controlled strings as label values.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import AsyncIterator, Iterable, Optional

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, REGISTRY

logger = logging.getLogger(__name__)


# Closed set of remediation outcomes. Anything else is a programming error.
class RemediationOutcome:
    SUCCESS = "success"
    JOB_SUCCESS_NO_RESOLUTION_CHECK = "job_success_no_resolution_check"
    JOB_FAILED = "job_failed"
    ALERT_STILL_FIRING = "alert_still_firing"
    ESCALATED = "escalated"
    RETRIGGER_FAILED = "retrigger_failed"
    MISSING_OPTIONS = "missing_options"


class ResolutionSource:
    WEBHOOK = "webhook"
    POLLING = "polling"
    SKIPPED = "skip_resolution_check"
    TIMEOUT = "timeout"


class ExternalService:
    RUNDECK = "rundeck"
    ALERTMANAGER = "alertmanager"
    JIRA = "jira"
    SLACK = "slack"
    DYNAMODB = "dynamodb"


class JiraOperation:
    """Closed set of Jira operation label values.

    Used both as the ``operation`` label on :data:`JIRA_OPERATIONS` (recorded
    by :class:`hermes.core.remediation_manager.RemediationManager`) and as
    the ``operation`` label passed to :func:`track_call` from
    :mod:`hermes.clients.jira` so the same Jira metric series stay aligned
    across both layers.
    """

    # Low-level client methods (track_call).
    ADD_COMMENT = "add_comment"
    GET_TICKET = "get_ticket"
    SEARCH_TICKETS = "search_tickets"

    # High-level remediation operations (JIRA_OPERATIONS).
    ADD_REMEDIATION_SUCCESS_COMMENT = "add_remediation_success_comment"
    ADD_REMEDIATION_FAILURE_COMMENT = "add_remediation_failure_comment"
    ADD_JOB_FAILURE_COMMENT = "add_job_failure_comment"


class SlackNotificationType:
    """Closed set of values for the ``type`` label on :data:`SLACK_NOTIFICATIONS`.

    Add a new constant here whenever a new notification surface is added —
    do not pass free-form strings at the call site or you'll silently create
    a new label series.
    """

    ESCALATION = "escalation"


class WorkflowRecoveryOutcome:
    """Closed set of values for the ``outcome`` label on :data:`WORKFLOW_RECOVERY`."""

    RECOVERED = "recovered"
    STALE_SKIPPED = "stale_skipped"
    TERMINAL_SKIPPED = "terminal_skipped"
    FAILED = "failed"


class AttemptBucket:
    """Closed set of values for the ``attempts`` / ``attempt_number`` labels.

    The default ``RemediationConfig.max_attempts`` is ``2`` and every
    real-world value is in the low single digits, but
    ``AlertRemediationConfig.max_attempts`` is a per-alert override with
    no upper bound. Without bucketing, a misconfiguration of
    ``max_attempts=100`` (or any value beyond a sane retry policy) would
    create one new label series per integer, blowing up Prometheus
    cardinality on :data:`REMEDIATION_OUTCOMES` and
    :data:`REMEDIATION_RETRIES`.

    These constants are the only legal label values. Use
    :func:`bucket_attempts` at every call site instead of ``str(n)``.

    Bucket choice: ``"1"`` is preserved exactly because the SLO dashboards
    rely on a literal ``attempts="1"`` query for first-attempt success
    rate. ``"2"`` and ``"3"`` keep integer precision in the regime that
    matters operationally (the default policy maxes out at attempt 2).
    Anything beyond gets folded into ``"4+"`` so the long tail / runaway
    retries / misconfigurations are still visible as a single series
    rather than vanishing or blowing up cardinality.
    """

    ONE = "1"
    TWO = "2"
    THREE = "3"
    FOUR_PLUS = "4+"


def bucket_attempts(attempts: int) -> str:
    """Map an attempt count to its closed-set label value.

    Single source of truth for the ``attempts`` (on
    :data:`REMEDIATION_OUTCOMES`) and ``attempt_number`` (on
    :data:`REMEDIATION_RETRIES`) labels — every call site MUST go through
    this helper instead of ``str(n)``. See :class:`AttemptBucket` for the
    rationale.

    ``attempts`` is clamped to a minimum of ``1`` because the call sites
    treat "no recorded attempt" the same as "attempt 1" (every workflow
    has at least one job execution by the time it reaches a terminal
    state). Negative values are also clamped to keep the label space
    safe under unexpected inputs.
    """
    if attempts <= 1:
        return AttemptBucket.ONE
    if attempts == 2:
        return AttemptBucket.TWO
    if attempts == 3:
        return AttemptBucket.THREE
    return AttemptBucket.FOUR_PLUS


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
INCOMING_REQUESTS = Counter(
    "hermes_incoming_requests_total",
    "Incoming HTTP requests handled by Hermes, by route and outcome.",
    ["endpoint", "status"],
)

PROCESSING_DURATION = Histogram(
    "hermes_processing_duration_seconds",
    "End-to-end latency of HTTP requests, by route.",
    ["endpoint"],
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


# ---------------------------------------------------------------------------
# Alert intake / dedup
# ---------------------------------------------------------------------------
ALERTS_RECEIVED_BY_TYPE = Counter(
    "hermes_alerts_received_total",
    "Alerts received per alert type.",
    ["alert_type"],
)

PROCESSING_ERRORS = Counter(
    "hermes_processing_errors_total",
    "Errors raised while processing inbound alerts.",
    ["error_type", "alert_type"],
)

ALERTS_DEDUPLICATED = Counter(
    "hermes_alerts_deduplicated_total",
    "Alerts dropped because of an active workflow or cooldown.",
    ["alert_type", "reason"],
)

CONCURRENCY_LIMIT_HIT = Counter(
    "hermes_concurrency_limit_hit_total",
    "Alerts rejected because the global concurrent-workflow cap was reached.",
    ["alert_type"],
)

RATE_LIMITED_REQUESTS = Counter(
    "hermes_rate_limited_requests_total",
    "Requests rejected by the per-source / global rate limiter.",
    ["source", "limit_type"],
)


# ---------------------------------------------------------------------------
# Workflow lifecycle
# ---------------------------------------------------------------------------
REMEDIATION_WORKFLOWS = Counter(
    "hermes_remediation_workflows_total",
    "Remediation workflows started.",
    ["alert_type"],
)

REMEDIATION_OUTCOMES = Counter(
    "hermes_remediation_outcomes_total",
    "Remediation outcomes by alert type, outcome, and number of attempts at "
    "terminal time. ``attempts`` is bucketed via ``AttemptBucket`` "
    "(``\"1\"``, ``\"2\"``, ``\"3\"``, ``\"4+\"``) so cardinality stays bounded "
    "regardless of ``max_attempts`` config overrides; ``attempts=\"1\"`` "
    "still gives you first-attempt success rate exactly.",
    ["alert_type", "outcome", "attempts"],
)

REMEDIATION_RETRIES = Counter(
    "hermes_remediation_retries_total",
    "Retry attempts initiated for a remediation workflow. ``attempt_number`` "
    "is the new attempt number being started, bucketed via ``AttemptBucket`` "
    "(``\"2\"``, ``\"3\"``, ``\"4+\"``) — note ``\"1\"`` is impossible here "
    "because the first run is not a retry.",
    ["alert_type", "attempt_number"],
)

RESOLUTION_SOURCE = Counter(
    "hermes_resolution_source_total",
    "How an alert resolution was determined.",
    ["alert_type", "source"],
)

WORKFLOW_RECOVERY = Counter(
    "hermes_workflow_recovery_total",
    "Outcomes of workflow recovery on service restart.",
    ["outcome"],
)

ACTIVE_WORKFLOWS = Gauge(
    "hermes_active_workflows",
    "Current number of in-flight remediation workflows.",
)

REMEDIATION_DURATION = Histogram(
    "hermes_remediation_duration_seconds",
    "Workflow lifecycle duration from creation to terminal state.",
    ["alert_type", "outcome"],
    buckets=(60, 300, 600, 1800, 3600, 7200, 14400),
)

JOB_EXECUTION_DURATION = Histogram(
    "hermes_job_execution_duration_seconds",
    "Time from Rundeck job trigger to terminal job status.",
    ["alert_type", "status"],
    buckets=(10, 30, 60, 300, 600, 1800),
)

ALERT_RESOLUTION_WAIT = Histogram(
    "hermes_alert_resolution_wait_seconds",
    "Time from job success to alert resolution (or timeout).",
    ["alert_type", "method"],
    buckets=(10, 30, 60, 300, 600, 1800),
)


# ---------------------------------------------------------------------------
# Rundeck job triggering
# ---------------------------------------------------------------------------
RUNDECK_JOB_TRIGGERS = Counter(
    "hermes_rundeck_job_triggers_total",
    "Rundeck job trigger attempts, by alert type and outcome.",
    ["alert_type", "status"],
)


# ---------------------------------------------------------------------------
# External service calls (latency + error breakdown)
# ---------------------------------------------------------------------------
EXTERNAL_CALL_DURATION = Histogram(
    "hermes_external_call_duration_seconds",
    "External service call latency.",
    ["service", "operation", "status"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)

EXTERNAL_CALL_ERRORS = Counter(
    "hermes_external_call_errors_total",
    "External service call failures.",
    ["service", "operation", "error_type"],
)


# ---------------------------------------------------------------------------
# Reliability / circuit breakers
# ---------------------------------------------------------------------------
CIRCUIT_BREAKER_TRIPS = Counter(
    "hermes_circuit_breaker_trips_total",
    "Circuit breaker trip events.",
    ["service"],
)

CIRCUIT_BREAKER_STATE = Gauge(
    "hermes_circuit_breaker_state",
    "Circuit breaker current state (0=closed, 1=open).",
    ["service"],
)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
ESCALATIONS_SENT = Counter(
    "hermes_escalations_sent_total",
    "Escalation events dispatched, by target and result.",
    ["alert_type", "target", "result"],
)

SLACK_NOTIFICATIONS = Counter(
    "hermes_slack_notifications_total",
    "Slack notification attempts.",
    ["type", "status"],
)

JIRA_OPERATIONS = Counter(
    "hermes_jira_operations_total",
    "JIRA write/read operations issued by Hermes.",
    ["operation", "status"],
)

JIRA_TICKET_FETCH = Counter(
    "hermes_jira_ticket_fetch_total",
    "JIRA ticket lookup attempts (legacy, kept for dashboard back-compat).",
    ["alert_type", "status"],
)


# ---------------------------------------------------------------------------
# Build / config metadata
# ---------------------------------------------------------------------------
BUILD_INFO = Gauge(
    "hermes_build_info",
    "Build/config metadata. The value is always 1; use the labels.",
    ["version", "commit", "config_hash"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _classify_error(exc: BaseException) -> str:
    """Map an exception to a bounded label value (the class name, truncated)."""
    return type(exc).__name__[:64] or "Unknown"


@contextlib.asynccontextmanager
async def track_call(service: str, operation: str) -> AsyncIterator[None]:
    """
    Async context manager that records latency and error metrics around an
    external service call.

    ``service`` and ``operation`` MUST come from a closed set (see
    ``ExternalService`` and the per-client constants). Any caller wrapping a
    user-controlled string here will blow up Prometheus cardinality.

    Exception handling:

    - ``Exception`` subclasses → recorded as a service error: increments
      ``EXTERNAL_CALL_ERRORS`` and observes latency with ``status="error"``.
    - ``asyncio.CancelledError`` → propagated untouched. Cancellation is a
      control-flow signal (e.g. ``RemediationManager.shutdown`` calls
      ``task.cancel()`` on every in-flight workflow), not a dependency
      failure. Recording it would spike ``hermes_external_call_errors_total``
      on every graceful shutdown / deploy.
    - Other ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``,
      ``GeneratorExit``) → also propagated untouched. They are interpreter
      shutdown / generator-cleanup signals, not service errors.
    """
    start = time.monotonic()
    try:
        yield
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - intentionally broad, see docstring
        EXTERNAL_CALL_ERRORS.labels(
            service=service,
            operation=operation,
            error_type=_classify_error(exc),
        ).inc()
        EXTERNAL_CALL_DURATION.labels(
            service=service,
            operation=operation,
            status="error",
        ).observe(time.monotonic() - start)
        raise
    else:
        EXTERNAL_CALL_DURATION.labels(
            service=service,
            operation=operation,
            status="success",
        ).observe(time.monotonic() - start)


def set_circuit_breaker_state(service: str, is_open: bool) -> None:
    CIRCUIT_BREAKER_STATE.labels(service=service).set(1 if is_open else 0)


def init_circuit_breaker_states(services: Iterable[str]) -> None:
    """Seed the gauge at 0 for known services so dashboards have a series."""
    for service in services:
        CIRCUIT_BREAKER_STATE.labels(service=service).set(0)


def set_active_workflow_count(count: int) -> None:
    ACTIVE_WORKFLOWS.set(count)


def set_build_info(version: str, commit: str = "unknown", config_hash: str = "unknown") -> None:
    """Set the build_info gauge to 1 with the given labels."""
    BUILD_INFO.labels(version=version, commit=commit, config_hash=config_hash).set(1)


def reset_for_tests(registry: Optional[CollectorRegistry] = None) -> None:
    """
    Helper used only by tests to clear collector state between cases. Prometheus
    metrics are process-global; call this from a fixture if you need a clean
    slate.
    """
    target = registry or REGISTRY
    for collector in list(getattr(target, "_collector_to_names", {}).keys()):
        if hasattr(collector, "_metrics"):
            collector._metrics.clear()  # type: ignore[attr-defined]
        if hasattr(collector, "_metric_init"):
            try:
                collector._metric_init()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - best-effort reset
                pass


# Public re-exports
__all__ = [
    "ACTIVE_WORKFLOWS",
    "ALERT_RESOLUTION_WAIT",
    "ALERTS_DEDUPLICATED",
    "ALERTS_RECEIVED_BY_TYPE",
    "AttemptBucket",
    "BUILD_INFO",
    "CIRCUIT_BREAKER_STATE",
    "CIRCUIT_BREAKER_TRIPS",
    "CONCURRENCY_LIMIT_HIT",
    "ESCALATIONS_SENT",
    "EXTERNAL_CALL_DURATION",
    "EXTERNAL_CALL_ERRORS",
    "ExternalService",
    "INCOMING_REQUESTS",
    "JIRA_OPERATIONS",
    "JIRA_TICKET_FETCH",
    "JOB_EXECUTION_DURATION",
    "JiraOperation",
    "PROCESSING_DURATION",
    "PROCESSING_ERRORS",
    "RATE_LIMITED_REQUESTS",
    "REMEDIATION_DURATION",
    "REMEDIATION_OUTCOMES",
    "REMEDIATION_RETRIES",
    "REMEDIATION_WORKFLOWS",
    "RESOLUTION_SOURCE",
    "RUNDECK_JOB_TRIGGERS",
    "RemediationOutcome",
    "ResolutionSource",
    "SLACK_NOTIFICATIONS",
    "SlackNotificationType",
    "WORKFLOW_RECOVERY",
    "WorkflowRecoveryOutcome",
    "bucket_attempts",
    "init_circuit_breaker_states",
    "reset_for_tests",
    "set_active_workflow_count",
    "set_build_info",
    "set_circuit_breaker_state",
    "track_call",
]
