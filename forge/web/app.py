"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db
from ..config import get_settings
from .deps import NotAuthenticated, redirect_to_login

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _asset(path: str) -> str:
    """A static URL stamped with the file's modification time.

    StaticFiles sends an ETag but no Cache-Control, which leaves the browser
    free to reuse a stylesheet without asking. It does: a CSS change looked
    like a CSS change that had not taken effect, and there is no way to tell
    those two apart from the outside. The stamp makes an edited file a new URL.
    """
    file = STATIC_DIR / path
    try:
        return f"/static/{path}?v={int(file.stat().st_mtime)}"
    except OSError:
        return f"/static/{path}"


templates.env.globals["asset"] = _asset


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.database_url:
        try:
            db.get_pool()
            status = db.migration_status()
            if status["pending"]:
                log.warning(
                    "database has %d pending migration(s): %s - run: forge migrate",
                    len(status["pending"]),
                    ", ".join(status["pending"]),
                )
        except Exception:
            log.exception("could not connect to the database at startup")
    else:
        log.warning("DATABASE_URL is not set - the app will run but nothing will work")

    # Fetch OpenRouter's catalogue in the background, so the first extraction
    # after a deploy resolves its model against the real list rather than an
    # invented one. In a thread because start-up must not wait on somebody
    # else's website, and `ensure()` never raises - a failure here costs the
    # first extraction one fetch of its own, nothing more.
    import threading
    from ..ai import openrouter
    threading.Thread(target=openrouter.ensure, name="openrouter-catalogue",
                     daemon=True).start()

    # Said at start-up because the failure mode is otherwise invisible until it
    # is too late: on a platform with an ephemeral filesystem, every upload and
    # every built model is deleted by the next deploy. The app keeps working,
    # the database still lists the files, and the downloads 404. Nothing in the
    # interface can tell you that is coming, so this line has to.
    from .. import mail
    from ..storage.backend import get_storage, storage_summary

    store = storage_summary()
    if store["backend"] == "local":
        log.warning(
            "storage is LOCAL (%s). Fine for development. On Railway or any "
            "container platform the filesystem is wiped on every deploy, so "
            "set FORGE_S3_BUCKET, or attach a volume and point "
            "FORGE_LOCAL_STORAGE at it.",
            store["detail"],
        )
    else:
        # A write and a delete, once, at start-up. A wrong bucket name or a
        # key without PutObject is otherwise discovered by the first person to
        # upload something, as a 500 with the reason buried in a traceback.
        problem = get_storage().check()
        if problem:
            log.error("storage is S3 (%s) but NOT USABLE: %s",
                      store["detail"], problem)
        else:
            log.info("storage is S3 (%s), write and delete confirmed",
                     store["detail"])
    # Checked here because the failure is otherwise silent and remote: both
    # places that use Pillow catch ImportError and carry on, so a container
    # built without it serves 404s for every question crop and writes nothing
    # to the log. That shipped once. One import at start-up is cheaper than
    # finding out from a screenshot.
    try:
        import PIL  # noqa: F401, PLC0415
    except ImportError:
        log.error(
            "Pillow is NOT installed. Question crops will not render and PDF "
            "pages will be sent to the model at full size. It is a declared "
            "dependency, so this image was built from a stale requirement "
            "set - rebuild without the layer cache."
        )

    if not settings.secure_cookies:
        log.warning(
            "FORGE_SECURE_COOKIES is off. Behind HTTPS the browser will drop "
            "the session cookie and sign-in will appear to fail for no reason."
        )
    if not mail.configured():
        log.info(
            "email is not configured - password reset links will be written "
            "to this log instead of being sent (FORGE_SMTP_HOST, "
            "FORGE_MAIL_FROM)."
        )

    yield
    db.close_pool()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Model Forge",
        docs_url="/api/docs" if settings.debug else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from .routes import account as account_routes
    from .routes import admin as admin_routes
    from .routes import auth as auth_routes
    from .routes import dashboard as dashboard_routes
    from .routes import items as items_routes
    from .routes import members as members_routes
    from .routes import projects as projects_routes
    from .routes import settings as settings_routes

    app.include_router(auth_routes.router)
    app.include_router(dashboard_routes.router)
    app.include_router(members_routes.router)
    app.include_router(projects_routes.router)
    app.include_router(items_routes.router)
    app.include_router(account_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(admin_routes.router)

    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, exc: NotAuthenticated):
        return redirect_to_login(request)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        """Is this container serving, and is its database reachable.

        Answers 200 whenever the process can serve a request, and reports the
        database in the body rather than in the status code.

        That is a deliberate change from returning 503. A health check answers
        "should traffic come here", and on a single replica with a
        misconfigured database the answer is still yes: there is nowhere to
        fail over to, so a platform that kills the container achieves nothing
        except denying the operator the error page that says what is wrong.
        Here it meant a service that started, failed its check, got restarted,
        and never stayed up long enough for anyone to read the diagnostic.

        The fault is not hidden. `status` says degraded, `database` carries the
        error, every page returns 503 with an explanation, and the start-up log
        says the migration did not run. Restore the 503 here once there is more
        than one replica and a failover worth having.
        """
        checks = {"app": "ok"}
        degraded = False
        try:
            db.fetch_one("SELECT 1 AS ok")
            checks["database"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["database"] = f"error: {type(exc).__name__}: {exc}"[:200]
            degraded = True
        return JSONResponse(
            {"status": "degraded" if degraded else "ok", **checks},
            status_code=200,
        )

    @app.exception_handler(Exception)
    async def _database_down(request: Request, exc: Exception):
        """Turn a database outage into a page that says so.

        Every request that touches the database raises from deep inside psycopg
        when there is no database, and the default response is a bare
        "Internal Server Error" with no clue whether the fault is the app, the
        network or a variable nobody set. On a first deploy that is nearly
        always a missing DATABASE_URL reference, and the person reading the
        screen is the person who can fix it.

        Only database errors are reclassified. Anything else keeps the generic
        500, because inventing a cause is worse than admitting there is a bug.
        """
        from psycopg import DataError, OperationalError
        from psycopg_pool import PoolTimeout

        # A malformed identifier cannot name a row that exists, so it is a
        # 404, not a server error. Postgres rejects it at parse time -
        # "invalid input syntax for type uuid" - which arrives here as a
        # DataError, and every route that takes an id in a path or a form was
        # answering 500 to a typo. /items/not-a-uuid is a wrong address, not a
        # broken application.
        if isinstance(exc, DataError):
            log.info("malformed identifier in %s: %s", request.url.path, exc)
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "Not found"}, status_code=404)
            return HTMLResponse(
                templates.get_template("error.html").render(
                    request=request, code=404,
                    message="That address does not name anything here. The "
                            "link may be incomplete or mistyped.",
                ),
                status_code=404,
            )

        # Connection-level failures only. `psycopg.Error` was too broad: a
        # check-constraint violation is a psycopg.Error too, and reporting a
        # bug in a query as "the database is unreachable" sends whoever reads
        # it looking for a network problem. It masked exactly that once - a
        # signup inserting a role the constraint forbade.
        unreachable = isinstance(exc, (OperationalError, PoolTimeout)) or (
            isinstance(exc, RuntimeError) and "database" in str(exc).lower()
        )
        if not unreachable:
            raise exc

        log.error("database unreachable while serving %s: %s",
                  request.url.path, exc)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "The database is unreachable."},
                                status_code=503)
        return HTMLResponse(
            templates.get_template("error.html").render(
                request=request, code=503,
                message="The database is unreachable, so nothing can be read "
                        "or saved. If this is a new deployment, the most "
                        "likely cause is that DATABASE_URL has not been set "
                        "on this service. The server log names what is "
                        "missing.",
            ),
            status_code=503,
        )

    @app.exception_handler(404)
    async def _not_found(request: Request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return HTMLResponse(
            templates.get_template("error.html").render(
                request=request, code=404, message="Page not found."
            ),
            status_code=404,
        )

    return app


app = create_app()
