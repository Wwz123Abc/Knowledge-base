from datetime import datetime

from pydantic import BaseModel, Field


class DocumentOut(BaseModel):
    id: str
    title: str
    filename: str
    department: str | None
    access_group: str | None
    access_groups: list[str] = Field(default_factory=list)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    status: str
    chunk_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    department: str | None = Field(default=None, max_length=120)
    access_groups: list[str] | None = Field(default=None, max_length=50)
    knowledge_base_ids: list[str] | None = Field(default=None, max_length=20)


class VersionOut(BaseModel):
    id: str
    document_id: str
    version_number: int
    content_hash: str
    status: str
    owner_id: str | None
    published_by: str | None
    valid_from: datetime | None
    valid_until: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class VersionUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(draft|published|expired)$")
    owner_id: str | None = Field(default=None, max_length=160)
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    conversation_history: list[dict[str, str]] = Field(default_factory=list, max_length=10)
    conversation_id: str | None = Field(default=None, max_length=80)
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=20)


class Citation(BaseModel):
    document_id: str
    title: str
    chunk_id: str
    page_number: int | None = None
    excerpt: str


class AskResponse(BaseModel):
    trace_id: str
    answer: str
    citations: list[Citation]
    insufficient_context: bool = False
    fallback: bool = False


class HealthResponse(BaseModel):
    status: str
    database: str
    redis: str
    clamav: str
    model: str
    details: dict[str, str] = Field(default_factory=dict)
    vector_backend: str
    model_ready: bool


class JobOut(BaseModel):
    id: str
    document_id: str
    status: str
    progress: int
    attempts: int
    error_message: str | None
    cancel_requested: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UploadAccepted(BaseModel):
    document: DocumentOut
    job: JobOut


class FeedbackIn(BaseModel):
    trace_id: str = Field(min_length=1, max_length=80)
    score: int = Field(ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackOut(BaseModel):
    id: str
    trace_id: str
    score: int
    comment: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AuthOut(BaseModel):
    user_id: str
    display_name: str
    tenant_id: str
    groups: list[str]
    roles: list[str]
    permissions: list[str]


class AuditOut(BaseModel):
    id: str
    user_id: str
    action: str
    resource_type: str
    resource_id: str | None
    details: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class ConnectorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    connector_type: str = Field(pattern="^directory$")
    configuration: dict


class ConnectorOut(BaseModel):
    id: str
    name: str
    connector_type: str
    configuration: dict
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConnectorSyncOut(BaseModel):
    id: str
    connector_id: str
    status: str
    discovered: int
    created_count: int
    updated_count: int
    deleted_count: int
    failed_count: int
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class ToolRequest(BaseModel):
    tool_name: str = Field(pattern="^(sql.read|http.get)$")
    arguments: dict


class ToolDecision(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    note: str | None = Field(default=None, max_length=2000)


class ToolApprovalOut(BaseModel):
    id: str
    user_id: str
    tool_name: str
    arguments: dict
    status: str
    decision_by: str | None
    decision_note: str | None
    created_at: datetime
    decided_at: datetime | None

    model_config = {"from_attributes": True}


class ToolExecutionOut(BaseModel):
    id: str
    approval_id: str
    status: str
    result: dict
    error_message: str | None
    executed_at: datetime

    model_config = {"from_attributes": True}


class ToolDecisionResult(BaseModel):
    approval: ToolApprovalOut
    execution: ToolExecutionOut | None


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    routing_keywords: list[str] = Field(default_factory=list, max_length=100)


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    routing_keywords: list[str] | None = Field(default=None, max_length=100)
    enabled: bool | None = None


class KnowledgeBaseOut(BaseModel):
    id: str
    name: str
    description: str | None
    routing_keywords: list[str]
    enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class WeComDepartmentOut(BaseModel):
    id: int
    name: str
    parent_id: int


class WeComQrConfigOut(BaseModel):
    corp_id: str
    agent_id: str
    redirect_uri: str
    state: str


class PermissionOut(BaseModel):
    code: str
    description: str
    category: str

    model_config = {"from_attributes": True}


class RoleOut(BaseModel):
    id: str
    name: str
    description: str | None
    is_system: bool
    permission_codes: list[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=2000)


class RoleUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=2000)


class RolePermissionsUpdate(BaseModel):
    permission_codes: list[str] = Field(default_factory=list, max_length=100)


class UserRoleAssignmentOut(BaseModel):
    id: str
    user_id: str
    role_id: str
    role_name: str
    created_at: datetime

    model_config = {"from_attributes": True}


class UserRoleAssignmentCreate(BaseModel):
    user_id: str = Field(min_length=1, max_length=160)
    role_id: str


class VectorReconcileOut(BaseModel):
    expected: int
    existing: int
    added: int
    deleted: int


class FailureCaseOut(BaseModel):
    id: str
    trace_id: str
    score: int
    comment: str | None
    question: str | None
    answer: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class RecommendationOut(BaseModel):
    document_id: str
    title: str
    department: str | None
    reason: str
    score: float
