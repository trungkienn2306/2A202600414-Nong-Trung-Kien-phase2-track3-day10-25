from reliability_lab.chaos import run_simulation
from reliability_lab.config import CacheConfig, CircuitBreakerConfig, LabConfig, LoadTestConfig, ProviderConfig, ScenarioConfig


def test_run_simulation_records_named_scenarios_and_cache_comparison() -> None:
    config = LabConfig(
        providers=[
            ProviderConfig(name="primary", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.01),
            ProviderConfig(name="backup", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.006),
        ],
        circuit_breaker=CircuitBreakerConfig(
            failure_threshold=1,
            reset_timeout_seconds=0.01,
            success_threshold=1,
        ),
        cache=CacheConfig(
            enabled=True,
            backend="memory",
            ttl_seconds=60,
            similarity_threshold=0.92,
        ),
        load_test=LoadTestConfig(requests=8, concurrency=2),
        scenarios=[
            ScenarioConfig(
                name="primary_timeout_100",
                description="Primary always fails",
                provider_overrides={"primary": 1.0},
            ),
            ScenarioConfig(
                name="all_healthy",
                description="All healthy",
                provider_overrides={"primary": 0.0, "backup": 0.0},
            ),
            ScenarioConfig(
                name="all_providers_down",
                description="All fail",
                provider_overrides={"primary": 1.0, "backup": 1.0},
            ),
        ],
    )

    metrics = run_simulation(config, ["Explain circuit breaker states", "Explain circuit breaker states"])
    report = metrics.to_report_dict()

    assert metrics.total_requests == 24
    assert report["latency_p95_ms"] >= 0
    assert report["cache_comparison"]
    assert set(metrics.scenarios) >= {"primary_timeout_100", "all_healthy", "all_providers_down"}
    assert metrics.scenarios["primary_timeout_100"] == "pass"
    assert metrics.scenarios["all_providers_down"] == "pass"
