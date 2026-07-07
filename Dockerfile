FROM python:3.12-slim

# uv for fast dependency installation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install Python dependencies as a separate layer so rebuilds are fast
# when only app code changes.
RUN uv pip install --system --no-cache \
    "httpx>=0.27" \
    "pydantic>=2.7" \
    "pydantic-settings>=2.3" \
    "pyyaml>=6.0" \
    "aiosqlite>=0.20" \
    "tenacity>=8.3" \
    "structlog>=24.4" \
    "fastapi>=0.111" \
    "uvicorn>=0.30"

COPY app/ ./app/

# searches.yaml and data/ are bind-mounted at runtime; the paths below
# must match the SEARCHES_FILE and DATA_DIR defaults in settings.py.
ENV SEARCHES_FILE=/app/searches.yaml
ENV DATA_DIR=/data

CMD ["python", "-m", "app.main"]
