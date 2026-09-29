#!/bin/sh
# Container entrypoint. Decides whether this container is the web server or a
# build worker, then never exits on a configuration problem.
#
# Both roles come from one image and one start command, because a platform
# deploys a repo and every service from that repo inherits the same
# railway.json. A second service therefore ran a second web server by default:
# no domain, no reason to exist, crash-looping against the same database and
# printing an identical failure to the real web service's. Two services
# producing the same log is how an afternoon disappears.
#
# Role is taken from FORGE_ROLE if set, otherwise from the service's own name.
set -e

ROLE="${FORGE_ROLE:-}"
if [ -z "$ROLE" ]; then
    case "$(echo "${RAILWAY_SERVICE_NAME:-web}" | tr 'A-Z' 'a-z')" in
        *worker*|*build*) ROLE="worker" ;;
        *)                ROLE="web" ;;
    esac
fi

echo "==> forge: role=${ROLE} service=${RAILWAY_SERVICE_NAME:-unknown} env=${RAILWAY_ENVIRONMENT_NAME:-unknown}"

# Only the web role migrates. Two services racing the same migrations is
# pointless even though the advisory lock makes it safe.
if [ "$ROLE" = "web" ]; then
    echo "==> forge: applying migrations"
    if python run.py migrate; then
        echo "==> forge: migrations applied"
    else
        echo "==> forge: MIGRATIONS DID NOT RUN (see above). Starting the"
        echo "==> forge: server anyway so the problem is reachable: /healthz"
        echo "==> forge: will report status=degraded and every page will say"
        echo "==> forge: the database is unreachable."
    fi
    echo "==> forge: starting the web server on ${PORT:-8000}"
    exec python run.py serve --host 0.0.0.0 --port "${PORT:-8000}"
fi

# The worker polls for build jobs. Today nothing enqueues any - builds run
# inside the web request - so this idles. It stays up rather than exiting,
# because a container that exits is a container the platform restarts forever,
# and a worker with nothing to do is not an error.
echo "==> forge: starting the build worker (idle until builds are queued)"
exec python run.py worker
