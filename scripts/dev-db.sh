#!/usr/bin/env bash
# Local development database.
#
# This runs a PostgreSQL cluster owned by your own user, on port 5433, entirely
# inside your home directory. It deliberately does NOT touch the system-wide
# PostgreSQL on 5432: no sudo is needed, nothing system-wide is modified, and
# deleting the data directory removes every trace of it.
#
# Production is unaffected by any of this. Railway provides its own Postgres and
# injects DATABASE_URL; the application only ever reads that one variable, so
# local and deployed differ by a connection string and nothing else.
#
#   ./scripts/dev-db.sh start     start the cluster (run this after a reboot)
#   ./scripts/dev-db.sh stop      stop it
#   ./scripts/dev-db.sh status    is it running?
#   ./scripts/dev-db.sh psql      open a shell on the database
#   ./scripts/dev-db.sh reset     DESTROY all data and recreate empty tables
#   ./scripts/dev-db.sh destroy   remove the cluster entirely

set -euo pipefail

PGDATA="${FORGE_PGDATA:-$HOME/.local/share/model-forge/pgdata}"
PGPORT=5433
PGUSER_APP=forge
PGPASS_APP=forge_dev
PGDB=forge_dev

# Pick the newest installed PostgreSQL rather than hardcoding a version.
BIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)"
if [ -z "$BIN" ]; then
  echo "No PostgreSQL installation found under /usr/lib/postgresql." >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

running() {
  "$BIN/pg_isready" -h 127.0.0.1 -p "$PGPORT" -q 2>/dev/null
}

init_cluster() {
  if [ -d "$PGDATA/base" ]; then return 0; fi
  echo "Creating cluster at $PGDATA"
  mkdir -p "$(dirname "$PGDATA")"
  # trust auth is acceptable because the cluster listens on loopback only and is
  # owned by this user. It is never how the deployed database is configured.
  "$BIN/initdb" -D "$PGDATA" -U "$USER" \
    --auth-local=trust --auth-host=trust -E UTF8 >/dev/null
}

start() {
  init_cluster
  if running; then echo "Already running on port $PGPORT."; else
    "$BIN/pg_ctl" -D "$PGDATA" -l "$PGDATA/server.log" \
      -o "-p $PGPORT -k /tmp -c listen_addresses=127.0.0.1" start >/dev/null
    sleep 1
  fi

  PGHOST=127.0.0.1 PGPORT=$PGPORT "$BIN/psql" -d postgres -qtA <<SQL >/dev/null
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$PGUSER_APP') THEN
    CREATE ROLE $PGUSER_APP LOGIN PASSWORD '$PGPASS_APP' CREATEDB;
  END IF;
END \$\$;
SQL

  if ! PGHOST=127.0.0.1 PGPORT=$PGPORT "$BIN/psql" -d postgres -qtA \
        -c "SELECT 1 FROM pg_database WHERE datname='$PGDB'" | grep -q 1; then
    PGHOST=127.0.0.1 PGPORT=$PGPORT "$BIN/createdb" -O "$PGUSER_APP" "$PGDB"
  fi

  echo "Database ready:"
  echo "  postgresql://$PGUSER_APP:$PGPASS_APP@127.0.0.1:$PGPORT/$PGDB"
}

case "${1:-start}" in
  start)  start ;;
  stop)   "$BIN/pg_ctl" -D "$PGDATA" stop >/dev/null && echo "Stopped." ;;
  status)
    if running; then echo "Running on port $PGPORT."; else echo "Not running."; fi ;;
  psql)
    shift
    PGPASSWORD="$PGPASS_APP" "$BIN/psql" -h 127.0.0.1 -p "$PGPORT" \
      -U "$PGUSER_APP" -d "$PGDB" "$@" ;;
  reset)
    read -rp "This destroys all local data in $PGDB. Type 'yes' to continue: " ok
    [ "$ok" = "yes" ] || { echo "Cancelled."; exit 1; }
    start >/dev/null
    PGHOST=127.0.0.1 PGPORT=$PGPORT "$BIN/dropdb" --if-exists "$PGDB"
    PGHOST=127.0.0.1 PGPORT=$PGPORT "$BIN/createdb" -O "$PGUSER_APP" "$PGDB"
    (cd "$REPO_ROOT" && .venv/bin/python run.py migrate)
    echo "Reset complete." ;;
  destroy)
    read -rp "This deletes the entire cluster at $PGDATA. Type 'yes': " ok
    [ "$ok" = "yes" ] || { echo "Cancelled."; exit 1; }
    running && "$BIN/pg_ctl" -D "$PGDATA" stop >/dev/null || true
    rm -rf "$PGDATA"
    echo "Removed." ;;
  *)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 1 ;;
esac
