# Day 10 Lab — Reliability Engineering cho Production Agents

## Báo cáo chấm điểm

- [Báo cáo cuối bằng tiếng Việt](reports/final_report.md)
- [Metrics JSON sinh từ chaos/load test](reports/metrics.json)
- Rubric gốc: [docs/RUBRIC.md](docs/RUBRIC.md)

## Kết quả hiện tại

| Hạng mục | Bằng chứng |
|---|---|
| Circuit breaker và fallback | Có state machine `CLOSED → OPEN → HALF_OPEN → CLOSED`, fail-fast khi circuit mở, route reason có provider. |
| Cache và cost | Redis/shared cache, TTL 300 giây, threshold 0.92, cache hit rate tổng 0.591, cache comparison hit rate 0.745, cost saved 0.2955. |
| Observability và metrics | `reports/metrics.json` có availability, error rate, P50/P95/P99, circuit count, recovery time, cache/cost metrics. |
| Chaos/load testing | 5 scenario, 1000 request tổng, concurrency 10, Redis evidence và cache comparison. |
| Report và code quality | `pytest`, `ruff`, `mypy` pass; report có kiến trúc, cấu hình, phân tích lỗi và next steps. |

## Kiến trúc

```text
User Request
    |
    v
[ReliabilityGateway] -> [Redis/In-memory Cache] -- HIT --> Cached response
    | MISS
    v
[CircuitBreaker: primary] -> Provider primary
    | OPEN/error
    v
[CircuitBreaker: backup]  -> Provider backup
    | OPEN/error
    v
[Static fallback]
```

## Cài đặt và chạy lại

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
docker compose up -d
python -m pytest
ruff check src tests scripts
mypy src
python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json
python scripts/generate_report.py --metrics reports/metrics.json --config configs/default.yaml --out reports/final_report.md
```

Nếu dùng `make`:

```powershell
make docker-up
make test
make lint
make typecheck
make run-chaos
make report
```

## Cấu trúc thư mục chính

```text
src/reliability_lab/
  circuit_breaker.py   # Circuit breaker 3 trạng thái và transition log
  gateway.py           # Fallback routing, route reason, latency toàn request
  cache.py             # In-memory cache và SharedRedisCache
  chaos.py             # Scenario runner, concurrent load, cache comparison
  metrics.py           # Metrics model và JSON output
  config.py            # Loader cấu hình YAML

scripts/
  run_chaos.py         # Sinh reports/metrics.json
  generate_report.py   # Sinh reports/final_report.md tiếng Việt

reports/
  metrics.json         # Kết quả chạy thật
  final_report.md      # Báo cáo cuối để chấm điểm
```

## Lệnh xác minh đã chạy

```text
python -m pytest
19 passed

ruff check src tests scripts
All checks passed!

mypy src
Success: no issues found in 8 source files

python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json
wrote reports/metrics.json
```

## Ghi chú môi trường

- Redis chạy qua Docker Compose ở `localhost:6379`.
- Lab dùng `FakeLLMProvider`, không cần gọi API thật.
- Không đưa API key hoặc secret vào report, README hay metrics.
- `.env` trong working tree đã được thay bằng placeholder; nếu bạn đã dùng key thật, hãy rotate key trước khi nộp/public repo.
