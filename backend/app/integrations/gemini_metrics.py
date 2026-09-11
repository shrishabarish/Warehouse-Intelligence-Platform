import time
import datetime
import threading
from typing import Dict, Any, List, Optional
from collections import deque

class GeminiMetricsTracker:
    """
    Lightweight, thread-safe operational metrics and audit logging for Gemini API usage.
    Tracks requests, cache efficiency, 429 quota events, and latency.
    Strictly sanitizes and never records API keys or confidential prompt data.
    """
    def __init__(self, max_recent_logs: int = 100):
        self._lock = threading.Lock()
        self.total_requests = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.api_calls_made = 0
        self.success_count = 0
        self.failure_count = 0
        self.rate_limit_429_count = 0
        self.quota_exhausted_count = 0
        self.retry_count = 0
        self.model_usage: Dict[str, int] = {}
        self.recent_logs: deque = deque(maxlen=max_recent_logs)

    def record_request(
        self,
        endpoint: str,
        operation_type: str,
        video_id: Optional[str] = "global",
        cache_hit: bool = False,
        status: str = "SUCCESS",
        model: str = "gemini-3.5-flash",
        latency_ms: float = 0.0,
        retries: int = 0,
        is_429: bool = False,
        quota_exhausted: bool = False
    ):
        with self._lock:
            self.total_requests += 1
            if cache_hit:
                self.cache_hits += 1
            else:
                self.cache_misses += 1
                self.api_calls_made += 1
                self.model_usage[model] = self.model_usage.get(model, 0) + 1

            if status == "SUCCESS":
                self.success_count += 1
            else:
                self.failure_count += 1

            if is_429:
                self.rate_limit_429_count += 1
            if quota_exhausted:
                self.quota_exhausted_count += 1

            self.retry_count += retries

            log_entry = {
                "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
                "endpoint": endpoint,
                "operation_type": operation_type,
                "video_id": video_id or "global",
                "cache_hit": cache_hit,
                "status": status,
                "model": model,
                "latency_ms": round(latency_ms, 1),
                "retries": retries,
                "is_429": is_429,
                "quota_exhausted": quota_exhausted
            }
            self.recent_logs.append(log_entry)

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            total_reqs = self.total_requests
            hits = self.cache_hits
            savings_pct = round((hits / total_reqs) * 100, 1) if total_reqs > 0 else 0.0
            return {
                "total_requests": total_reqs,
                "cache_hits": hits,
                "cache_misses": self.cache_misses,
                "api_calls_made": self.api_calls_made,
                "calls_saved_by_cache": hits,
                "cache_savings_percent": savings_pct,
                "success_count": self.success_count,
                "failure_count": self.failure_count,
                "rate_limit_429_count": self.rate_limit_429_count,
                "quota_exhausted_count": self.quota_exhausted_count,
                "total_retries": self.retry_count,
                "model_usage_breakdown": dict(self.model_usage),
                "recent_logs": list(self.recent_logs)[-15:]
            }

# Global singleton metrics tracker
gemini_metrics = GeminiMetricsTracker(max_recent_logs=100)
