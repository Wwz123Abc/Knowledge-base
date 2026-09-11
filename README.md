# 企业知识库问答

生产化的 LangChain + LangGraph 企业 RAG 工程。支持多租户、多组 ACL、多知识库、异步入库、混合检索、纠错检索、来源引用、企业微信登录/企业 SSO、审计评测和受控业务工具。

日常启动、关闭、重启和故障排查请参阅 [个人测试使用说明书](docs/使用说明书.md)。

## 核心能力

- PDF、DOCX、PPTX、图片、Markdown、TXT 入库
- OCR、表格和 PPT 图表转为可检索文本
- Celery/Redis 异步解析，支持进度、重试、取消和版本更新
- PostgreSQL/pgvector 向量检索 + 数据库原生词法检索（PostgreSQL `pg_trgm` / SQLite FTS5）+ RRF + 可替换重排器
- 企业微信登录 / OIDC/JWT 身份认证、租户隔离和多访问组检索前过滤
- 多知识库自动路由、指定库检索和跨库检索
- LangGraph 检索质量判断、自动改写和纠错重试
- 流式回答、显式取消、来源引用和无答案拒答
- 提示注入、PII 脱敏、病毒/文件结构检查、限流与熔断
- 受控只读 SQL/HTTP 工具和人工审批
- Prometheus 指标、JSON 日志、Token/费用追踪和 100 条评测集

## 本地启动

使用 Python 3.12：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

编辑 `.env`，设置模型服务：

```dotenv
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=
CHAT_MODEL=gpt-4.1-mini
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
EMBEDDING_PROVIDER=openai
EMBEDDING_API_KEY=
EMBEDDING_BASE_URL=
```

兼容 OpenAI 协议的私有模型可以通过 `OPENAI_BASE_URL` 接入。聊天与嵌入服务可以分开配置：`OPENAI_*` 用于聊天，`EMBEDDING_*` 用于向量化。聊天服务没有 embeddings 接口时，可以设置 `EMBEDDING_PROVIDER=fastembed` 使用本地 BGE；无模型网络环境可设置 `EMBEDDING_PROVIDER=local_hash` 作为开发后备。生产环境建议使用企业托管 BGE 或独立的 OpenAI 兼容嵌入服务。

DeepSeek 本地开发示例：

```dotenv
OPENAI_API_KEY=你的DeepSeek密钥
OPENAI_BASE_URL=https://api.deepseek.com
CHAT_MODEL=deepseek-v4-flash
EMBEDDING_PROVIDER=fastembed
EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
EMBEDDING_DIMENSIONS=512
```

Docker Compose 默认启动 Infinity CPU 重排服务并启用中文 Cross-Encoder；服务不可用时会自动回退到 `token_overlap`：

```dotenv
RERANKER_PROVIDER=cross_encoder_api
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
RERANKER_BASE_URL=http://reranker:7997
RERANKER_API_KEY=
RERANKER_TIMEOUT_SECONDS=60
RERANKER_MAX_CHARS=256
```

启动：

```powershell
uvicorn app.main:app --reload
```

- 页面：<http://localhost:8000>
- API 文档：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/api/health>
- Prometheus：<http://localhost:8000/api/metrics>

开发模式使用固定管理员身份、SQLite 和内存向量索引。可以上传 `data/sample_handbook.md` 和 `data/sample_finance_policy.md`，分别验证人力资源与财务问答。

健康检查会返回数据库、Redis、ClamAV 和模型服务状态。模型默认只报告是否配置；设置
`HEALTH_CHECK_MODEL=true` 后才会请求模型服务的 `/models` 接口。SQL 工具使用
`TOOL_SQL_TIMEOUT_MS` 限制单条查询时间，默认 10 秒。

## Docker 部署

```powershell
$env:OPENAI_API_KEY="你的密钥"
docker compose up --build -d
docker compose ps
```

容器模式使用 PostgreSQL/pgvector、Redis、ClamAV、API 和 Celery Worker，并在启动时执行 Alembic 迁移。服务设置了 `unless-stopped` 重启策略。Compose 中的默认密码只适用于本地联调，生产必须使用密钥管理系统和托管高可用数据库。

