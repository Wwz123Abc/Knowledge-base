"""Delete old retrieval traces and audit-log rows.

Every question writes a retrieval trace (query, scores, the answer) and one or more audit rows,
and nothing ever removed them. Run this from time to time (cron, or by hand) to keep those
tables — and the audit page's paging — from growing without bound. Answer feedback is kept: it
is the input to the evaluation set and is small.

    docker compose exec -T api python scripts/prune_history.py --dry-run
    docker compose exec -T api python scripts/prune_history.py --traces-days 180 --audit-days 365
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import AuditLog, RetrievalTrace

BATCH = 5000


def prune_older_than(db: Session, model, cutoff: datetime, dry_run: bool = False) -> int:
    """Delete rows of `model` created before `cutoff`, in batches; returns how many."""
    if dry_run:
        return db.scalar(select(func.count()).select_from(model).where(model.created_at < cutoff))
    removed = 0
    while True:
        ids = list(db.scalars(select(model.id).where(model.created_at < cutoff).limit(BATCH)))
        if not ids:
            return removed
        db.execute(delete(model).where(model.id.in_(ids)))
        db.commit()
        removed += len(ids)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--traces-days", type=int, default=180)
    parser.add_argument("--audit-days", type=int, default=365)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from app.db import SessionLocal

    now = datetime.now(UTC)
    with SessionLocal() as db:
        for label, model, days in (
            ("retrieval_traces", RetrievalTrace, args.traces_days),
            ("audit_logs", AuditLog, args.audit_days),
        ):
            count = prune_older_than(db, model, now - timedelta(days=days), args.dry_run)
            verb = "would delete" if args.dry_run else "deleted"
            print(f"{label}: {verb} {count} rows older than {days} days")


if __name__ == "__main__":
    main()
