from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * fraction)
    return round(ordered[min(len(ordered) - 1, max(0, index))], 2)


async def run(base_url: str, requests: int, concurrency: int, output: Path) -> dict:
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(base_url=base_url, timeout=90) as client:

        async def execute(index: int) -> dict:
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = await client.post(
                        "/api/chat",
                        json={"question": "差旅报销需要哪些材料？"},
                    )
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    payload = (
                        response.json()
                        if response.headers.get("content-type", "").startswith("application/json")
                        else {}
                    )
                    valid = bool(payload.get("answer") and payload.get("citations"))
                    return {
                        "index": index,
                        "status": response.status_code,
                        "latency_ms": round(elapsed_ms, 2),
                        "valid": valid,
                    }
                except Exception as exc:
                    return {"index": index, "error": type(exc).__name__, "valid": False}

        results = await asyncio.gather(*(execute(index) for index in range(requests)))

    latencies = [item["latency_ms"] for item in results if "latency_ms" in item]
    successes = sum(item.get("status") == 200 and item["valid"] for item in results)
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "requests": requests,
        "concurrency": concurrency,
        "successes": successes,
        "errors": requests - successes,
        "error_rate": round((requests - successes) / requests, 4),
        "p50_ms": percentile(latencies, 0.50),
        "p95_ms": percentile(latencies, 0.95),
        "max_ms": round(max(latencies), 2) if latencies else None,
        "results": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a lightweight local RAG concurrency smoke test"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--output", type=Path, default=Path("reports/local_load_smoke.json"))
    args = parser.parse_args()
    result = asyncio.run(run(args.base_url, args.requests, args.concurrency, args.output))
    print(json.dumps({key: value for key, value in result.items() if key != "results"}, indent=2))


if __name__ == "__main__":
    main()
