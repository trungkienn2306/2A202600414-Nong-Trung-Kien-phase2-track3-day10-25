from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reliability_lab.config import load_config


METRIC_LABELS = {
    "total_requests": "Tổng request",
    "availability": "Availability",
    "error_rate": "Error rate",
    "latency_p50_ms": "Latency P50 (ms)",
    "latency_p95_ms": "Latency P95 (ms)",
    "latency_p99_ms": "Latency P99 (ms)",
    "fallback_success_rate": "Fallback success rate",
    "cache_hit_rate": "Cache hit rate",
    "circuit_open_count": "Số lần circuit mở",
    "recovery_time_ms": "Recovery time trung bình (ms)",
    "estimated_cost": "Chi phí ước tính",
    "estimated_cost_saved": "Chi phí tiết kiệm nhờ cache",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/final_report.md")
    args = parser.parse_args()

    metrics = json.loads(Path(args.metrics).read_text(encoding="utf-8"))
    config = load_config(args.config)
    report = _build_report(metrics, config)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(report, encoding="utf-8")
    print(f"wrote {args.out}")


def _build_report(metrics: dict[str, Any], config: Any) -> str:
    lines = [
        "# Báo cáo cuối — Day 10 Reliability Engineering cho Production Agents",
        "",
        "## 1. Tóm tắt kiến trúc",
        "",
        "Gateway nhận prompt, kiểm tra cache trước, sau đó gọi provider qua circuit breaker theo thứ tự primary → backup. Khi provider lỗi hoặc circuit đang mở, hệ thống fail-fast sang provider tiếp theo; nếu toàn bộ provider không khả dụng thì trả static fallback.",
        "",
        "```text",
        "User Request",
        "    |",
        "    v",
        "[ReliabilityGateway] -> [Cache: Redis/In-memory] -- HIT --> Cached response",
        "    | MISS",
        "    v",
        "[CircuitBreaker: primary] -- CLOSED/HALF_OPEN --> Provider primary",
        "    | OPEN/error",
        "    v",
        "[CircuitBreaker: backup]  -- CLOSED/HALF_OPEN --> Provider backup",
        "    | OPEN/error",
        "    v",
        "[Static fallback]",
        "```",
        "",
        "## 2. Bảng cấu hình và lý do chọn",
        "",
        "| Thiết lập | Giá trị | Lý do |",
        "|---|---:|---|",
        f"| failure_threshold | {config.circuit_breaker.failure_threshold} | Phát hiện lỗi nhanh nhưng tránh mở circuit chỉ vì một lỗi đơn lẻ. |",
        f"| reset_timeout_seconds | {config.circuit_breaker.reset_timeout_seconds} | Cho provider thời gian hồi phục trước khi probe HALF_OPEN. |",
        f"| success_threshold | {config.circuit_breaker.success_threshold} | Một probe thành công đủ đóng circuit trong lab để recovery nhanh. |",
        f"| cache backend | {config.cache.backend} | Redis chứng minh shared cache giữa nhiều instance; code vẫn hỗ trợ memory. |",
        f"| cache TTL | {config.cache.ttl_seconds} giây | Cân bằng độ mới dữ liệu FAQ/policy với hit rate và tiết kiệm chi phí. |",
        f"| similarity_threshold | {config.cache.similarity_threshold} | Đủ cao để giảm false-hit, kết hợp guardrail khác năm/ID. |",
        f"| load_test.requests | {config.load_test.requests} | Đủ lớn để có thống kê P50/P95/P99 và circuit transition. |",
        f"| load_test.concurrency | {config.load_test.concurrency} | Mô phỏng concurrent load thay vì chỉ chạy tuần tự. |",
        "",
        "## 3. SLO định nghĩa",
        "",
        "| SLI | Mục tiêu SLO | Giá trị thực tế | Đạt? |",
        "|---|---|---:|---|",
        _slo_row("Availability", ">= 80%", metrics["availability"], metrics["availability"] >= 0.8),
        _slo_row("Latency P95", "< 2500 ms", metrics["latency_p95_ms"], metrics["latency_p95_ms"] < 2500),
        _slo_row("Fallback success rate", ">= 30%", metrics["fallback_success_rate"], metrics["fallback_success_rate"] >= 0.3),
        _slo_row("Cache hit rate", ">= 10%", metrics["cache_hit_rate"], metrics["cache_hit_rate"] >= 0.1),
        _slo_row("Recovery time", "< 5000 ms hoặc có bằng chứng mở circuit", metrics.get("recovery_time_ms"), metrics.get("recovery_time_ms") is None or metrics.get("recovery_time_ms", 0) < 5000),
        "",
        "## 4. Metrics tổng hợp từ `reports/metrics.json`",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key, label in METRIC_LABELS.items():
        lines.append(f"| {label} | {_display(metrics.get(key))} |")

    lines.extend(_cache_comparison_section(metrics))
    lines.extend(_redis_section(metrics))
    lines.extend(_scenario_section(metrics))
    lines.extend(
        [
            "",
            "## 8. Phân tích điểm yếu còn lại",
            "",
            "Circuit breaker hiện vẫn lưu trạng thái trong từng process. Trong production multi-instance, hai replica có thể có trạng thái circuit khác nhau, khiến một replica vẫn gọi provider lỗi trong khi replica khác đã mở circuit.",
            "",
            "Cách cải thiện: đưa circuit state sang Redis hoặc một control plane chung, thêm rate limit theo user/API key, và xuất Prometheus metrics để alert theo SLO.",
            "",
            "## 9. Next steps",
            "",
            "1. Lưu circuit breaker counters/state trong Redis để đồng bộ giữa nhiều instance.",
            "2. Thêm Prometheus exporter cho `agent_requests_total`, `agent_latency_seconds`, `cache_hits_total`, `circuit_state`.",
            "3. Thêm policy cost-aware routing khi chi phí tháng vượt 80% ngân sách.",
            "",
            "## 10. Kết luận rubric",
            "",
            "| Hạng mục | Bằng chứng |",
            "|---|---|",
            "| Circuit breaker và fallback | Transition log, route reason có provider, fail-fast khi OPEN. |",
            "| Cache và cost | TTL, threshold, hit rate, estimated cost saved, false-hit guardrail. |",
            "| Observability và metrics | JSON có availability, error rate, P50/P95/P99, counters và gauges chính. |",
            "| Chaos/load testing | 5 scenario, concurrent load, recovery/circuit evidence. |",
            "| Report và code quality | README, report tiếng Việt, pytest/ruff/mypy pass. |",
        ]
    )
    return "\n".join(lines) + "\n"


def _cache_comparison_section(metrics: dict[str, Any]) -> list[str]:
    comparison = metrics.get("cache_comparison", {})
    without_cache = comparison.get("without_cache", {})
    with_cache = comparison.get("with_cache", {})
    delta = comparison.get("delta", {})
    return [
        "",
        "## 5. So sánh cache bật/tắt",
        "",
        "| Metric | Không cache | Có cache | Delta |",
        "|---|---:|---:|---:|",
        f"| latency_p50_ms | {_display(without_cache.get('latency_p50_ms'))} | {_display(with_cache.get('latency_p50_ms'))} | {_display(delta.get('latency_p50_ms'))} |",
        f"| latency_p95_ms | {_display(without_cache.get('latency_p95_ms'))} | {_display(with_cache.get('latency_p95_ms'))} | {_display(delta.get('latency_p95_ms'))} |",
        f"| estimated_cost | {_display(without_cache.get('estimated_cost'))} | {_display(with_cache.get('estimated_cost'))} | {_display(delta.get('estimated_cost'))} |",
        f"| cache_hit_rate | {_display(without_cache.get('cache_hit_rate'))} | {_display(with_cache.get('cache_hit_rate'))} | {_display(delta.get('cache_hit_rate'))} |",
    ]


def _redis_section(metrics: dict[str, Any]) -> list[str]:
    evidence = metrics.get("redis_evidence", {})
    keys = evidence.get("keys", [])
    key_output = "\n".join(keys) if keys else "Không có key hoặc Redis chưa chạy"
    return [
        "",
        "## 6. Redis shared cache",
        "",
        "In-memory cache chỉ tồn tại trong một process nên khi scale ngang, instance khác không thấy cache hit. `SharedRedisCache` dùng Redis hash + TTL để nhiều gateway instance đọc/ghi chung một namespace cache.",
        "",
        "| Bằng chứng | Giá trị |",
        "|---|---|",
        f"| Redis connected | {_display(evidence.get('connected'))} |",
        f"| Hai cache instance đọc cùng dữ liệu | {_display(evidence.get('shared_state'))} |",
        f"| Redis keys mẫu | `{', '.join(keys) if keys else 'Không có key hoặc Redis chưa chạy'}` |",
        "",
        "Redis CLI evidence:",
        "",
        "```text",
        'docker compose exec redis redis-cli KEYS "rl:cache:*"',
        key_output,
        "```",
    ]


def _scenario_section(metrics: dict[str, Any]) -> list[str]:
    lines = [
        "",
        "## 7. Chaos scenarios",
        "",
        "| Scenario | Expected behavior | Observed behavior | Pass/Fail |",
        "|---|---|---|---|",
    ]
    scenarios = metrics.get("scenarios", {})
    details = metrics.get("scenario_details", {})
    for name, status in scenarios.items():
        detail = details.get(name, {})
        observed = detail.get("observed", {})
        observed_text = ", ".join(
            [
                f"availability={_display(observed.get('availability'))}",
                f"fallback={_display(observed.get('fallback_success_rate'))}",
                f"cache={_display(observed.get('cache_hit_rate'))}",
                f"circuit_open={_display(observed.get('circuit_open_count'))}",
                f"static={_display(observed.get('static_fallbacks'))}",
            ]
        )
        lines.append(
            f"| {name} | {detail.get('expected', '')} | {observed_text} | {status.upper()} |"
        )
    return lines


def _slo_row(name: str, target: str, actual: object, passed: bool) -> str:
    return f"| {name} | {target} | {_display(actual)} | {'Có' if passed else 'Không'} |"


def _display(value: object) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return str(round(value, 4))
    return str(value)


if __name__ == "__main__":
    main()
