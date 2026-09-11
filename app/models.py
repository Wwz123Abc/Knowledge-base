"""Compatibility facade for the application's SQLAlchemy models."""

from app.domain.models.common import utc_now
from app.domain.models.connectors import ConnectorItem, ConnectorSyncRun, KnowledgeConnector
from app.domain.models.documents import (
    ChunkVectorEntry,
    DocumentAccessGroup,
    DocumentVersion,
    IngestionJob,
    KnowledgeChunk,
    KnowledgeDocument,
)
from app.domain.models.knowledge_bases import DocumentKnowledgeBase, KnowledgeBase
from app.domain.models.observability import AnswerFeedback, AuditLog, RetrievalTrace
from app.domain.models.rbac import Permission, Role, RolePermission, UserRoleAssignment
from app.domain.models.tools import ToolApproval, ToolExecution

__all__ = [
    "AnswerFeedback",
    "AuditLog",
    "ChunkVectorEntry",
    "ConnectorItem",
    "ConnectorSyncRun",
    "DocumentAccessGroup",
    "DocumentKnowledgeBase",
    "DocumentVersion",
    "IngestionJob",
    "KnowledgeBase",
    "KnowledgeChunk",
    "KnowledgeConnector",
    "KnowledgeDocument",
    "Permission",
    "RetrievalTrace",
    "Role",
    "RolePermission",
    "ToolApproval",
    "ToolExecution",
    "UserRoleAssignment",
    "utc_now",
]
