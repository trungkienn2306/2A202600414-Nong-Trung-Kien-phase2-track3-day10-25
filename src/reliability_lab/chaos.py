from __future__ import annotations

import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import GatewayResponse, ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider

CACHE_SAVED_TOKENS_ESTIMATE = 50


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(
    config: LabConfig,
    provider_overrides: dict[str, float] | None = None,
    cache_prefix: str = "rl:cache:",
) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
                prefix=cache_prefix,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open" and open_ts is None:
                open_ts = float(entry["ts"])
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    gateway = build_gateway(
        config,
        scenario.provider_overrides or None,
        cache_prefix=f"rl:cache:{scenario.name}:",
    )
    if isinstance(gateway.cache, SharedRedisCache):
        gateway.cache.flush()
    metrics = RunMetrics()
    request_count = config.load_test.requests
    prompts = [queries[index % len(queries)] for index in range(request_count)]
    random.Random(scenario.name).shuffle(prompts)

    with ThreadPoolExecutor(max_workers=config.load_test.concurrency) as executor:
        results = list(executor.map(gateway.complete, prompts))

    for result in results:
        _record_result(config, metrics, result)

    _attempt_recovery_probe(config, gateway, scenario)

    metrics.circuit_open_count = sum(
        1 for breaker in gateway.breakers.values() for entry in breaker.transition_log if entry["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    metrics.scenario_details[scenario.name] = _scenario_detail(gateway, metrics, scenario)
    _probe_false_hit_scenario(gateway, metrics, scenario)
    return metrics


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        metrics.cache_comparison = _cache_comparison(config, queries)
        return metrics

    combined = RunMetrics()
    recovery_times: list[float] = []
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)
        combined.scenarios[scenario.name] = "pass" if _scenario_passed(scenario.name, result) else "fail"
        combined.scenario_details[scenario.name] = result.scenario_details[scenario.name]
        if scenario.name in result.scenario_details:
            combined.scenario_details[scenario.name]["status"] = combined.scenarios[scenario.name]

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            recovery_times.append(result.recovery_time_ms)

    combined.recovery_time_ms = sum(recovery_times) / len(recovery_times) if recovery_times else None
    combined.cache_comparison = _cache_comparison(config, queries)
    combined.redis_evidence = _redis_evidence(config) if config.cache.backend == "redis" else {}
    return combined


def _record_result(config: LabConfig, metrics: RunMetrics, result: GatewayResponse) -> None:
    metrics.total_requests += 1
    metrics.estimated_cost += result.estimated_cost
    if result.cache_hit:
        metrics.cache_hits += 1
        metrics.estimated_cost_saved += _estimated_cache_savings(config)
        metrics.successful_requests += 1
    elif result.route.startswith("fallback:"):
        metrics.fallback_successes += 1
        metrics.successful_requests += 1
    elif result.route == "static_fallback":
        metrics.static_fallbacks += 1
        metrics.failed_requests += 1
    else:
        metrics.successful_requests += 1
    metrics.latencies_ms.append(result.latency_ms)


def _estimated_cache_savings(config: LabConfig) -> float:
    highest_cost = max(provider.cost_per_1k_tokens for provider in config.providers)
    return (CACHE_SAVED_TOKENS_ESTIMATE / 1000) * highest_cost


def _scenario_detail(
    gateway: ReliabilityGateway, metrics: RunMetrics, scenario: ScenarioConfig
) -> dict[str, object]:
    return {
        "description": scenario.description,
        "expected": _scenario_expected(scenario.name),
        "observed": {
            "availability": round(metrics.availability, 4),
            "fallback_success_rate": round(metrics.fallback_success_rate, 4),
            "cache_hit_rate": round(metrics.cache_hit_rate, 4),
            "circuit_open_count": metrics.circuit_open_count,
            "static_fallbacks": metrics.static_fallbacks,
            "recovery_time_ms": metrics.recovery_time_ms,
            "transition_log": {
                name: breaker.transition_log for name, breaker in gateway.breakers.items()
            },
        },
    }


def _scenario_expected(name: str) -> str:
    expectations = {
        "primary_timeout_100": "Primary mở circuit và backup xử lý phần lớn traffic.",
        "primary_flaky_50": "Có cả primary và fallback, circuit có thể dao động theo lỗi.",
        "all_healthy": "Không có lỗi, request đi qua primary hoặc cache.",
        "cache_stale_candidate": "Cache chặn false-hit khi prompt khác năm hoặc ID.",
        "all_providers_down": "Tất cả provider lỗi và static fallback được kích hoạt.",
    }
    return expectations.get(name, "Scenario có request thành công hoặc trạng thái mong đợi rõ ràng.")


def _scenario_passed(name: str, metrics: RunMetrics) -> bool:
    if name == "primary_timeout_100":
        return metrics.circuit_open_count >= 1 and metrics.fallback_success_rate >= 0.9
    if name == "primary_flaky_50":
        return metrics.availability >= 0.75 and metrics.circuit_open_count >= 1
    if name == "all_healthy":
        return metrics.error_rate == 0.0
    if name == "cache_stale_candidate":
        detail = metrics.scenario_details.get(name, {})
        return bool(detail.get("false_hit_guardrail_passed"))
    if name == "all_providers_down":
        return metrics.static_fallbacks > 0 and metrics.circuit_open_count >= 2
    return metrics.successful_requests > 0


def _attempt_recovery_probe(
    config: LabConfig, gateway: ReliabilityGateway, scenario: ScenarioConfig
) -> None:
    open_provider = next(
        (provider for provider in gateway.providers if gateway.breakers[provider.name].state.value == "open"),
        None,
    )
    if open_provider is None:
        return
    effective_fail_rate = scenario.provider_overrides.get(open_provider.name, open_provider.fail_rate)
    if effective_fail_rate >= 1.0:
        return
    time.sleep(config.circuit_breaker.reset_timeout_seconds + 0.05)
    open_provider.fail_rate = 0.0
    gateway.complete(f"recovery probe {scenario.name} {open_provider.name}")


def _probe_false_hit_scenario(
    gateway: ReliabilityGateway, metrics: RunMetrics, scenario: ScenarioConfig
) -> None:
    if scenario.name != "cache_stale_candidate" or gateway.cache is None:
        return
    gateway.cache.set("refund policy for 2024", "old policy")
    cached, score = gateway.cache.get("refund policy for 2026")
    detail = metrics.scenario_details[scenario.name]
    detail["false_hit_guardrail_passed"] = cached is None and len(gateway.cache.false_hit_log) > 0
    detail["false_hit_log"] = gateway.cache.false_hit_log


def _cache_comparison(config: LabConfig, queries: list[str]) -> dict[str, dict[str, float]]:
    enabled_config = config.model_copy(
        update={"cache": config.cache.model_copy(update={"enabled": True, "backend": "memory"})},
        deep=True,
    )
    disabled_config = config.model_copy(
        update={"cache": config.cache.model_copy(update={"enabled": False})},
        deep=True,
    )
    comparison_scenario = ScenarioConfig(
        name="cache_comparison",
        description="Compare memory cache with no cache",
        provider_overrides={"primary": 0.0, "backup": 0.0},
    )
    with_cache = run_scenario(enabled_config, queries, comparison_scenario)
    without_cache = run_scenario(disabled_config, queries, comparison_scenario)
    return {
        "without_cache": _comparison_metrics(without_cache),
        "with_cache": _comparison_metrics(with_cache),
        "delta": {
            "latency_p50_ms": round(with_cache.percentile(50) - without_cache.percentile(50), 2),
            "latency_p95_ms": round(with_cache.percentile(95) - without_cache.percentile(95), 2),
            "estimated_cost": round(with_cache.estimated_cost - without_cache.estimated_cost, 6),
            "cache_hit_rate": round(with_cache.cache_hit_rate - without_cache.cache_hit_rate, 4),
        },
    }


def _comparison_metrics(metrics: RunMetrics) -> dict[str, float]:
    return {
        "latency_p50_ms": round(metrics.percentile(50), 2),
        "latency_p95_ms": round(metrics.percentile(95), 2),
        "estimated_cost": round(metrics.estimated_cost, 6),
        "cache_hit_rate": round(metrics.cache_hit_rate, 4),
    }


def _redis_evidence(config: LabConfig) -> dict[str, object]:
    cache_a = SharedRedisCache(
        config.cache.redis_url,
        config.cache.ttl_seconds,
        config.cache.similarity_threshold,
        prefix="rl:cache:",
    )
    cache_b = SharedRedisCache(
        config.cache.redis_url,
        config.cache.ttl_seconds,
        config.cache.similarity_threshold,
        prefix="rl:cache:",
    )
    try:
        connected = cache_a.ping() and cache_b.ping()
        if not connected:
            return {"connected": False, "shared_state": False, "keys": []}
        cache_a.set("redis shared evidence query", "redis shared evidence response")
        cached, score = cache_b.get("redis shared evidence query")
        keys = list(cache_a._redis.scan_iter("rl:cache:*"))
        return {
            "connected": True,
            "shared_state": cached == "redis shared evidence response" and score == 1.0,
            "key_count": len(keys),
            "keys": [f"rl:cache:<redacted:{index}>" for index, _ in enumerate(keys[:10], start=1)],
        }
    finally:
        cache_a.close()
        cache_b.close()
