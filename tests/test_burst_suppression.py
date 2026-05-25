"""Tests for burst suppression.

The feature exists to stop triggering Rundeck jobs when many distinct
fingerprints fire the same ``alert_name`` in a short window (e.g. an
infra-wide event causing one Snowpipe alert per dataflow). All state is
in-memory per pod; the tests below pin both the happy path and the
explicit trade-offs documented in ``RemediationManager``:

- counter resets on restart (verified by constructing a fresh manager)
- pruning is timestamp-based, not count-based
- the K-th fire is allowed; suppression kicks in for K+1 onwards
- dismiss clears state so a future spike can re-trip from zero
"""
from collections import deque
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from hermes.config import Config, RemediationConfig
from hermes.core.remediation_manager import RemediationManager
from hermes.core.state_store import InMemoryStateStore
from hermes.utils.metrics import BurstSuppressionPhase


@pytest.fixture
def manager():
    """Build a RemediationManager with no external clients configured.

    ``mock_config`` from ``conftest.py`` uses fields that don't exist on
    the current ``RemediationConfig`` (tracked tech debt in CLAUDE.md), so
    we construct a minimal one inline to keep these tests insulated.
    """
    config = MagicMock(spec=Config)
    config.remediation = RemediationConfig()
    config.alertmanager = None
    config.jira = None
    config.slack = None
    return RemediationManager(config, InMemoryStateStore(), MagicMock())


def _fire(manager: RemediationManager, alert_name: str, n: int) -> None:
    for _ in range(n):
        manager.record_burst_fire(alert_name)


class TestCheckBurstSuppression:
    """Behavior of the trip / drop / allow decision."""

    def test_allows_when_below_threshold(self, manager):
        """K-1 fires should not trip suppression."""
        _fire(manager, "Snowpipe", 4)
        should_suppress, _, transition, count = manager.check_burst_suppression(
            "Snowpipe", threshold=5, window_minutes=10, suppression_minutes=30
        )
        assert should_suppress is False
        assert transition is None
        assert count == 4

    def test_trips_at_threshold(self, manager):
        """The K-th call sees K fires already recorded and trips."""
        _fire(manager, "Snowpipe", 5)
        should_suppress, reason, transition, count = manager.check_burst_suppression(
            "Snowpipe", threshold=5, window_minutes=10, suppression_minutes=30
        )
        assert should_suppress is True
        assert transition == BurstSuppressionPhase.TRIPPED
        assert count == 5
        assert "threshold=5" in reason

    def test_drops_subsequent_alerts_during_suppression(self, manager):
        """After a trip, every subsequent check returns ``dropped``."""
        _fire(manager, "Snowpipe", 5)
        manager.check_burst_suppression(
            "Snowpipe", threshold=5, window_minutes=10, suppression_minutes=30
        )
        for _ in range(3):
            should_suppress, _, transition, _ = manager.check_burst_suppression(
                "Snowpipe", threshold=5, window_minutes=10, suppression_minutes=30
            )
            assert should_suppress is True
            assert transition == BurstSuppressionPhase.DROPPED

    def test_auto_lifts_when_suppression_window_expires(self, manager):
        """A stale suppression entry should be cleared, and the rolling window
        evaluated fresh (and stale entries pruned)."""
        _fire(manager, "Snowpipe", 5)
        manager.check_burst_suppression(
            "Snowpipe", threshold=5, window_minutes=10, suppression_minutes=30
        )

        # Backdate suppression so it appears expired, and backdate the recorded
        # fires past the window so pruning kicks in.
        manager._suppressed_until["Snowpipe"] = datetime.utcnow() - timedelta(seconds=1)
        old = datetime.utcnow() - timedelta(minutes=20)
        manager._burst_window["Snowpipe"] = deque([old for _ in range(5)])

        should_suppress, _, transition, count = manager.check_burst_suppression(
            "Snowpipe", threshold=5, window_minutes=10, suppression_minutes=30
        )
        assert should_suppress is False
        assert transition is None
        assert count == 0
        assert "Snowpipe" not in manager._suppressed_until

    def test_distinct_alert_names_isolated(self, manager):
        """Burst on alert A must not affect alert B."""
        _fire(manager, "AlertA", 5)
        manager.check_burst_suppression(
            "AlertA", threshold=5, window_minutes=10, suppression_minutes=30
        )
        should_suppress_b, _, _, count_b = manager.check_burst_suppression(
            "AlertB", threshold=5, window_minutes=10, suppression_minutes=30
        )
        assert should_suppress_b is False
        assert count_b == 0

    def test_pruning_drops_timestamps_outside_window(self, manager):
        """Old fires past ``window_minutes`` must not count toward the trip."""
        old = datetime.utcnow() - timedelta(minutes=30)
        manager._burst_window["Snowpipe"] = deque([old for _ in range(10)])

        should_suppress, _, _, count = manager.check_burst_suppression(
            "Snowpipe", threshold=5, window_minutes=10, suppression_minutes=30
        )
        assert should_suppress is False
        assert count == 0
        # Empty deque is dropped to avoid memory leaks for one-shot alerts.
        assert "Snowpipe" not in manager._burst_window

    def test_partial_pruning_keeps_recent_fires(self, manager):
        """Pruning must drop only the timestamps older than the cutoff."""
        now = datetime.utcnow()
        manager._burst_window["Snowpipe"] = deque([
            now - timedelta(minutes=30),
            now - timedelta(minutes=20),
            now - timedelta(minutes=5),
            now - timedelta(minutes=2),
            now - timedelta(seconds=10),
        ])

        should_suppress, _, _, count = manager.check_burst_suppression(
            "Snowpipe", threshold=5, window_minutes=10, suppression_minutes=30
        )
        assert should_suppress is False
        assert count == 3


