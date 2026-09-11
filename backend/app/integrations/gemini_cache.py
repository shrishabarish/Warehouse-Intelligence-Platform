import time
import hashlib
import threading
from typing import Optional, Dict, Any, Tuple
from collections import OrderedDict

class GeminiCacheManager:
    """
    High-performance in-memory LRU + TTL response cache for Gemini LLM completions.
    Prevents redundant API calls across repeated supervisor queries, page reloads,
    and identical automated analysis requests.
    
    Cache Key Schema:
    {video_id}:{operation_type}:{prompt_version}:{prompt_sha256}
    """
    def __init__(self, max_entries: int = 1000, default_ttl_seconds: float = 3600.0):
        self.max_entries = max_entries
        self.default_ttl = default_ttl_seconds
        # OrderedDict mapping key -> (value, expiry_timestamp, metadata)
        self._cache: OrderedDict[str, Tuple[str, float, Dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def build_cache_key(
        prompt: str,
        video_id: Optional[str] = "global",
        operation_type: str = "assistant_chat",
        prompt_version: str = "v1.2"
    ) -> str:
        clean_vid = (video_id or "global").strip().lower()
        clean_op = (operation_type or "chat").strip().lower()
        clean_ver = (prompt_version or "v1.0").strip().lower()
        prompt_digest = hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()[:16]
        return f"{clean_vid}:{clean_op}:{clean_ver}:{prompt_digest}"

    def get(self, key: str) -> Optional[str]:
        now = time.time()
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None
            
            value, expires_at, meta = self._cache[key]
            if now > expires_at:
                # Expired entry
                del self._cache[key]
                self._misses += 1
                return None
            
            # Move to end for LRU freshness
            self._cache.move_to_end(key)
            self._hits += 1
            return value

    def set(
        self, 
        key: str, 
        value: str, 
        ttl_seconds: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        if not value or not value.strip():
            return
            
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        expires_at = time.time() + ttl
        meta = metadata or {}
        
        with self._lock:
            if key in self._cache:
                del self._cache[key]
            elif len(self._cache) >= self.max_entries:
                # Evict oldest LRU entry
                self._cache.popitem(last=False)
                
            self._cache[key] = (value, expires_at, meta)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            hit_ratio = round((self._hits / total) * 100, 1) if total > 0 else 0.0
            return {
                "total_cached_entries": len(self._cache),
                "cache_hits": self._hits,
                "cache_misses": self._misses,
                "total_lookups": total,
                "hit_ratio_percent": hit_ratio,
                "max_entries": self.max_entries,
                "default_ttl_seconds": self.default_ttl
            }

# Global singleton cache instance
gemini_cache = GeminiCacheManager(max_entries=1000, default_ttl_seconds=3600.0)
