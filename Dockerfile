# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app
COPY data ./data
COPY scripts ./scripts

RUN chmod +x /app/scripts/docker-entrypoint.sh \
    && mkdir -p /app/data/input /app/data/output \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["api"]
