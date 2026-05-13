from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from threading import RLock
from typing import Any

PRIVACY_PATTERNS = re.compile(
    r"(\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+|"
    r"token|api.key|secret|cookie|session|otp|invoice|customer|address)\b|"
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|\b\+?\d[\d\s().-]{7,}\d\b)",
    re.IGNORECASE,
)
TOKEN_PATTERN = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def _is_uncacheable(query: str) -> bool:
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """Small deterministic in-memory response cache."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.false_hit_log: list[dict[str, object]] = []
        self._entries: list[CacheEntry] = []
        self._lock: Any = RLock()

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0

        with self._lock:
            best_entry: CacheEntry | None = None
            best_score = 0.0
            now = time.time()
            self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]

            normalized_query = _normalize(query)
            for entry in self._entries:
                if _normalize(entry.key) == normalized_query:
                    return entry.value, 1.0
                score = self.similarity(query, entry.key)
                if score > best_score:
                    best_score = score
                    best_entry = entry

            if best_entry is None:
                return None, best_score
            if _looks_like_false_hit(query, best_entry.key) and best_score >= 0.3:
                self.false_hit_log.append(_safe_false_hit_entry(query, best_entry.key, best_score))
                return None, best_score
            if best_score < self.similarity_threshold:
                return None, best_score
            return best_entry.value, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        if _is_uncacheable(query):
            return
        with self._lock:
            self._entries.append(CacheEntry(query, value, time.time(), metadata or {}))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        normalized_a = _normalize(a)
        normalized_b = _normalize(b)
        if not normalized_a or not normalized_b:
            return 0.0
        if normalized_a == normalized_b:
            return 1.0

        token_score = _jaccard(set(TOKEN_PATTERN.findall(normalized_a)), set(TOKEN_PATTERN.findall(normalized_b)))
        ngram_score = _jaccard(_char_ngrams(normalized_a), _char_ngrams(normalized_b))
        return (token_score * 0.7) + (ngram_score * 0.3)


class SharedRedisCache:
    """Redis-backed cache for multi-instance deployments."""

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0

        try:
            exact_key = f"{self.prefix}{self._query_hash(query)}"
            exact_response = self._redis.hget(exact_key, "response")
            if isinstance(exact_response, str):
                return exact_response, 1.0

            best_response: str | None = None
            best_query: str | None = None
            best_score = 0.0
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                cached_query = self._redis.hget(key, "query")
                cached_response = self._redis.hget(key, "response")
                if not isinstance(cached_query, str) or not isinstance(cached_response, str):
                    continue
                score = ResponseCache.similarity(query, cached_query)
                if score > best_score:
                    best_score = score
                    best_query = cached_query
                    best_response = cached_response

            if best_query is None or best_response is None:
                return None, best_score
            if _looks_like_false_hit(query, best_query) and best_score >= 0.3:
                self.false_hit_log.append(_safe_false_hit_entry(query, best_query, best_score))
                return None, best_score
            if best_score < self.similarity_threshold:
                return None, best_score
            return best_response, best_score
        except Exception:
            return None, 0.0

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        if _is_uncacheable(query):
            return
        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            mapping = {"query": query, "response": value}
            if metadata:
                mapping.update({f"metadata:{k}": v for k, v in metadata.items()})
            self._redis.hset(key, mapping=mapping)
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            return

    def flush(self) -> None:
        try:
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                self._redis.delete(key)
        except Exception:
            return

    def close(self) -> None:
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]


def _safe_false_hit_entry(query: str, cached_key: str, score: float) -> dict[str, object]:
    return {
        "query_hash": hashlib.sha256(query.encode()).hexdigest()[:12],
        "cached_key_hash": hashlib.sha256(cached_key.encode()).hexdigest()[:12],
        "score": round(score, 4),
    }


def _normalize(text: str) -> str:
    return " ".join(TOKEN_PATTERN.findall(text.lower()))


def _char_ngrams(text: str, size: int = 3) -> set[str]:
    compact = text.replace(" ", "")
    if len(compact) <= size:
        return {compact} if compact else set()
    return {compact[index : index + size] for index in range(len(compact) - size + 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)