class TestRecordBurstFire:
    def test_appends_to_window(self, manager):
        manager.record_burst_fire("Snowpipe")
        assert len(manager._burst_window["Snowpipe"]) == 1

    def test_no_record_means_no_window(self, manager):
        """``check_burst_suppression`` with no recorded fires returns count=0
        and does not create a deque (memory hygiene)."""
        should_suppress, _, _, count = manager.check_burst_suppression(
            "Snowpipe", threshold=5, window_minutes=10, suppression_minutes=30
        )
        assert should_suppress is False
        assert count == 0
        assert "Snowpipe" not in manager._burst_window


class TestMarkBurstNotified:
    """One-shot Slack page gate — flips once per cycle."""

    def test_first_call_returns_true(self, manager):
        assert manager.mark_burst_notified("Snowpipe") is True

    def test_subsequent_calls_return_false(self, manager):
        manager.mark_burst_notified("Snowpipe")
        assert manager.mark_burst_notified("Snowpipe") is False
        assert manager.mark_burst_notified("Snowpipe") is False

    def test_cleared_by_dismiss(self, manager):
        manager.mark_burst_notified("Snowpipe")
        manager._suppressed_until["Snowpipe"] = datetime.utcnow() + timedelta(minutes=10)
        manager.clear_burst_suppression("Snowpipe")
        # After dismiss, a future trip should be allowed to page again.
        assert manager.mark_burst_notified("Snowpipe") is True


class TestClearBurstSuppression:
    def test_clears_all_state(self, manager):
        _fire(manager, "Snowpipe", 5)
        manager.check_burst_suppression(
            "Snowpipe", threshold=5, window_minutes=10, suppression_minutes=30
        )
        manager.mark_burst_notified("Snowpipe")

        assert manager.clear_burst_suppression("Snowpipe") is True
        assert "Snowpipe" not in manager._suppressed_until
        assert "Snowpipe" not in manager._burst_window
        assert "Snowpipe" not in manager._suppression_notified_at

    def test_returns_false_when_nothing_to_clear(self, manager):
        assert manager.clear_burst_suppression("Snowpipe") is False

    def test_post_dismiss_next_k_fires_trip_again(self, manager):
        """Dismissing should not whitelist the alert — the next K fires
        must trip again from zero."""
        _fire(manager, "Snowpipe", 5)
        manager.check_burst_suppression(
            "Snowpipe", threshold=5, window_minutes=10, suppression_minutes=30
        )
        manager.clear_burst_suppression("Snowpipe")

        _fire(manager, "Snowpipe", 5)
        should_suppress, _, transition, count = manager.check_burst_suppression(
            "Snowpipe", threshold=5, window_minutes=10, suppression_minutes=30
        )
        assert should_suppress is True
        assert transition == BurstSuppressionPhase.TRIPPED
        assert count == 5


class TestRestartSemantics:
    """The documented trade-off: restart resets the counter. The spike will
    refill it and re-trip; in the meantime a few extra Rundeck jobs may run."""

    def _make_manager(self, store):
        config = MagicMock(spec=Config)
        config.remediation = RemediationConfig()
        config.alertmanager = None
        config.jira = None
        config.slack = None
        return RemediationManager(config, store, MagicMock())

    def test_new_manager_starts_clean(self):
        store = InMemoryStateStore()
        m1 = self._make_manager(store)
        _fire(m1, "Snowpipe", 5)
        m1.check_burst_suppression(
            "Snowpipe", threshold=5, window_minutes=10, suppression_minutes=30
        )
        assert "Snowpipe" in m1._suppressed_until

        # Simulated restart: brand-new manager. Suppression state is gone.
        m2 = self._make_manager(store)
        assert m2._suppressed_until == {}
        assert m2._burst_window == {}
        assert m2._suppression_notified_at == {}

    def test_post_restart_spike_re_trips_quickly(self):
        """After restart, the counter is empty. K alerts later it trips again."""
        m = self._make_manager(InMemoryStateStore())

        # First 4 alerts pass through (would-be Rundeck triggers).
        for _ in range(4):
            should_suppress, _, _, _ = m.check_burst_suppression(
                "Snowpipe", threshold=5, window_minutes=10, suppression_minutes=30
            )
            assert should_suppress is False
            m.record_burst_fire("Snowpipe")

        # The 5th alert now sees 4 fires recorded → still below threshold.
        # The endpoint would then record a 5th fire.
        m.record_burst_fire("Snowpipe")

        # The 6th alert sees 5 fires → trips.
        should_suppress, _, transition, count = m.check_burst_suppression(
            "Snowpipe", threshold=5, window_minutes=10, suppression_minutes=30
        )
        assert should_suppress is True
        assert transition == BurstSuppressionPhase.TRIPPED
        assert count == 5
