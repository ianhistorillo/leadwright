#!/bin/sh
set -eu

role="${1:-api}"

echo "Starting container role=${role}"

# Apply migrations before API/worker start (safe for concurrent alembic in most cases;
# compose starts one api first via depends_on + healthchecks).
if [ "${SKIP_DB_MIGRATIONS:-false}" != "true" ]; then
  echo "Running alembic upgrade head..."
  alembic upgrade head
fi

case "${role}" in
  api)
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  worker)
    exec celery -A app.workers.celery_app.celery_app worker --loglevel="${CELERY_LOGLEVEL:-INFO}"
    ;;
  beat)
    exec celery -A app.workers.celery_app.celery_app beat --loglevel="${CELERY_LOGLEVEL:-INFO}"
    ;;
  migrate)
    echo "Migrations complete."
    ;;
  *)
    echo "Unknown role: ${role}" >&2
    echo "Supported roles: api | worker | beat | migrate" >&2
    exit 1
    ;;
esac
