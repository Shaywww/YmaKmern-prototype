"""Base MCP service framework."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

class CachePolicy(str, Enum):
    NONE = "none"; SHORT = "short"; MEDIUM = "medium"; LONG = "long"; PERMANENT = "permanent"

CACHE_TTL = {CachePolicy.NONE: 0, CachePolicy.SHORT: 300, CachePolicy.MEDIUM: 3600, CachePolicy.LONG: 86400, CachePolicy.PERMANENT: float("inf")}

class ServiceHealth(str, Enum):
    HEALTHY = "healthy"; DEGRADED = "degraded"; UNAVAILABLE = "unavailable"; UNKNOWN = "unknown"

@dataclass
class MCPServiceConfig:
    service_name: str
    description: str = ""
    cache_policy: CachePolicy = CachePolicy.MEDIUM
    timeout_seconds: float = 10.0
    max_retries: int = 2
    base_url: Optional[str] = None
    mock_mode: bool = True

@dataclass
class ServiceResult:
    success: bool
    data: Any = None
    error: Optional[str] = None
    source: str = ""
    latency_ms: float = 0.0
    cached: bool = False
    truncated: bool = False

    @staticmethod
    def ok(data: Any, source: str = "mock", latency_ms: float = 0) -> "ServiceResult":
        return ServiceResult(success=True, data=data, source=source, latency_ms=latency_ms)

    @staticmethod
    def fail(error: str) -> "ServiceResult":
        return ServiceResult(success=False, error=error)

class BaseMCPService(ABC):
    def __init__(self, config: MCPServiceConfig):
        self.config = config
        self._cache: dict[str, tuple[Any, float]] = {}
        self._last_health: ServiceHealth = ServiceHealth.UNKNOWN
        self._last_health_check: float = 0.0

    @property
    def name(self) -> str:
        return self.config.service_name

    @abstractmethod
    async def _fetch_live(self, **kwargs) -> Any: ...

    @abstractmethod
    def _get_mock(self, **kwargs) -> Any: ...

    async def query(self, cache_key: Optional[str] = None, **kwargs) -> ServiceResult:
        import time as _t
        start = _t.time()
        if cache_key and self.config.cache_policy != CachePolicy.NONE:
            cached = self._cache.get(cache_key)
            if cached is not None:
                data, ts = cached
                ttl = CACHE_TTL.get(self.config.cache_policy, 0)
                if ttl == float("inf") or (_t.time() - ts) < ttl:
                    return ServiceResult.ok(data, "cache", (_t.time() - start) * 1000)
        if not self.config.mock_mode:
            try:
                data = await self._fetch_live(**kwargs)
                if cache_key: self._cache[cache_key] = (data, _t.time())
                return ServiceResult.ok(data, "live", (_t.time() - start) * 1000)
            except Exception as e:
                return ServiceResult.fail(str(e))
        try:
            data = self._get_mock(**kwargs)
            if cache_key: self._cache[cache_key] = (data, _t.time())
            return ServiceResult.ok(data, "mock", (_t.time() - start) * 1000)
        except Exception as e:
            return ServiceResult.fail(str(e))

    def check_health(self) -> ServiceHealth:
        import time as _t
        now = _t.time()
        if now - self._last_health_check < 60:
            return self._last_health
        self._last_health_check = now
        self._last_health = ServiceHealth.HEALTHY if self.config.mock_mode else ServiceHealth.UNKNOWN
        return self._last_health

    def invalidate_cache(self, key: Optional[str] = None):
        if key: self._cache.pop(key, None)
        else: self._cache.clear()
