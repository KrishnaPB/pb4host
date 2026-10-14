# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 10001 prospector
WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY prospector ./prospector
COPY migrations ./migrations
RUN pip install --upgrade pip && pip install ".[postgres]"
RUN mkdir -p /data && chown prospector:prospector /data
USER prospector
ENV DATA_DIR=/data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)" || exit 1
CMD ["prospector", "serve", "--host", "0.0.0.0", "--port", "8000"]
