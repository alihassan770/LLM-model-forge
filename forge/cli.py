"""Command line entry point: serve, migrate, and small operational helpers."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forge", description="Model Forge")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the web server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true",
                         help="restart automatically when a file changes")

    sub.add_parser("migrate", help="apply pending database migrations")
    sub.add_parser("status", help="show migration status")
    sub.add_parser("genkey", help="generate a FORGE_SECRET_KEY")

    p_worker = sub.add_parser("worker", help="run build jobs from the queue")
    p_worker.add_argument("--once", action="store_true",
                          help="drain the queue and exit")
    p_worker.add_argument("--poll", type=float, default=3.0,
                          help="seconds between polls when idle")

    p_admin = sub.add_parser("promote", help="grant platform superadmin to a user")
    p_admin.add_argument("email")

    p_reset = sub.add_parser(
        "reset-link", help="print a password reset link for an account")
    p_reset.add_argument("email")
    p_reset.add_argument("--base-url", default="http://127.0.0.1:8000",
                         help="the site's public URL, used to build the link")

    p_models = sub.add_parser(
        "check-models", help="compare the model registry with the providers' docs")

    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args.host, args.port, args.reload)
    if args.command == "migrate":
        return _migrate()
    if args.command == "status":
        return _status()
    if args.command == "worker":
        return _worker(args.once, args.poll)
    if args.command == "genkey":
        from .crypto import generate_key

        print(generate_key())
        return 0
    if args.command == "promote":
        return _promote(args.email)
    if args.command == "reset-link":
        return _reset_link(args.email, args.base_url)
    if args.command == "check-models":
        return _check_models()
    return 1


def _serve(host: str, port: int, reload: bool) -> int:
    import uvicorn

    uvicorn.run(
        "forge.web.app:app",
        host=host,
        port=port,
        reload=reload,
        reload_dirs=["forge"] if reload else None,
        log_level="info",
    )
    return 0


def _migrate() -> int:
    from . import db
    from .config import DATABASE_URL_NAMES, get_settings

    # Checked before the pool is touched, because the message a platform shows
    # you is whatever the process printed before it exited. This command runs
    # first in the container's start command, so when it fails the whole service
    # crash-loops - and "couldn't get a connection after 5.00 sec" does not tell
    # anyone which variable to go and set.
    if not get_settings().database_url:
        from .config import database_variables_seen

        seen = database_variables_seen()

        # Which service, which environment, and whether ANY of the variables
        # this app expects arrived. When a reference looks correct in the
        # dashboard and the container still sees nothing, the usual answer is
        # that the dashboard being edited is not the one running: another
        # service from the same repo, or another environment. The log has to
        # say who it is before the guidance can mean anything.
        import os

        service = os.environ.get("RAILWAY_SERVICE_NAME", "")
        environment = os.environ.get("RAILWAY_ENVIRONMENT_NAME", "")
        project = os.environ.get("RAILWAY_PROJECT_NAME", "")
        ours = sorted(n for n in os.environ if n.startswith("FORGE_"))
        if service or environment or project:
            print(
                f"\n  Running as service '{service or '?'}' in environment "
                f"'{environment or '?'}' of project '{project or '?'}'.\n"
                f"  FORGE_* variables this container received: "
                f"{', '.join(ours) if ours else 'NONE'}\n"
                f"  If that service name is not the one you edited, you have\n"
                f"  been setting variables on a different service. If the\n"
                f"  FORGE_* list is empty too, then none of the four\n"
                f"  variables reached this container at all, which means the\n"
                f"  wrong service or the wrong environment - not a bad\n"
                f"  reference.",
                file=sys.stderr,
            )

        print(
            "  No database connection string found, so there is nothing to\n"
            "  migrate. Looked for: " + ", ".join(DATABASE_URL_NAMES) + ",\n"
            "  and for PGHOST with PGUSER/PGPASSWORD/PGDATABASE/PGPORT.\n"
            "\n"
            "  Database-related variables this container can actually see:\n"
            "      " + (", ".join(seen) if seen else "NONE AT ALL") + "\n"
            "\n"
            + ("  PRESENT BUT EMPTY means the variable exists and its value\n"
               "  is blank, which is what a platform stores when a reference\n"
               "  like ${{ Postgres.DATABASE_URL }} does not resolve. The\n"
               "  reference is saved, the lookup failed. Either the database\n"
               "  service is not called Postgres, or it is not in this\n"
               "  environment. Paste the connection string literally and it\n"
               "  will work.\n"
               "\n" if any("EMPTY" in s for s in seen) else "")
            +
            "  On Railway a database is NOT shared with other services\n"
            "  automatically. Fix it on THIS service, not on the Postgres\n"
            "  service, in its own Variables tab. Either:\n"
            "\n"
            "  1. Reference it, which survives a password rotation:\n"
            "\n"
            "         DATABASE_URL=${{ Postgres.DATABASE_URL }}\n"
            "\n"
            "     replacing Postgres with your database service's exact name\n"
            "     as it appears in the project.\n"
            "\n"
            "  2. Or paste the value directly, which always works and is the\n"
            "     quicker thing to try when the reference above has not\n"
            "     taken: open the Postgres service -> Variables, reveal\n"
            "     DATABASE_URL, copy the whole postgresql://... string, and\n"
            "     set it literally here.\n"
            "\n"
            "  Both services must be in the SAME project, or the internal\n"
            "  hostname in that URL will not resolve.\n"
            "\n"
            "  Locally: copy .env.example to .env and fill it in.",
            file=sys.stderr,
        )
        return 1

    print("Applying migrations…", flush=True)
    try:
        db.migrate()
    except Exception as exc:  # noqa: BLE001
        print(f"  failed: {exc}", file=sys.stderr)
        # The common case on a platform is a database that exists but is not
        # reachable from this service, which looks identical to a database that
        # is down. Naming both saves a round trip.
        print(
            "\n  The database is configured but could not be reached. Check:\n"
            "    - the Postgres service is Online, not asleep or deploying\n"
            "    - DATABASE_URL is a reference to it, not a pasted URL from\n"
            "      another project, and not the public URL when both services\n"
            "      are in the same project\n"
            "    - both services are in the SAME Railway project, or the\n"
            "      private hostname will not resolve",
            file=sys.stderr,
        )
        return 1
    return 0


def _status() -> int:
    from . import db

    try:
        status = db.migration_status()
    except Exception as exc:  # noqa: BLE001
        print(f"Cannot reach the database: {exc}", file=sys.stderr)
        return 1
    for version in status["applied"]:
        print(f"  [applied] {version}")
    for version in status["pending"]:
        print(f"  [pending] {version}")
    if not status["applied"] and not status["pending"]:
        print("  no migrations found")
    return 0


def _worker(once: bool, poll: float) -> int:
    import os
    import socket
    import time

    from . import db
    from .jobs import runner

    blender = runner.find_blender()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    print(f"worker {worker_id}")
    print(f"  blender: {blender or 'NOT FOUND - builds will fail'}")

    from .config import get_settings

    if not get_settings().database_url:
        # No database means nothing to poll. Say so once and wait rather than
        # exiting: a container that exits gets restarted forever, and this
        # service having no work is not a failure worth a crash loop.
        print("  no database configured - nothing to poll. Waiting.")
        print("  set DATABASE_URL on this service if it is meant to build.")
        while not once:
            time.sleep(60)
        return 0

    idle = 0
    failures = 0
    while True:
        # A database that goes away must not kill the worker. It retries with
        # a widening gap, because a worker that exits is restarted by the
        # platform in a tighter loop than this one and fills the log doing it.
        try:
            # Reclaim anything a dead worker left behind before looking for
            # new work, or a crashed container strands its job forever.
            recovered = runner.requeue_stalled()
            if recovered:
                print(f"  requeued {recovered} stalled job(s)")

            job = runner.claim_next(worker_id)
            failures = 0
        except Exception as exc:  # noqa: BLE001
            failures += 1
            wait = min(60, 2 ** min(failures, 6))
            print(f"  database error ({type(exc).__name__}), retrying in "
                  f"{wait}s: {str(exc)[:120]}")
            if once:
                return 1
            time.sleep(wait)
            continue
        if job is None:
            if once:
                print("  queue empty")
                return 0
            idle += 1
            if idle % 20 == 1:
                print("  idle")
            time.sleep(poll)
            continue

        idle = 0
        spec = db.fetch_one("SELECT * FROM specs WHERE id = %s", (job["spec_id"],))
        item = db.fetch_one("SELECT name FROM items WHERE id = %s",
                            (job["item_id"],))
        if spec is None:
            db.execute(
                "UPDATE jobs SET state='failed', error=%s, finished_at=now() "
                "WHERE id=%s",
                ("The specification for this job no longer exists.", job["id"]),
            )
            continue

        print(f"  building job {job['id']} (spec rev {spec['revision']})")
        result = runner.run_job(str(job["id"]), spec["body"], spec["revision"],
                                (item or {}).get("name", "model"))
        db.execute(
            "UPDATE items SET status=%s, updated_at=now() WHERE id=%s",
            ("complete" if result["ok"] else "failed", job["item_id"]),
        )
        print(f"    {'done' if result['ok'] else 'failed'}: "
              f"{result.get('error') or str(result.get('formats'))}")


def _promote(email: str) -> int:
    from . import db

    row = db.fetch_one(
        "UPDATE users SET is_superadmin = true WHERE lower(email) = lower(%s) "
        "RETURNING email",
        (email,),
    )
    if row is None:
        print(f"No user with email {email}", file=sys.stderr)
        return 1
    print(f"{row['email']} is now a platform superadmin")
    return 0


def _reset_link(email: str, base_url: str) -> int:
    """Mint a reset link and print it.

    For the case the web flow cannot cover: the only admin has forgotten the
    password and email is not configured, so there is nobody to send the link
    and nobody who can log in to generate one. Running this needs shell access
    to the server, which is a person who could change the password in the
    database anyway - so it grants nothing that was not already available, and
    it does it without hand-editing a password hash.
    """
    from .auth import reset, users

    user = users.get_user_by_email(users.normalise_email(email))
    if user is None:
        print(f"No account for {email}.")
        return 1

    token = reset.create_for_user(str(user["id"]))
    print(f"\nReset link for {user['email']} - valid once, for "
          f"{reset.TTL_HOURS} hours:\n")
    print(f"  {base_url.rstrip('/')}/reset/{token}\n")
    print("Anyone holding this link can set the password. Do not paste it "
          "anywhere it will be kept.")
    return 0


def _check_models() -> int:
    """Report where the model registry disagrees with the providers' docs.

    The registry is a hand-written copy of published numbers, and published
    numbers change: a model is added, a price is cut, a default flips. This
    prints what to go and read rather than trying to parse the pages itself - a scraper that silently mis-parses a price is worse than no scraper.
    """
    from .ai import openrouter
    from .ai.providers import CHECKED, PROVIDERS

    print(f"Registry last checked against the docs: {CHECKED}\n")
    for prov in PROVIDERS.values():
        print(f"{prov.label} - {prov.docs_url}")

        if prov.dynamic:
            # Nothing to drift against: this list is fetched, not copied. The
            # useful check here is whether the fetch works at all and what it
            # currently offers.
            models = openrouter.catalog(force=True)
            problem = openrouter.status()["error"]
            if problem:
                print(f"    could not fetch the catalogue: {problem}")
                print()
                continue
            vision = [m for m in models if m.vision]
            print(f"    fetched live: {len(models)} models, "
                  f"{len(vision)} of them vision-capable")
            print(f"    {sum(1 for m in vision if m.structured)} enforce a "
                  f"JSON schema; {sum(1 for m in vision if m.reasoning)} "
                  f"can think before answering")
            print("    cheapest vision models:")
            for m in sorted(vision, key=lambda m: m.price_in)[:5]:
                print(f"      {m.id:44} ${m.price_in:.2f}/${m.price_out:.2f} per 1M")
            print()
            continue

        for m in prov.models:
            bits = [f"{m.context // 1000}k ctx",
                    f"{m.max_output // 1000}k out",
                    f"${m.price_in}/${m.price_out} per 1M"]
            if not m.vision:
                bits.append("text only")
            elif m.image_tokens:
                bits.append(f"vision @{m.image_tokens} tok")
            else:
                bits.append("vision, detail budget unpublished")
            if m.reasoning:
                bits.append("thinking: " + "/".join(m.reasoning_efforts)
                            + f" (default {m.default_effort})")
            print(f"    {m.id:20} {'  ·  '.join(bits)}")
        print()
    print("Open each URL above and compare. What to look for:")
    print("  - models added or withdrawn")
    print("  - price changes (ours are the peak/cache-miss rates on purpose)")
    print("  - a change to the default thinking mode or its levels")
    print("  - a lower max output than we ask for")
    print("\nEdit forge/ai/providers.py and move CHECKED to today's date.")
    return 0
