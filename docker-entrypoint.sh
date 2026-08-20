#!/usr/bin/env sh
# Wait for the database, apply migrations, then hand off to the real command.
#
# Migrations run here rather than at import time so that starting a second
# replica cannot race a half-applied schema: alembic takes a lock, and the app
# only begins serving once `upgrade head` has returned.
set -eu

echo "Waiting for the database..."
python - <<'PY'
import os, sys, time
import psycopg2
from urllib.parse import urlparse

from app.core.config import settings

url = urlparse(settings.sqlalchemy_database_uri.replace("postgresql+psycopg2", "postgresql"))
deadline = time.time() + float(os.environ.get("DB_WAIT_SECONDS", "60"))

while True:
    try:
        psycopg2.connect(
            dbname=url.path.lstrip("/"),
            user=url.username,
            password=url.password,
            host=url.hostname,
            port=url.port or 5432,
            connect_timeout=3,
        ).close()
        print("Database is up.")
        break
    except Exception as error:
        if time.time() > deadline:
            print(f"Database never became available: {error}", file=sys.stderr)
            sys.exit(1)
        time.sleep(1)
PY

echo "Applying migrations..."
alembic upgrade head

echo "Starting: $*"
exec "$@"
