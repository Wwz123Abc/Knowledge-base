from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from evals.dataset import build_dataset, build_mechanical_dataset
from evals.metrics import aggregate, score_case


def run(
    base_url: str,
    output: Path,
    limit: int | None = None,
    one_per_category: bool = False,
    dataset: str = "sample",
    timeout_seconds: float = 180,
    answerability: str = "all",
    llm_judge: bool = False,
    token: str | None = None,
) -> dict:
    cases = build_mechanical_dataset() if dataset == "mechanical" else build_dataset()
    if answerability != "all":
        expected = answerability == "answerable"
        cases = [case for case in cases if case.answerable is expected]
    if one_per_category:
        cases = list({case.category: case for case in reversed(cases)}.values())
    cases = cases[:limit]
    results = []
    output.parent.mkdir(parents=True, exist_ok=True)
    # Only AUTH_MODE=dev accepts unauthenticated requests; a wecom/oidc target (e.g. a
    # real deployment) needs a bearer token or every /api/chat call just 401s.
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=base_url, timeout=timeout_seconds, headers=headers) as client:
        for case in cases:
            try:
                response = client.post("/api/chat", json={"question": case.question})
                response.raise_for_status()
                payload = response.json()
                scores = score_case(case, payload)
                if llm_judge:
                    from evals.llm_judge import judge_groundedness

                    scores["groundedness_llm"] = judge_groundedness(
                        case.question,
                        str(payload.get("answer", "")),
                        payload.get("citations", []) or [],
                    )
                results.append({"case": case.to_dict(), "response": payload, "scores": scores})
            except (httpx.HTTPError, ValueError) as exc:
                results.append({"case": case.to_dict(), "error": str(exc), "scores": None})
            _write_report(output, base_url, dataset, results)
    return _write_report(output, base_url, dataset, results)


def _write_report(output: Path, base_url: str, dataset: str, results: list[dict]) -> dict:
    completed_scores = [item["scores"] for item in results if item.get("scores")]
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "dataset": dataset,
        "case_count": len(results),
        "completed_count": len(completed_scores),
        "error_count": len(results) - len(completed_scores),
        "summary": aggregate(completed_scores),
        "results": results,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the enterprise RAG offline evaluation")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--output", type=Path, default=Path("reports/evaluation.json"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--one-per-category", action="store_true")
    parser.add_argument("--dataset", choices=("sample", "mechanical"), default="sample")
    parser.add_argument("--timeout-seconds", type=float, default=180)
    parser.add_argument(
        "--answerability",
        choices=("all", "answerable", "unanswerable"),
        default="all",
    )
    parser.add_argument("--llm-judge", action="store_true")
    parser.add_argument(
        "--token", default="", help="Bearer token, required for a wecom/oidc target"
    )
    args = parser.parse_args()
    report = run(
        args.base_url,
        args.output,
        args.limit,
        args.one_per_category,
        args.dataset,
        args.timeout_seconds,
        args.answerability,
        args.llm_judge,
        args.token or None,
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
