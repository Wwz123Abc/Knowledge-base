import hashlib
import hmac
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from app.api.routes import router
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.logging_setup import configure_logging
from app.middleware import BodySizeLimitMiddleware, PlatformMiddleware


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.prepare_directories()
    init_db()
    if settings.model_ready and settings.vector_backend == "memory":
        from app.services import get_document_service

        with SessionLocal() as db:
            get_document_service().rebuild_memory_index(db)
    yield


settings = get_settings()
configure_logging()


def _api_docs_options(current) -> dict:
    # The interactive docs and openapi.json list every route and schema; there is no reason
    # to publish that on an internet-facing production host.
    if current.app_env.strip().lower() == "production":
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {}


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
    **_api_docs_options(settings),
)
app.add_middleware(PlatformMiddleware, settings=settings)
# Outermost: oversized bodies are refused before anything else touches them. Uploads get the
# configured file limit plus a little multipart overhead; every other body is small JSON.
app.add_middleware(
    BodySizeLimitMiddleware,
    multipart_limit=(settings.max_upload_mb + 1) * 1024 * 1024,
    default_limit=1024 * 1024,
)
if settings.allowed_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )
app.include_router(router, prefix=settings.api_prefix)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.middleware("http")
async def no_heuristic_cache(request, call_next):
    # Neither StaticFiles nor FileResponse below set Cache-Control, only
    # ETag/Last-Modified. Without an explicit directive browsers may apply
    # RFC 7234 heuristic caching and skip revalidation entirely, so a static
    # asset change (or this app's own deploy) can silently not reach open
    # tabs. Forcing revalidation keeps ETag support for bandwidth savings
    # while guaranteeing freshness.
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _asset_version() -> str:
    # Derived from the real files so a changed app.js/styles.css always gets a new URL — the
    # versioned copies (app-v33.js ...) this replaces had to be bumped by hand, and one deploy
    # shipped a fix to app.js while index.html still pointed at the stale copy.
    digest = hashlib.sha1()
    for name in ("app.js", "styles.css"):
        stat = (static_dir / name).stat()
        digest.update(f"{name}:{stat.st_mtime_ns}:{stat.st_size}".encode())
    return digest.hexdigest()[:10]


@app.get("/", include_in_schema=False)
def index():
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__ASSET_VERSION__", _asset_version()))


@app.get("/api/metrics", include_in_schema=False)
def metrics(request: Request):
    if settings.metrics_token:
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {settings.metrics_token}"):
            raise HTTPException(status_code=401, detail="需要有效的指标访问令牌")
    elif settings.app_env.strip().lower() == "production":
        raise HTTPException(status_code=404, detail="Not Found")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
