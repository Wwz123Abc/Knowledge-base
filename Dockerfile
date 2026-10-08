# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app
# DEBIAN_FRONTEND=noninteractive above: without it, tesseract's dependency chain can
# pull in tzdata, whose postinst script prompts for a timezone selection — with no TTY
# attached that blocks forever in select() instead of failing, hanging the build with
# no error message.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python - <<'PY'
import subprocess
import sys
import tomllib

with open("pyproject.toml", "rb") as project_file:
    dependencies = tomllib.load(project_file)["project"]["dependencies"]
subprocess.check_call(
    [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--default-timeout=120",
        "--retries",
        "10",
        *dependencies,
    ]
)
PY
COPY README.md ./
COPY app ./app
RUN pip install --disable-pip-version-check --no-deps .
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY data ./data
RUN groupadd --system rag && useradd --system --gid rag --home-dir /app rag \
    && mkdir -p /app/data/uploads /app/data/model_cache \
    && chown -R rag:rag /app/data

USER rag

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
