from datetime import UTC, datetime, timedelta

from app.core.retrieval import HybridRetriever
from app.models import DocumentVersion, KnowledgeDocument


def test_expired_document_version_is_not_effective():
    document = KnowledgeDocument(
        tenant_id="tenant-a",
        title="旧制度",
        filename="old.md",
        stored_path="old.md",
        content_hash="0" * 64,
        status="ready",
    )
    document.versions.append(
        DocumentVersion(
            version_number=1,
            content_hash="0" * 64,
            stored_path="old.md",
            status="published",
            valid_until=datetime.now(UTC) - timedelta(days=1),
        )
    )

    assert HybridRetriever._is_effective(document) is False
