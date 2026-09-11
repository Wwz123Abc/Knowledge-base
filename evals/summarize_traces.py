from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.costs import estimate_cost
from app.db import SessionLocal
from app.models import RetrievalTrace


def percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * fraction)
    return ordered[min(len(ordered) - 1, max(0, index))]


def summarize(report_path: Path, output_path: Path) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    trace_ids = [item["response"]["trace_id"] for item in report["results"]]
    with SessionLocal() as db:
        traces = list(db.scalars(select(RetrievalTrace).where(RetrievalTrace.id.in_(trace_ids))))

    settings = get_settings()
    latencies = [trace.latency_ms for trace in traces if trace.latency_ms is not None]
    usage_rows = [trace.token_usage or {} for trace in traces]
    estimated_cost = sum(
        estimate_cost(settings, str(usage.get("model", "")), usage) or 0.0 for usage in usage_rows
    )
    runtime = {
        "trace_count": len(traces),
        "p50_ms": percentile(latencies, 0.50),
        "p95_ms": percentile(latencies, 0.95),
        "max_ms": max(latencies, default=None),
        "input_tokens": sum(int(usage.get("input_tokens") or 0) for usage in usage_rows),
        "output_tokens": sum(int(usage.get("output_tokens") or 0) for usage in usage_rows),
        "total_tokens": sum(int(usage.get("total_tokens") or 0) for usage in usage_rows),
        "estimated_cost_usd": round(estimated_cost, 6),
        "models": sorted({str(usage["model"]) for usage in usage_rows if usage.get("model")}),
    }
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_report": str(report_path),
        "case_count": report["case_count"],
        "quality": report["summary"],
        "runtime": runtime,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize RAG evaluation traces")
    parser.add_argument("report", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/deepseek_acceptance_summary.json"),
    )
    args = parser.parse_args()
    print(json.dumps(summarize(args.report, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
