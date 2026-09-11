import pytest
import asyncio
import time
from unittest.mock import AsyncMock, patch, MagicMock
from app.integrations.gemini_client import GeminiClient
from app.integrations.gemini_cache import GeminiCacheManager
from app.integrations.gemini_metrics import GeminiMetricsTracker

def test_cache_manager_lru_and_ttl():
    """Verify that GeminiCacheManager stores and retrieves cached responses within TTL."""
    cache = GeminiCacheManager(max_entries=2, default_ttl_seconds=1.0)
    key1 = cache.build_cache_key("Query 1", "vid-1", "chat", "v1.0")
    key2 = cache.build_cache_key("Query 2", "vid-1", "chat", "v1.0")
    key3 = cache.build_cache_key("Query 3", "vid-1", "chat", "v1.0")

    cache.set(key1, "Answer 1")
    cache.set(key2, "Answer 2")
    assert cache.get(key1) == "Answer 1"
    assert cache.get(key2) == "Answer 2"

    # Adding 3rd key evicts oldest (LRU)
    cache.set(key3, "Answer 3")
    assert len(cache._cache) == 2
    assert cache.get(key3) == "Answer 3"

    stats = cache.get_stats()
    assert stats["cache_hits"] >= 2
    assert stats["total_cached_entries"] == 2

def test_client_caching_prevents_duplicate_api_calls():
    """Verify that subsequent identical requests return cached result with 0 API calls."""
    async def _run():
        client = GeminiClient(api_key="AIzaSyTestMockKey")
        call_count = 0

        async def mock_execute(candidate_model, prompt, system_prompt, req_id):
            nonlocal call_count
            call_count += 1
            return f"Generated answer for {prompt}", False

        client._call_single_model = mock_execute

        # 1. First call - Cache Miss (calls API)
        res1 = await client.generate_response("Test inquiry on bay 1")
        assert res1 == "Generated answer for Test inquiry on bay 1"
        assert call_count == 1

        # 2. Second call with same question - Cache Hit (0 API calls)
        res2 = await client.generate_response("Test inquiry on bay 1")
        assert res2 == res1
        assert call_count == 1, "Duplicate API call was made despite cache hit!"
    asyncio.run(_run())

def test_429_quota_exhaustion_triggers_fallback_model():
    """Verify that HTTP 429 quota exhaustion immediately tries fallback model without wasteful retry loop."""
    async def _run():
        client = GeminiClient(api_key="AIzaSyTestMockKey")
        client.model = "gemini-model-primary"

        attempted_models = []
        async def mock_call_model(model_name, prompt, system_prompt, req_id):
            attempted_models.append(model_name)
            if model_name == "gemini-model-primary":
                # Returns 429 quota exhausted
                return None, False
            elif model_name == "gemini-3.5-flash":
                # Fallback model succeeds
                return "Fallback answer", False
            return None, False

        client._call_single_model = mock_call_model

        result = await client.generate_response("Question triggering fallback")
        assert result == "Fallback answer"
        assert "gemini-model-primary" in attempted_models
        assert "gemini-3.5-flash" in attempted_models
        assert client.model == "gemini-3.5-flash"
    asyncio.run(_run())

def test_metrics_tracker_no_sensitive_data():
    """Verify metrics tracker records accurate counts and does not log API keys."""
    tracker = GeminiMetricsTracker(max_recent_logs=10)
    tracker.record_request(
        endpoint="/api/assistant/chat",
        operation_type="supervisor_qa",
        video_id="vid_001",
        cache_hit=True,
        status="SUCCESS",
        model="gemini-3.5-flash",
        latency_ms=1.2
    )
    tracker.record_request(
        endpoint="/api/assistant/chat",
        operation_type="supervisor_qa",
        video_id="vid_001",
        cache_hit=False,
        status="FAILURE",
        model="gemini-3.5-flash",
        latency_ms=120.0,
        is_429=True,
        quota_exhausted=True
    )

    summary = tracker.get_summary()
    assert summary["total_requests"] == 2
    assert summary["cache_hits"] == 1
    assert summary["calls_saved_by_cache"] == 1
    assert summary["quota_exhausted_count"] == 1

    summary_str = str(summary)
    assert "AIzaSy" not in summary_str
    assert "api_key" not in summary_str
