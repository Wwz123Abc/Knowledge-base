from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from app.api.routes import router
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.logging_setup import configure_logging
from app.middleware import PlatformMiddleware


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
app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(PlatformMiddleware, settings=settings)
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


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(static_dir / "index.html")


@app.get("/api/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
