"""Client-side helpers that never own canonical Plastic Promise state."""

from plastic_promise.client.hot_memory_cache import (
    HotMemoryCacheEntry,
    HotMemoryCacheKey,
    HotMemoryCacheRequestContext,
    HotMemoryCacheSelection,
    ReadOnlyHotMemoryCache,
    hot_memory_cache_contract,
)
from plastic_promise.client.local_rerank_executor import (
    ClientLocalGatewayError,
    ClientLocalGatewayResponse,
    ClientLocalGatewayTransport,
    ClientLocalRerankExecutor,
    ClientLocalRerankExecutorError,
    HTTPXClientLocalGatewayTransport,
    LocalRerankCallable,
    LocalRerankCandidate,
    LocalRerankOutput,
    LocalRerankScore,
)

__all__ = [
    "HotMemoryCacheEntry",
    "HotMemoryCacheKey",
    "HotMemoryCacheRequestContext",
    "HotMemoryCacheSelection",
    "ReadOnlyHotMemoryCache",
    "hot_memory_cache_contract",
    "ClientLocalGatewayError",
    "ClientLocalGatewayResponse",
    "ClientLocalGatewayTransport",
    "ClientLocalRerankExecutor",
    "ClientLocalRerankExecutorError",
    "HTTPXClientLocalGatewayTransport",
    "LocalRerankCallable",
    "LocalRerankCandidate",
    "LocalRerankOutput",
    "LocalRerankScore",
]
