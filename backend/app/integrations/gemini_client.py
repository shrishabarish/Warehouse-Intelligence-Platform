import time
import uuid
import random
import asyncio
import logging
from typing import Optional, Dict, List, Any, Tuple
import httpx
from app.config import settings
from app.integrations.gemini_cache import gemini_cache
from app.integrations.gemini_metrics import gemini_metrics

logger = logging.getLogger(__name__)

# Fallback sequence of high-efficiency Gemini models
FALLBACK_MODELS = [
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash"
]


class AdaptiveRateLimiter:
    """
    Non-blocking sliding-window rate limiter.
    Adds ZERO delay when operating under quota budget (normal path is immediate).
    Only paces requests if the safe RPM limit is actively reached.
    """
    def __init__(self, max_rpm: int = 14):
        self.max_rpm = max_rpm
        self.window_seconds = 60.0
        self.timestamps = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.time()
            # Clean up timestamps older than window
            self.timestamps = [t for t in self.timestamps if now - t < self.window_seconds]
            
            if len(self.timestamps) >= self.max_rpm:
                # Need to wait only until the oldest timestamp exits the window
                oldest = self.timestamps[0]
                wait_time = max(0.05, (oldest + self.window_seconds) - now + 0.1)
                logger.info(f"[RateLimiter] Pacing request for {wait_time:.2f}s to protect API quota ({len(self.timestamps)}/{self.max_rpm} RPM)")
                await asyncio.sleep(wait_time)
                now = time.time()
                self.timestamps = [t for t in self.timestamps if now - t < self.window_seconds]

            self.timestamps.append(time.time())


