"""Tests for circuit breaker functionality (P0)."""
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

from hermes.core.remediation_manager import CircuitBreakerState, RemediationManager
from hermes.config import Config, RemediationConfig
from hermes.core.state_store import InMemoryStateStore


class TestCircuitBreakerState:
    """Tests for CircuitBreakerState dataclass."""
    
    def test_initial_state(self):
        """Circuit breaker should start in closed state."""
        cb = CircuitBreakerState()
        assert cb.failures == 0
        assert cb.is_open is False
        assert cb.last_failure is None
    
    def test_record_failure_increments_count(self):
        """Recording failure should increment failure count."""
        cb = CircuitBreakerState()
        cb.record_failure()
        assert cb.failures == 1
        assert cb.last_failure is not None
        
        cb.record_failure()
        assert cb.failures == 2
    
    def test_record_success_resets_state(self):
        """Recording success should reset the circuit breaker."""
        cb = CircuitBreakerState()
        cb.failures = 5
        cb.is_open = True
        cb.last_failure = datetime.utcnow()
        
        cb.record_success()
        
        assert cb.failures == 0
        assert cb.is_open is False
    
    def test_should_allow_request_under_threshold(self):
        """Should allow requests when failures are under threshold."""
        cb = CircuitBreakerState()
        cb.failures = 3
        
        assert cb.should_allow_request(failure_threshold=5) is True
    
    def test_should_block_request_over_threshold(self):
        """Should block requests when failures exceed threshold."""
        cb = CircuitBreakerState()
        cb.failures = 6
        cb.last_failure = datetime.utcnow()
        
        assert cb.should_allow_request(failure_threshold=5) is False
    
    def test_should_allow_request_after_recovery_timeout(self):
        """Should allow request after recovery timeout (half-open state)."""
        cb = CircuitBreakerState()
        cb.failures = 10
        cb.last_failure = datetime.utcnow() - timedelta(seconds=120)  # 2 minutes ago
        
        # Recovery timeout is 60 seconds
        assert cb.should_allow_request(failure_threshold=5, recovery_timeout_seconds=60) is True


class TestRemediationManagerCircuitBreaker:
    """Tests for RemediationManager circuit breaker integration."""
    
    @pytest.fixture
    def mock_config(self):
        """Create mock config for testing."""
        config = MagicMock(spec=Config)
        config.remediation = RemediationConfig(
            circuit_breaker_failure_threshold=3,
            circuit_breaker_recovery_seconds=30
        )
        config.alertmanager = None
        config.jira = None
        config.slack = None
        return config
    
    @pytest.fixture
    def mock_rundeck_client(self):
        """Create mock Rundeck client."""
        return MagicMock()
    
    @pytest.fixture
    def state_store(self):
        """Create in-memory state store."""
        return InMemoryStateStore()
    
    def test_circuit_breaker_initialization(self, mock_config, state_store, mock_rundeck_client):
        """Circuit breakers should be initialized for all services."""
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        assert "rundeck" in manager._circuit_breakers
        assert "alertmanager" in manager._circuit_breakers
        assert "jira" in manager._circuit_breakers
        assert "slack" in manager._circuit_breakers
    
    def test_check_circuit_breaker_allows_when_closed(self, mock_config, state_store, mock_rundeck_client):
        """Should allow requests when circuit breaker is closed."""
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        is_allowed, reason = manager.check_circuit_breaker("rundeck")
        
        assert is_allowed is True
        assert reason == ""
    
    def test_check_circuit_breaker_blocks_when_open(self, mock_config, state_store, mock_rundeck_client):
        """Should block requests when circuit breaker is open."""
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        # Simulate failures
        for _ in range(5):
            manager.record_circuit_failure("rundeck")
        
        is_allowed, reason = manager.check_circuit_breaker("rundeck")
        
        assert is_allowed is False
        assert "Circuit breaker OPEN" in reason
    
    def test_check_circuit_breaker_unknown_service(self, mock_config, state_store, mock_rundeck_client):
        """Should allow requests for unknown services."""
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        is_allowed, reason = manager.check_circuit_breaker("unknown_service")
        
        assert is_allowed is True
    
    def test_record_circuit_success_resets(self, mock_config, state_store, mock_rundeck_client):
        """Recording success should reset circuit breaker."""
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        # Simulate failures then success
        for _ in range(3):
            manager.record_circuit_failure("rundeck")
        
        manager.record_circuit_success("rundeck")
        
        cb = manager._circuit_breakers["rundeck"]
        assert cb.failures == 0
        assert cb.is_open is False
    
    def test_record_circuit_failure_opens_after_threshold(self, mock_config, state_store, mock_rundeck_client):
        """Circuit should open after failure threshold is reached."""
        manager = RemediationManager(mock_config, state_store, mock_rundeck_client)
        
        # Config has threshold of 3
        for _ in range(3):
            manager.record_circuit_failure("jira")
        
        cb = manager._circuit_breakers["jira"]
        assert cb.is_open is True
