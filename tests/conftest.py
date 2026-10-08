"""Isolate the whole test session from the developer's .env and local database.

Without this, tests read the real .env (API keys, WeCom secrets, DEV_ROLES) and the real
data/app.db: a plain `with TestClient(app)` then runs the app's startup, which re-embeds
every chunk in the dev database (minutes of CPU and 8+ GB of RAM with the Jina model), and
a few tests only pass on a machine whose .env happens to contain an API key. Everything
below must be set before `app` (and therefore get_settings()/the SQLAlchemy engine) is
imported, which is why it lives at module level rather than in a fixture.
"""

import os
import tempfile
from pathlib import Path

_SESSION_DIR = Path(tempfile.mkdtemp(prefix="rag-tests-"))

os.environ.update(
    {
        "APP_ENV": "development",
        "DATABASE_URL": f"sqlite:///{(_SESSION_DIR / 'app.db').as_posix()}",
        "UPLOAD_DIR": str(_SESSION_DIR / "uploads"),
        "EMBEDDING_CACHE_DIR": str(_SESSION_DIR / "model_cache"),
        "VECTOR_BACKEND": "memory",
        "EMBEDDING_PROVIDER": "local_hash",
        "AUTH_MODE": "dev",
        "DEV_USER_ID": "local-admin",
        "DEV_ROLES": "admin,user",
        "DEV_GROUPS": "admins,hr,finance",
        "TASK_BACKEND": "inprocess",
        "CACHE_URL": "",
        # A syntactically valid but fake key, pointed at a closed local port: the code paths
        # that check "is a model configured" run, and any accidental real LLM call fails
        # immediately instead of reaching the network.
        "OPENAI_API_KEY": "test-key-not-real",
        "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
        "MODEL_MAX_RETRIES": "0",
        "MODEL_TIMEOUT_SECONDS": "3",
        "RERANKER_PROVIDER": "token_overlap",
        "OCR_ENABLED": "false",
        "CLAMAV_HOST": "",
        "TRUSTED_PROXY_IPS": "",
        "ALLOWED_ORIGINS": "",
        "WECOM_CORP_ID": "",
        "WECOM_AGENT_ID": "",
        "WECOM_SECRET": "",
        "WECOM_REDIRECT_URI": "",
        "WECOM_SESSION_SECRET": "",
        "WECOM_ADMIN_USERIDS": "",
        "WECOM_SUPER_ADMIN_USERIDS": "",
        "TOOL_DATABASE_URL": "",
        "TOOL_ALLOWED_TABLES": "",
        "TOOL_HTTP_ALLOWED_DOMAINS": "",
    }
)

from app.config import Settings  # noqa: E402

Settings.model_config["env_file"] = None