本机项目原目录包含中文字符，Docker BuildKit 不能稳定处理该构建路径。已经创建指向同一项目的英文目录联接，后续 Docker 操作请在这里执行：

```powershell
Set-Location C:\Users\w\Desktop\enterprise-rag-docker
docker compose up -d
```

这不是项目副本，两处路径中的文件和数据完全相同。

## 企业认证

生产支持两种模式，`AUTH_MODE` 二选一。

**企业微信登录**（当前云端生产实际使用）：

```dotenv
AUTH_MODE=wecom
WECOM_CORP_ID=企业ID
WECOM_AGENT_ID=自建应用AgentId
WECOM_SECRET=自建应用Secret
WECOM_REDIRECT_URI=https://你的域名/api/auth/wecom/callback
WECOM_SESSION_SECRET=至少32字节的随机字符串
```

**通用 OIDC**：

```dotenv
AUTH_MODE=oidc
OIDC_ISSUER=https://identity.example.com/
OIDC_AUDIENCE=enterprise-rag
OIDC_JWKS_URL=https://identity.example.com/.well-known/jwks.json
```

租户、访问组和角色从已验证令牌读取，问答请求不接受客户端自报权限。企业微信模式下访问组直接映射到企业微信部门（`dept_<部门ID>`）。详细说明见 [SSO / 企业微信接入](docs/单点登录与身份接入.md)。

## 文档与知识库

上传接口返回文档和后台任务 ID：

```powershell
curl.exe -X POST http://localhost:8000/api/documents `
  -F "file=@data/sample_handbook.md" `
  -F "department=人力资源部" `
  -F "access_groups=hr,managers"
```

`access_groups` 留空表示租户内全员可见。管理页面可以新建逻辑知识库并将文档分配到一个或多个知识库；问答时可以指定知识库，也可以让系统根据名称、描述和关键词自动路由。

扫描文件需要安装 Tesseract 并设置：

```dotenv
OCR_ENABLED=true
OCR_LANGUAGE=chi_sim+eng
```

## 评测

项目内置两套评测数据：`sample`（启动评测，100 条）与 `mechanical`（64 条机械/治具类，含 10 条应拒答；2026-08-25 基线见 `docs/优化与容量.md` 的 A1）。默认跑 sample，用 `--dataset` 选择：

```powershell
python -m evals.run_eval --output reports/evaluation.json
python -m evals.run_eval --dataset mechanical --output reports/mechanical_evaluation.json
```

报告包含 Recall@K、MRR、正确率、引用准确率、忠实度和无答案准确率。负反馈可导出为回归数据：

```powershell
python scripts/export_feedback.py
```

## 验证

```powershell
pytest -q
ruff format --check .
ruff check .
alembic upgrade head
```

当前结果（2026-09 复核）：115 项测试全部通过（隔离环境）、代码规范检查通过、Alembic 迁移链完整到 `0009_connector_sync_failed_count`、OpenAPI 35 个路径 / 44 个操作 / 41 个数据结构、Docker 完整栈健康。历史验收快照见 [本地验收报告](reports/ACCEPTANCE.md)，机械领域 Cross-Encoder 对比见 [重排评测报告](reports/RERANKER_EVALUATION_20260817.md)。

## 交付文档

- [使用说明书](docs/使用说明书.md)（含云端企业微信登录使用方式与本地开发附录）
- [完整任务状态](TASKS.md)
- [SSO / 企业微信接入](docs/单点登录与身份接入.md)
- [安全基线](docs/安全基线.md)
- [运维、备份与灰度发布](docs/运维手册.md)
- [评测说明](evals/README.md)

企业微信登录已在云端生产环境启用并投入使用；企业 OIDC（备用身份模式）、托管高可用基础设施、峰值压测、正式 RPO/RTO 和外部安全审核仍待在扩容到更大规模前补齐，具体项目见 `docs/优化与容量.md` 第四节「300 人规模容量保障」和任务清单末尾。