class GeminiClient:
    """
    Enterprise-grade Gemini integration client with:
    - In-memory LRU + TTL Result Caching (Zero API calls for repeated queries)
    - Concurrency In-Flight Request Deduplication
    - Automatic Multi-Model Quota Failover
    - Fail-Fast Protection against 429 Daily Quota Exhaustion (No wasteful retry sleeps)
    - Lightweight Operational Metrics & Audit Tracking
    """
    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key
        self.model = settings.GEMINI_MODEL
        self.base_url = settings.GEMINI_BASE_URL
        self.prompt_version = "v1.2"
        self._rate_limiter = AdaptiveRateLimiter(max_rpm=14)
        self._in_flight_requests: Dict[str, asyncio.Future] = {}
        self._in_flight_lock = asyncio.Lock()

    @property
    def api_key(self) -> str:
        return self._api_key or settings.GEMINI_API_KEY

    def is_configured(self) -> bool:
        key = self.api_key
        return bool(key and key.strip() and not key.startswith("mock_"))

    async def generate_response(
        self,
        prompt: str,
        system_prompt: str = settings.SYSTEM_ASSISTANT_PROMPT,
        request_id: Optional[str] = None,
        operation_type: str = "assistant_chat",
        video_id: Optional[str] = "global",
        endpoint: str = "/api/assistant/chat"
    ) -> Optional[str]:
        if not self.is_configured():
            return None

        req_id = request_id or f"gemini-{uuid.uuid4().hex[:8]}"

        # 1. Check Result Cache (Immediate zero-cost return)
        cache_key = gemini_cache.build_cache_key(
            prompt=f"{system_prompt}\n{prompt}",
            video_id=video_id,
            operation_type=operation_type,
            prompt_version=self.prompt_version
        )
        cached_result = gemini_cache.get(cache_key)
        if cached_result is not None:
            gemini_metrics.record_request(
                endpoint=endpoint,
                operation_type=operation_type,
                video_id=video_id,
                cache_hit=True,
                status="SUCCESS",
                model=self.model,
                latency_ms=0.5
            )
            logger.info(f"[{req_id}] Cache HIT (Saved 1 Gemini API call) | op={operation_type} vid={video_id}")
            return cached_result

        # 2. In-flight Deduplication for identical concurrent queries
        dedup_key = f"{self.model}:{hash((prompt, system_prompt))}"
        async with self._in_flight_lock:
            if dedup_key in self._in_flight_requests:
                logger.info(f"[{req_id}] Reusing in-flight Gemini request for duplicate query")
                return await self._in_flight_requests[dedup_key]
            
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._in_flight_requests[dedup_key] = future

        start_t = time.time()
        try:
            result = await self._execute_with_retry(prompt, system_prompt, req_id)
            if not future.done():
                future.set_result(result)

            elapsed_ms = (time.time() - start_t) * 1000
            if result:
                # Store in LRU Cache for subsequent calls
                gemini_cache.set(
                    key=cache_key,
                    value=result,
                    ttl_seconds=3600.0,
                    metadata={"model": self.model, "video_id": video_id}
                )
                gemini_metrics.record_request(
                    endpoint=endpoint,
                    operation_type=operation_type,
                    video_id=video_id,
                    cache_hit=False,
                    status="SUCCESS",
                    model=self.model,
                    latency_ms=elapsed_ms
                )
            else:
                gemini_metrics.record_request(
                    endpoint=endpoint,
                    operation_type=operation_type,
                    video_id=video_id,
                    cache_hit=False,
                    status="FAILURE",
                    model=self.model,
                    latency_ms=elapsed_ms
                )

            return result
        except Exception as e:
            if not future.done():
                future.set_exception(e)
            gemini_metrics.record_request(
                endpoint=endpoint,
                operation_type=operation_type,
                video_id=video_id,
                cache_hit=False,
                status="ERROR",
                model=self.model,
                latency_ms=(time.time() - start_t) * 1000
            )
            raise e
        finally:
            async with self._in_flight_lock:
                self._in_flight_requests.pop(dedup_key, None)

    async def _execute_with_retry(
        self,
        prompt: str,
        system_prompt: str,
        req_id: str
    ) -> Optional[str]:
        # Build candidate model list with current model first, followed by fallbacks
        models_to_try = [self.model]
        for fb_model in FALLBACK_MODELS:
            if fb_model not in models_to_try:
                models_to_try.append(fb_model)

        for candidate_model in models_to_try:
            result, is_fatal = await self._call_single_model(candidate_model, prompt, system_prompt, req_id)
            if result is not None:
                if candidate_model != self.model:
                    logger.info(f"[{req_id}] Multi-model failover succeeded with model={candidate_model}")
                    self.model = candidate_model
                return result
            if is_fatal:
                # Fatal client errors (400, 401, 403, 404) must fail fast without trying other models
                break

        logger.error(f"[{req_id}] All candidate Gemini models exhausted or unavailable.")
        return None

    async def _call_single_model(
        self,
        model_name: str,
        prompt: str,
        system_prompt: str,
        req_id: str
    ) -> Tuple[Optional[str], bool]:
        url = f"{self.base_url}/{model_name}:generateContent?key={self.api_key}"
        
        payload = {
            "system_instruction": {
                "parts": [{"text": system_prompt}]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}]
                }
            ],
            "generationConfig": {
                "temperature": settings.GEMINI_TEMPERATURE,
                "maxOutputTokens": settings.GEMINI_MAX_OUTPUT_TOKENS
            }
        }

        headers = {"Content-Type": "application/json"}
        max_attempts = 3
        approx_in_tokens = (len(prompt) + len(system_prompt)) // 4

        for attempt in range(1, max_attempts + 1):
            await self._rate_limiter.acquire()
            start_t = time.time()

            try:
                async with httpx.AsyncClient(timeout=12.0) as client:
                    response = await client.post(url, headers=headers, json=payload)
                    latency_ms = round((time.time() - start_t) * 1000, 1)

                    # HTTP 200: Immediate Success
                    if response.status_code == 200:
                        data = response.json()
                        candidates = data.get("candidates", [])
                        if candidates and "content" in candidates[0]:
                            parts = candidates[0]["content"].get("parts", [])
                            if parts and "text" in parts[0]:
                                text_out = parts[0]["text"]
                                approx_out_tokens = len(text_out) // 4
                                logger.info(
                                    f"[{req_id}] HTTP 200 OK | model={model_name} | "
                                    f"latency={latency_ms}ms | tokens≈{approx_in_tokens}in/{approx_out_tokens}out"
                                )
                                return text_out, False

                    # HTTP 400, 401, 403, 404: Non-transient client errors (FAIL FAST, NO RETRY, FATAL)
                    if response.status_code in [400, 401, 403, 404]:
                        err_msg = ""
                        try:
                            err_msg = response.json().get("error", {}).get("message", response.text)
                        except Exception:
                            err_msg = response.text
                        logger.warning(f"[{req_id}] Non-retryable error {response.status_code} ({model_name}): {err_msg}")
                        return None, True

                    # HTTP 429: Check if daily quota exhausted vs transient burst
                    if response.status_code == 429:
                        err_text = response.text
                        is_daily_quota = (
                            "exceeded your current quota" in err_text or 
                            "RESOURCE_EXHAUSTED" in err_text or 
                            "free_tier_requests" in err_text or
                            "GenerateRequestsPerDay" in err_text
                        )
                        if is_daily_quota:
                            logger.warning(
                                f"[{req_id}] Quota EXHAUSTED for model {model_name}. "
                                f"Failing fast to try fallback model."
                            )
                            return None, False

                        # Transient short-term burst: do bounded backoff
                        retry_after = response.headers.get("Retry-After")
                        if retry_after and retry_after.isdigit():
                            backoff = min(float(retry_after), 3.0)
                        else:
                            backoff = (1.0 * (2 ** (attempt - 1))) + random.uniform(0.1, 0.3)

                        logger.warning(
                            f"[{req_id}] Transient 429 rate limit ({model_name}, attempt {attempt}/{max_attempts}). "
                            f"Backing off {backoff:.2f}s..."
                        )
                        if attempt < max_attempts:
                            await asyncio.sleep(backoff)
                            continue
                        return None, False

                    # HTTP 500, 502, 503, 504: Transient server errors
                    if response.status_code in [500, 502, 503, 504]:
                        backoff = (1.0 * (2 ** (attempt - 1))) + random.uniform(0.1, 0.3)
                        logger.warning(
                            f"[{req_id}] Transient status {response.status_code} ({model_name}, attempt {attempt}/{max_attempts}). "
                            f"Backing off for {backoff:.2f}s..."
                        )
                        if attempt < max_attempts:
                            await asyncio.sleep(backoff)
                            continue
                        return None, False

                    logger.warning(f"[{req_id}] Unexpected status {response.status_code}: {response.text[:200]}")
                    return None, False

            except (httpx.TimeoutException, httpx.NetworkError) as net_err:
                latency_ms = round((time.time() - start_t) * 1000, 1)
                logger.warning(f"[{req_id}] Network error ({net_err.__class__.__name__}) on attempt {attempt}/{max_attempts} after {latency_ms}ms")
                if attempt < max_attempts:
                    backoff = (1.0 * (2 ** (attempt - 1))) + random.uniform(0.1, 0.3)
                    await asyncio.sleep(backoff)
                    continue
                return None, False
            except Exception as e:
                logger.error(f"[{req_id}] Unexpected exception: {e}")
                return None, False

        return None, False

    async def check_connection(self) -> Dict[str, Any]:
        """
        Non-destructive diagnostic probe to test Gemini API key validity and active model health.
        """
        if not self.is_configured():
            return {
                "status": "NOT_CONFIGURED",
                "message": "Gemini API key is not configured",
                "active_model": self.model,
                "connected": False
            }

        probe_url = f"{self.base_url}/{self.model}:generateContent?key={self.api_key}"
        probe_payload = {
            "contents": [{"parts": [{"text": "Ping"}]}],
            "generationConfig": {"maxOutputTokens": 4}
        }
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.post(probe_url, json=probe_payload)
                if r.status_code == 200:
                    return {
                        "status": "ONLINE",
                        "message": f"Connected to Gemini API ({self.model})",
                        "active_model": self.model,
                        "connected": True
                    }
                elif r.status_code == 429:
                    return {
                        "status": "QUOTA_EXHAUSTED",
                        "message": f"Daily quota exhausted for {self.model}. Resilient fallback active.",
                        "active_model": self.model,
                        "connected": False
                    }
                elif r.status_code in [401, 403]:
                    return {
                        "status": "INVALID_KEY",
                        "message": "Invalid or unauthorized API key.",
                        "active_model": self.model,
                        "connected": False
                    }
                else:
                    return {
                        "status": "DEGRADED",
                        "message": f"HTTP {r.status_code}: {r.text[:100]}",
                        "active_model": self.model,
                        "connected": False
                    }
        except Exception as e:
            return {
                "status": "OFFLINE",
                "message": str(e),
                "active_model": self.model,
                "connected": False
            }


gemini_client = GeminiClient()
