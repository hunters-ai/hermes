"""Tests for rate limiter functionality (P3)."""
import pytest
import asyncio
import time
from unittest.mock import patch

from hermes.utils.rate_limiter import TokenBucket, RateLimiter, RateLimitConfig


class TestTokenBucket:
    """Tests for TokenBucket class."""
    
    def test_initial_full_bucket(self):
        """Bucket should start full."""
        bucket = TokenBucket(rate=10.0, bucket_size=50)
        assert bucket.tokens == 50.0
    
    def test_acquire_single_token(self):
        """Should successfully acquire single token."""
        bucket = TokenBucket(rate=10.0, bucket_size=50)
        
        result = bucket.try_acquire(1)
        
        assert result is True
        assert bucket.tokens == 49.0
    
    def test_acquire_multiple_tokens(self):
        """Should successfully acquire multiple tokens."""
        bucket = TokenBucket(rate=10.0, bucket_size=50)
        
        result = bucket.try_acquire(10)
        
        assert result is True
        assert bucket.tokens == 40.0
    
    def test_acquire_fails_when_insufficient_tokens(self):
        """Should fail when not enough tokens."""
        bucket = TokenBucket(rate=10.0, bucket_size=5)
        
        result = bucket.try_acquire(10)
        
        assert result is False
        assert bucket.tokens == 5.0  # Tokens unchanged
    
    def test_refill_over_time(self):
        """Bucket should refill over time."""
        bucket = TokenBucket(rate=100.0, bucket_size=50)  # 100 tokens/sec
        bucket.tokens = 0.0
        bucket.last_update = time.time() - 0.5  # 0.5 seconds ago
        
        # Force refill by calling try_acquire
        bucket._refill()
        
        # Should have refilled ~50 tokens (100 * 0.5)
        assert bucket.tokens >= 40.0  # Allow some tolerance
    
    def test_refill_caps_at_bucket_size(self):
        """Refill should not exceed bucket size."""
        bucket = TokenBucket(rate=100.0, bucket_size=50)
        bucket.tokens = 45.0
        bucket.last_update = time.time() - 1.0  # 1 second ago
        
        bucket._refill()
        
        assert bucket.tokens == 50.0  # Capped at bucket_size
    
    def test_time_until_available(self):
        """Should calculate wait time correctly."""
        bucket = TokenBucket(rate=10.0, bucket_size=50)
        bucket.tokens = 5.0
        
        wait_time = bucket.time_until_available(10)
        
        # Need 5 more tokens at 10/sec = 0.5 seconds
        assert 0.4 <= wait_time <= 0.6
    
    def test_time_until_available_zero_when_sufficient(self):
        """Should return 0 when tokens are available."""
        bucket = TokenBucket(rate=10.0, bucket_size=50)
        
        wait_time = bucket.time_until_available(10)
        
        assert wait_time == 0.0


class TestRateLimiter:
    """Tests for RateLimiter class."""
    
    @pytest.mark.asyncio
    async def test_allow_request_under_limit(self):
        """Should allow requests under the limit."""
        limiter = RateLimiter(default_rate=10.0, default_burst=50)
        
        allowed, reason = await limiter.try_acquire("source1")
        
        assert allowed is True
        assert reason is None
    
    @pytest.mark.asyncio
    async def test_block_request_over_per_source_limit(self):
        """Should block when per-source limit exceeded."""
        limiter = RateLimiter(default_rate=10.0, default_burst=5)
        
        # Exhaust the bucket
        for _ in range(5):
            await limiter.try_acquire("source1")
        
        allowed, reason = await limiter.try_acquire("source1")
        
        assert allowed is False
        assert "Rate limit exceeded" in reason
    
    @pytest.mark.asyncio
    async def test_separate_limits_per_source(self):
        """Different sources should have separate limits."""
        limiter = RateLimiter(default_rate=10.0, default_burst=3)
        
        # Exhaust source1
        for _ in range(3):
            await limiter.try_acquire("source1")
        
        # source2 should still work
        allowed, reason = await limiter.try_acquire("source2")
        
        assert allowed is True
    
    @pytest.mark.asyncio
    async def test_global_limit(self):
        """Should enforce global limit across all sources."""
        limiter = RateLimiter(
            default_rate=10.0,
            default_burst=100,  # High per-source limit
            global_rate=10.0,
            global_burst=5  # Low global limit
        )
        
        # Exhaust global limit using different sources
        for i in range(5):
            await limiter.try_acquire(f"source{i}")
        
        # Should be blocked by global limit
        allowed, reason = await limiter.try_acquire("source_new")
        
        assert allowed is False
        assert "Global rate limit exceeded" in reason
    
    @pytest.mark.asyncio
    async def test_get_stats(self):
        """Should return rate limiter statistics."""
        limiter = RateLimiter(
            default_rate=10.0,
            default_burst=50,
            global_rate=100.0,
            global_burst=500
        )
        
        await limiter.try_acquire("source1")
        await limiter.try_acquire("source2")
        
        stats = limiter.get_stats()
        
        assert "sources" in stats
        assert "source1" in stats["sources"]
        assert "source2" in stats["sources"]
        assert "global" in stats
        assert stats["global"] is not None
    
    def test_cleanup_idle_buckets(self):
        """Should cleanup buckets that haven't been used."""
        limiter = RateLimiter(default_rate=10.0, default_burst=50)
        
        # Create a bucket and make it old
        bucket = limiter._get_bucket("old_source")
        bucket.last_update = time.time() - 7200  # 2 hours ago
        
        removed = limiter.cleanup_idle_buckets(max_idle_seconds=3600)
        
        assert removed == 1
        assert "old_source" not in limiter._buckets


class TestRateLimitConfig:
    """Tests for RateLimitConfig dataclass."""
    
    def test_default_values(self):
        """Should have sensible defaults."""
        config = RateLimitConfig()
        
        assert config.enabled is True
        assert config.per_source_rate == 10.0
        assert config.per_source_burst == 50
        assert config.global_rate == 100.0
        assert config.global_burst == 500
    
    def test_custom_values(self):
        """Should accept custom values."""
        config = RateLimitConfig(
            enabled=False,
            per_source_rate=5.0,
            per_source_burst=20
        )
        
        assert config.enabled is False
        assert config.per_source_rate == 5.0
        assert config.per_source_burst == 20
