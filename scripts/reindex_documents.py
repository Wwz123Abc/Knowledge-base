"""Re-ingest all ready documents with the current parsing/chunking pipeline.

Used after splitter/loader changes so existing documents pick up the new chunking.
Run inside the API container (uses the production environment):
    docker compose exec -T api python /tmp/reindex_documents.py [limit]
"""

from __future__ import annotations

import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import IngestionJob, KnowledgeDocument
from app.services import get_document_service


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    service = get_document_service()
    completed = 0
    failed: list[tuple[str, str]] = []
    with SessionLocal() as db:
        documents = list(
            db.scalars(
                select(KnowledgeDocument)
                .where(KnowledgeDocument.status == "ready")
                .order_by(KnowledgeDocument.created_at)
            )
        )
        if limit:
            documents = documents[:limit]
        print(f"reindexing {len(documents)} documents", flush=True)
        for index, document in enumerate(documents, start=1):
            job = IngestionJob(
                tenant_id=document.tenant_id,
                document_id=document.id,
                status="queued",
                progress=0,
                created_by="system-reindex-all",
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            try:
                result = service.process_job(db, job.id)
                if result.status == "completed":
                    completed += 1
                    print(
                        f"[{index}/{len(documents)}] OK   {document.title[:40]} "
                        f"chunks={result.document.chunk_count}",
                        flush=True,
                    )
                else:
                    failed.append((document.id, f"{result.status}: {result.error_message}"))
                    print(
                        f"[{index}/{len(documents)}] FAIL {document.title[:40]} "
                        f"status={result.status}",
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                failed.append((document.id, f"exception: {type(exc).__name__}: {exc}"))
                print(f"[{index}/{len(documents)}] EXC  {document.title[:40]}", flush=True)
    print(f"DONE completed={completed} failed={len(failed)}", flush=True)
    for document_id, reason in failed:
        print(f"FAILED {document_id} {reason[:200]}", flush=True)


if __name__ == "__main__":
    main()
