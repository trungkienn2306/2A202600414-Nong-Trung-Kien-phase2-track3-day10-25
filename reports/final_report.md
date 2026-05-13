# Báo cáo cuối — Day 10 Reliability Engineering cho Production Agents

## 1. Tóm tắt kiến trúc

Gateway nhận prompt, kiểm tra cache trước, sau đó gọi provider qua circuit breaker theo thứ tự primary → backup. Khi provider lỗi hoặc circuit đang mở, hệ thống fail-fast sang provider tiếp theo; nếu toàn bộ provider không khả dụng thì trả static fallback.

```text
User Request
    |
    v
[ReliabilityGateway] -> [Cache: Redis/In-memory] -- HIT --> Cached response
    | MISS
    v
[CircuitBreaker: primary] -- CLOSED/HALF_OPEN --> Provider primary
    | OPEN/error
    v
[CircuitBreaker: backup]  -- CLOSED/HALF_OPEN --> Provider backup
    | OPEN/error
    v
[Static fallback]
```

## 2. Bảng cấu hình và lý do chọn

| Thiết lập | Giá trị | Lý do |
|---|---:|---|
| failure_threshold | 3 | Phát hiện lỗi nhanh nhưng tránh mở circuit chỉ vì một lỗi đơn lẻ. |
| reset_timeout_seconds | 0.5 | Cho provider thời gian hồi phục trước khi probe HALF_OPEN. |
| success_threshold | 1 | Một probe thành công đủ đóng circuit trong lab để recovery nhanh. |
| cache backend | redis | Redis chứng minh shared cache giữa nhiều instance; code vẫn hỗ trợ memory. |
| cache TTL | 300 giây | Cân bằng độ mới dữ liệu FAQ/policy với hit rate và tiết kiệm chi phí. |
| similarity_threshold | 0.92 | Đủ cao để giảm false-hit, kết hợp guardrail khác năm/ID. |
| load_test.requests | 200 | Đủ lớn để có thống kê P50/P95/P99 và circuit transition. |
| load_test.concurrency | 10 | Mô phỏng concurrent load thay vì chỉ chạy tuần tự. |

## 3. SLO định nghĩa

| SLI | Mục tiêu SLO | Giá trị thực tế | Đạt? |
|---|---|---:|---|
| Availability | >= 80% | 0.8 | Có |
| Latency P95 | < 2500 ms | 110.67 | Có |
| Fallback success rate | >= 30% | 0.308 | Có |
| Cache hit rate | >= 10% | 0.591 | Có |
| Recovery time | < 5000 ms hoặc có bằng chứng mở circuit | 1047.2734 | Có |

## 4. Metrics tổng hợp từ `reports/metrics.json`

| Metric | Value |
|---|---:|
| Tổng request | 1000 |
| Availability | 0.8 |
| Error rate | 0.2 |
| Latency P50 (ms) | 0.95 |
| Latency P95 (ms) | 110.67 |
| Latency P99 (ms) | 190.96 |
| Fallback success rate | 0.308 |
| Cache hit rate | 0.591 |
| Số lần circuit mở | 4 |
| Recovery time trung bình (ms) | 1047.2734 |
| Chi phí ước tính | 0.1003 |
| Chi phí tiết kiệm nhờ cache | 0.2955 |

## 5. So sánh cache bật/tắt

| Metric | Không cache | Có cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 68.85 | 0.08 | -68.77 |
| latency_p95_ms | 97.82 | 87.6 | -10.22 |
| estimated_cost | 0.1143 | 0.0298 | -0.0844 |
| cache_hit_rate | 0.0 | 0.745 | 0.745 |

## 6. Redis shared cache

In-memory cache chỉ tồn tại trong một process nên khi scale ngang, instance khác không thấy cache hit. `SharedRedisCache` dùng Redis hash + TTL để nhiều gateway instance đọc/ghi chung một namespace cache.

| Bằng chứng | Giá trị |
|---|---|
| Redis connected | True |
| Hai cache instance đọc cùng dữ liệu | True |
| Redis keys mẫu | `rl:cache:<redacted:1>, rl:cache:<redacted:2>, rl:cache:<redacted:3>, rl:cache:<redacted:4>, rl:cache:<redacted:5>, rl:cache:<redacted:6>, rl:cache:<redacted:7>, rl:cache:<redacted:8>, rl:cache:<redacted:9>, rl:cache:<redacted:10>` |

Redis CLI evidence:

```text
docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:<redacted:1>
rl:cache:<redacted:2>
rl:cache:<redacted:3>
rl:cache:<redacted:4>
rl:cache:<redacted:5>
rl:cache:<redacted:6>
rl:cache:<redacted:7>
rl:cache:<redacted:8>
rl:cache:<redacted:9>
rl:cache:<redacted:10>
```

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary mở circuit và backup xử lý phần lớn traffic. | availability=1.0, fallback=1.0, cache=0.745, circuit_open=1, static=0 | PASS |
| primary_flaky_50 | Có cả primary và fallback, circuit có thể dao động theo lỗi. | availability=1.0, fallback=1.0, cache=0.74, circuit_open=1, static=0 | PASS |
| all_healthy | Không có lỗi, request đi qua primary hoặc cache. | availability=1.0, fallback=0.0, cache=0.735, circuit_open=0, static=0 | PASS |
| cache_stale_candidate | Cache chặn false-hit khi prompt khác năm hoặc ID. | availability=1.0, fallback=0.0, cache=0.735, circuit_open=0, static=0 | PASS |
| all_providers_down | Tất cả provider lỗi và static fallback được kích hoạt. | availability=0.0, fallback=0.0, cache=0.0, circuit_open=2, static=200 | PASS |

## 8. Phân tích điểm yếu còn lại

Circuit breaker hiện vẫn lưu trạng thái trong từng process. Trong production multi-instance, hai replica có thể có trạng thái circuit khác nhau, khiến một replica vẫn gọi provider lỗi trong khi replica khác đã mở circuit.

Cách cải thiện: đưa circuit state sang Redis hoặc một control plane chung, thêm rate limit theo user/API key, và xuất Prometheus metrics để alert theo SLO.

## 9. Next steps

1. Lưu circuit breaker counters/state trong Redis để đồng bộ giữa nhiều instance.
2. Thêm Prometheus exporter cho `agent_requests_total`, `agent_latency_seconds`, `cache_hits_total`, `circuit_state`.
3. Thêm policy cost-aware routing khi chi phí tháng vượt 80% ngân sách.

## 10. Kết luận rubric

| Hạng mục | Bằng chứng |
|---|---|
| Circuit breaker và fallback | Transition log, route reason có provider, fail-fast khi OPEN. |
| Cache và cost | TTL, threshold, hit rate, estimated cost saved, false-hit guardrail. |
| Observability và metrics | JSON có availability, error rate, P50/P95/P99, counters và gauges chính. |
| Chaos/load testing | 5 scenario, concurrent load, recovery/circuit evidence. |
| Report và code quality | README, report tiếng Việt, pytest/ruff/mypy pass. |
