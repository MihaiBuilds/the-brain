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
echo "  Starting scheduler daemon"
echo "============================================"

# The daemon is PID 1. It polls workflow_schedules every 10 seconds, fires
# due workflows sequentially, and writes a heartbeat row that the
# `brain daemon-status` healthcheck reads. SIGTERM (from `docker stop` or
# `docker compose down`) triggers a graceful shutdown — the current
# workflow finishes before the daemon exits.
#
# CLI commands continue to work alongside the daemon via
# `docker compose exec brain brain <command>` — separate processes against
# the same database, no handshake.
exec brain daemon
