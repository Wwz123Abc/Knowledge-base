from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description="Export negative production feedback")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--token", default="")
    parser.add_argument("--output", type=Path, default=Path("evals/production_failures.jsonl"))
    args = parser.parse_args()
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    response = httpx.get(f"{args.base_url}/api/evaluation/failures", headers=headers, timeout=30)
    response.raise_for_status()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as target:
        for item in response.json():
            target.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"Exported {len(response.json())} cases to {args.output}")


if __name__ == "__main__":
    main()
