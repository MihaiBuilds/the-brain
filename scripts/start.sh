#!/bin/bash
set -e

echo "============================================"
echo "  The Brain — starting up"
echo "============================================"

# Wait for Postgres
echo "Waiting for database..."
for i in $(seq 1 30); do
    if python -c "
import socket, sys
s = socket.socket()
s.settimeout(2)
try:
    s.connect(('${DB_HOST:-localhost}', ${DB_PORT:-5432}))
    s.close()
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        echo "Database is ready."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "ERROR: Database not reachable after 30 seconds."
        exit 1
    fi
    sleep 1
done

# Run migrations
echo "Running migrations..."
python -m src.cli migrate

# Show status
echo ""
python -m src.cli status

echo ""
echo "============================================"
echo "  The Brain is ready"
echo "============================================"

# M1 has no long-running service yet — the container's job is to apply
# migrations and expose the `brain` CLI. Keep it alive so `docker compose
# exec brain ...` works for running workflows. The runner service lands in M2.
exec tail -f /dev/null
