#!/bin/sh
set -e

# Apply database migrations on container start unless explicitly skipped
if [ "$RUN_MIGRATIONS" != "false" ]; then
    echo "[entrypoint] Applying database migrations (alembic upgrade head)..."
    alembic upgrade head
    echo "[entrypoint] Database migrations applied successfully."
fi

# Hand over to CMD (Uvicorn)
exec "$@"
