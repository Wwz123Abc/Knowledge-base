import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=False,
        extra="ignore",
    )

    app_env: str = "development"
    app_name: str = "企业知识库问答"
    api_prefix: str = "/api"
    database_url: str = "sqlite:///./data/app.db"
    # Every in-flight request holds one pooled connection while it queries; SQLAlchemy's
    # default (5 + 10 overflow) capped the whole service at 15 concurrent requests.
    db_pool_size: int = Field(default=20, ge=1, le=200)
    db_max_overflow: int = Field(default=20, ge=0, le=200)
    db_pool_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    vector_backend: str = "memory"
    postgres_vector_url: str = "postgresql+psycopg://rag:rag_password@localhost:5432/rag"
    vector_collection: str = "enterprise_knowledge"

    openai_api_key: str = ""
    openai_base_url: str = ""
    chat_model: str = "gpt-4.1-mini"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_provider: str = "openai"
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_cache_dir: Path = Field(default=Path("./data/model_cache"))

    chunk_size: int = 700
    chunk_overlap: int = 100
    retrieval_k: int = 6
    retrieval_fetch_k: int = 24
    max_chunks_per_document: int = 2
    max_upload_mb: int = 30
    upload_dir: Path = Field(default=Path("./data/uploads"))

    auth_mode: str = "dev"
    dev_user_id: str = "local-admin"
    dev_user_name: str = "本地管理员"
    dev_tenant_id: str = "default"
    dev_groups: str = "admins,hr,finance"
    dev_roles: str = "admin,user"
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    oidc_tenant_claim: str = "tenant_id"
    oidc_groups_claim: str = "groups"
    oidc_roles_claim: str = "roles"

    # 企业微信自建应用登录（AUTH_MODE=wecom）。企业微信不签发标准 OIDC JWT，登录流程是：
    # 跳转到企业微信授权页 -> 企业微信回调一个 code -> 后端用 code 换 userid/部门 -> 后端自己
    # 签发一个 HS256 会话令牌（wecom_session_secret 签名），前端拿这个令牌当普通 Bearer token
    # 用，走的还是 get_auth_context 里同一套 AuthContext 结构。
    wecom_corp_id: str = ""
    wecom_agent_id: str = ""
    wecom_secret: str = ""
    wecom_redirect_uri: str = ""
    wecom_tenant_id: str = "default"
    wecom_admin_userids: str = ""
    wecom_super_admin_userids: str = ""
    wecom_session_secret: str = ""
    # Require the callback's `state` to match the cookie set when login started. Turn off
    # only as an emergency switch if some embedded browser is found to drop cookies.
    wecom_require_state_cookie: bool = True
    wecom_session_ttl_minutes: int = Field(default=480, gt=0, le=10_080)

    task_backend: str = "inprocess"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    enable_query_rewrite: bool = True
    hybrid_vector_weight: float = 0.65
    hybrid_lexical_weight: float = 0.35
    rerank_k: int = 18
    model_timeout_seconds: float = 45.0
    model_max_retries: int = 2

    clamav_host: str = ""
    clamav_port: int = 3310
    max_archive_entries: int = 5000
    max_archive_uncompressed_mb: int = 200
    redact_audit_pii: bool = True
    rate_limit_per_minute: int = 120
    # /api/metrics is closed in production unless a token is set; Prometheus then scrapes
    # it with `Authorization: Bearer <token>`.
    metrics_token: str = ""
    trusted_proxy_ips: str = ""
    allowed_origins: str = ""
    reranker_provider: str = "token_overlap"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_base_url: str = ""
    reranker_api_key: str = ""
    reranker_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    reranker_max_chars: int = Field(default=256, ge=64, le=8000)
    cache_url: str = ""
    # The cache key already includes a per-tenant epoch that a document
    # delete/ACL/version change bumps immediately, so a longer TTL only widens the
    # window for the "nothing changed" case — it can't serve stale results past an
    # actual mutation.
    retrieval_cache_ttl_seconds: int = 600
    retrieval_cache_version: str = "2"
    ocr_enabled: bool = False
    ocr_language: str = "chi_sim+eng"
    connector_allowed_roots: str = "./connector_data"
    # A sync that would delete more than this share of what it previously synced is
    # treated as a missing/unmounted source, not as real deletions, and skips the deletes.
    connector_max_delete_ratio: float = Field(default=0.5, ge=0, le=1)
    tool_database_url: str = ""
    tool_allowed_tables: str = ""
    tool_http_allowed_domains: str = ""
    tool_http_require_https: bool = True
    tool_max_rows: int = 200
    tool_sql_timeout_ms: int = Field(default=10_000, ge=1, le=300_000)
    chat_fallback_models: str = ""
    sensitive_chat_model: str = ""
    unanswerable_fallback_enabled: bool = True
    unanswerable_fallback_prefix: str = "（以下内容为 AI 通用回答，不属于企业官方文档，仅供参考）"
    max_image_megapixels: int = 50
    model_pricing_json: str = "{}"
    health_dependency_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    health_check_model: bool = False
    corrective_min_rerank_score: float = Field(default=0.05, ge=0, le=1)

    @property
    def allowed_origin_list(self) -> list[str]:
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]

    @property
    def connector_root_list(self) -> list[Path]:
        return [
            Path(item.strip()).resolve()
            for item in self.connector_allowed_roots.split(",")
            if item.strip()
        ]

    @property
    def tool_allowed_table_list(self) -> list[str]:
        return [item.strip() for item in self.tool_allowed_tables.split(",") if item.strip()]

    @property
    def tool_http_domain_list(self) -> list[str]:
        return [
            item.strip().lower()
            for item in self.tool_http_allowed_domains.split(",")
            if item.strip()
        ]

    @property
    def trusted_proxy_ip_list(self) -> list[str]:
        return [item.strip() for item in self.trusted_proxy_ips.split(",") if item.strip()]

    @property
    def chat_fallback_model_list(self) -> list[str]:
        return [item.strip() for item in self.chat_fallback_models.split(",") if item.strip()]

    @property
    def model_pricing(self) -> dict[str, dict[str, float]]:
        try:
            value = json.loads(self.model_pricing_json)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}

    @model_validator(mode="after")
    def refuse_dev_auth_in_production(self) -> "Settings":
        # AUTH_MODE=dev hands every anonymous caller the DEV_ROLES identity (admin by
        # default). Forgetting to set AUTH_MODE on a production host must fail loudly at
        # startup instead of silently serving the whole system without a login.
        if self.app_env.strip().lower() == "production" and self.auth_mode == "dev":
            raise ValueError(
                "APP_ENV=production 时不允许 AUTH_MODE=dev（等于所有人匿名管理员），"
                "请改用 AUTH_MODE=wecom 或 oidc"
            )
        return self

    @field_validator("vector_backend")
    @classmethod
    def validate_vector_backend(cls, value: str) -> str:
        value = value.lower()
        if value not in {"memory", "pgvector"}:
            raise ValueError("VECTOR_BACKEND 只能是 memory 或 pgvector")
        return value

    @field_validator("auth_mode")
    @classmethod
    def validate_auth_mode(cls, value: str) -> str:
        value = value.lower()
        if value not in {"dev", "oidc", "wecom"}:
            raise ValueError("AUTH_MODE 只能是 dev、oidc 或 wecom")
        return value

    @field_validator("embedding_provider")
    @classmethod
    def validate_embedding_provider(cls, value: str) -> str:
        value = value.lower()
        if value not in {"openai", "fastembed", "local_hash"}:
            raise ValueError("EMBEDDING_PROVIDER must be openai, fastembed or local_hash")
        return value

    @field_validator("task_backend")
    @classmethod
    def validate_task_backend(cls, value: str) -> str:
        value = value.lower()
        if value not in {"inprocess", "celery"}:
            raise ValueError("TASK_BACKEND 只能是 inprocess 或 celery")
        return value

    @field_validator("reranker_provider")
    @classmethod
    def validate_reranker_provider(cls, value: str) -> str:
        value = value.lower()
        if value not in {"token_overlap", "cross_encoder_api"}:
            raise ValueError("RERANKER_PROVIDER must be token_overlap or cross_encoder_api")
        return value

    @property
    def dev_group_list(self) -> list[str]:
        return [item.strip() for item in self.dev_groups.split(",") if item.strip()]

    @property
    def dev_role_list(self) -> list[str]:
        return [item.strip() for item in self.dev_roles.split(",") if item.strip()]

    @property
    def wecom_admin_userid_list(self) -> list[str]:
        return [item.strip() for item in self.wecom_admin_userids.split(",") if item.strip()]

    @property
    def wecom_super_admin_userid_list(self) -> list[str]:
        return [item.strip() for item in self.wecom_super_admin_userids.split(",") if item.strip()]

    @property
    def model_ready(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def embeddings_ready(self) -> bool:
        return self.embedding_provider in {"fastembed", "local_hash"} or bool(
            self.embedding_api_key or self.openai_api_key
        )

    @property
    def embedding_model_version(self) -> str:
        return f"{self.embedding_provider}:{self.embedding_model}:{self.embedding_dimensions}"

    def prepare_directories(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.embedding_cache_dir.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite"):
            Path("./data").mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
