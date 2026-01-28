"""
Rate limiting for controlling request flow per Alertmanager source.

Implements a token bucket algorithm for rate limiting incoming alerts
per Alertmanager instance, preventing any single source from overwhelming
the system.
"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple
import logging


logger = logging.getLogger(__name__)


@dataclass
class TokenBucket:
    """
    Token bucket rate limiter.
    
    Allows bursts up to bucket_size, then limits to rate tokens per second.
    """
    rate: float  # tokens per second
    bucket_size: int  # max tokens in bucket
    tokens: float = field(default=0.0)
    last_update: float = field(default_factory=time.time)
    
    def __post_init__(self):
        # Start with full bucket
        self.tokens = float(self.bucket_size)
    
    def _refill(self):
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self.last_update
        self.tokens = min(self.bucket_size, self.tokens + elapsed * self.rate)
        self.last_update = now
    
    def try_acquire(self, tokens: int = 1) -> bool:
        """
        Try to acquire tokens from the bucket.
        
        Returns True if tokens were acquired, False if rate limited.
        """
        self._refill()
        
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False
    
    def time_until_available(self, tokens: int = 1) -> float:
        """Calculate seconds until tokens will be available."""
        self._refill()
        
        if self.tokens >= tokens:
            return 0.0
        
        needed = tokens - self.tokens
        return needed / self.rate


class RateLimiter:
    """
    Rate limiter for managing request flow from multiple sources.
    
    Features:
    - Per-source rate limiting (e.g., per Alertmanager URL)
    - Global rate limiting across all sources
    - Configurable burst size and rate
    """
    
    def __init__(
        self,
        default_rate: float = 10.0,  # requests per second
        default_burst: int = 50,  # max burst size
        global_rate: Optional[float] = None,  # overall rate limit
        global_burst: Optional[int] = None
    ):
        self.default_rate = default_rate
        self.default_burst = default_burst
        
        # Per-source buckets
        self._buckets: Dict[str, TokenBucket] = {}
        
        # Global bucket (optional)
        self._global_bucket: Optional[TokenBucket] = None
        if global_rate and global_burst:
            self._global_bucket = TokenBucket(rate=global_rate, bucket_size=global_burst)
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
    
    def _get_bucket(self, source: str) -> TokenBucket:
        """Get or create a token bucket for a source."""
        if source not in self._buckets:
            self._buckets[source] = TokenBucket(
                rate=self.default_rate,
                bucket_size=self.default_burst
            )
        return self._buckets[source]
    
    async def try_acquire(self, source: str, tokens: int = 1) -> Tuple[bool, Optional[str]]:
        """
        Try to acquire tokens for a request from a source.
        
        Args:
            source: The source identifier (e.g., Alertmanager URL)
            tokens: Number of tokens to acquire
        
        Returns:
            Tuple of (allowed, reason)
            - allowed: True if request should proceed
            - reason: Rate limit reason if denied, None otherwise
        """
        async with self._lock:
            # Check global limit first
            if self._global_bucket:
                if not self._global_bucket.try_acquire(tokens):
                    wait_time = self._global_bucket.time_until_available(tokens)
                    return (False, f"Global rate limit exceeded, retry in {wait_time:.1f}s")
            
            # Check per-source limit
            bucket = self._get_bucket(source)
            if not bucket.try_acquire(tokens):
                wait_time = bucket.time_until_available(tokens)
                return (False, f"Rate limit exceeded for {source}, retry in {wait_time:.1f}s")
            
            return (True, None)
    
    def get_stats(self) -> Dict[str, Dict]:
        """Get rate limiter statistics."""
        stats = {
            "sources": {},
            "global": None
        }
        
        for source, bucket in self._buckets.items():
            bucket._refill()  # Update tokens
            stats["sources"][source] = {
                "tokens_available": round(bucket.tokens, 2),
                "bucket_size": bucket.bucket_size,
                "rate_per_second": bucket.rate
            }
        
        if self._global_bucket:
            self._global_bucket._refill()
            stats["global"] = {
                "tokens_available": round(self._global_bucket.tokens, 2),
                "bucket_size": self._global_bucket.bucket_size,
                "rate_per_second": self._global_bucket.rate
            }
        
        return stats
    
    def cleanup_idle_buckets(self, max_idle_seconds: float = 3600):
        """Remove buckets that haven't been used recently."""
        now = time.time()
        to_remove = []
        
        for source, bucket in self._buckets.items():
            if now - bucket.last_update > max_idle_seconds:
                to_remove.append(source)
        
        for source in to_remove:
            del self._buckets[source]
            logger.info(f"Cleaned up idle rate limiter bucket for {source}")
        
        return len(to_remove)


@dataclass
class RateLimitConfig:
    """Rate limiting configuration."""
    enabled: bool = True
    per_source_rate: float = 10.0  # requests per second per Alertmanager
    per_source_burst: int = 50  # max burst per Alertmanager
    global_rate: float = 100.0  # total requests per second
    global_burst: int = 500  # total max burst
