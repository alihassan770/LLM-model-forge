"""Settings resolution.

One rule, applied everywhere: the environment wins. A container has no persistent
filesystem and no config file to read, so every setting has to be reachable through
an environment variable. Locally a `.env` file next to this repo fills the same role,
but it is only ever read when the variable is not already set, so an exported value
in the shell always beats the file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Populate os.environ from .env for values that are not already set.

    Deliberately not a dependency: the format we need is KEY=value with optional
    quotes and # comments, and pulling in a package for twenty lines of parsing
    is not a trade worth making.
    """
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


# Every variable name a Postgres connection string is published under, in the
# order they should be preferred. Railway's Postgres service exposes
# DATABASE_URL and, when public access is switched on, DATABASE_PUBLIC_URL -
# but it does not inject either into other services, so whichever one gets
# referenced is whichever one arrives. Heroku, Render, Fly and Supabase each
# use a name from this list too.
DATABASE_URL_NAMES = (
    "DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRESQL_URL",
    "DATABASE_PRIVATE_URL",
    "DATABASE_PUBLIC_URL",
)


# Names whose value was a URL with no host, recorded so the failure message
# can name them rather than describing a generic absence.
_BAD_URLS: list[str] = []


def _has_host(url: str) -> bool:
    from urllib.parse import urlsplit

    try:
        return bool(urlsplit(url).hostname)
    except ValueError:
        return False


def _database_url() -> str:
    """The connection string, from whichever variable carries it.

    Accepting several names is not indecision: a reference in a dashboard is
    typed by a person, and "it is set but the app says it is not" is a bad
    half hour. The parts form is accepted for the same reason - referencing
    PGHOST and its siblings is as reasonable a thing to do as referencing
    DATABASE_URL, and it silently did not work.
    """
    for name in DATABASE_URL_NAMES:
        value = (os.environ.get(name) or "").strip()
        if not value:
            continue
        # A URL assembled in a dashboard from references that did not resolve
        # comes out as "postgresql://:@:5432/" - not empty, so it passes a
        # truthiness test, and then fails deep in the driver with a message
        # about a host that was never named. Treat a URL with no host as not
        # configured, so the caller reports the variable instead.
        if _has_host(value):
            return value
        _BAD_URLS.append(name)

    # Assembled from the parts, under either spelling. A Postgres service
    # publishes its credentials as PGUSER/PGPASSWORD/PGDATABASE or as
    # POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB depending on which image it
    # is, and referencing the set it does not have yields empty strings with
    # no indication why. Accepting both removes a guess that cost a long
    # afternoon.
    def part(*names, default=""):
        for name in names:
            value = (os.environ.get(name) or "").strip()
            if value:
                return value
        return default

    host = part("PGHOST", "POSTGRES_HOST", "DB_HOST")
    if not host:
        return ""
    user = part("PGUSER", "POSTGRES_USER", "DB_USER", default="postgres")
    password = part("PGPASSWORD", "POSTGRES_PASSWORD", "DB_PASSWORD")
    database = part("PGDATABASE", "POSTGRES_DB", "DB_NAME", default=user)
    port = part("PGPORT", "POSTGRES_PORT", "DB_PORT", default="5432")
    # safe="" or quote leaves "/" alone, which is exactly the character that
    # breaks a URL when it turns up in a generated password.
    def esc(value: str) -> str:
        return quote(value, safe="")

    credentials = f"{esc(user)}:{esc(password)}@" if password else f"{esc(user)}@"
    return f"postgresql://{credentials}{host}:{port}/{database}"


def database_variables_seen() -> list[str]:
    """Which database-ish variables this process can actually see.

    Printed when there is no connection string, because the useful question is
    not "is DATABASE_URL set" - the operator has usually just set it - but
    "what did this container actually receive". A list that contains RAILWAY_*
    and nothing else answers it immediately.
    """
    # Whole-word-ish matching. A substring test for "PG" reported the Python
    # base image's GPG_KEY as a database variable, which is worse than saying
    # nothing: the list exists to be trusted at a glance.
    exact = {
        "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE",
        "PGSSLMODE", "PGAPPNAME", "PGHOSTADDR",
    }
    names = sorted(
        name for name in os.environ
        if name.upper() in exact
        or name.upper() in DATABASE_URL_NAMES
        or name.upper().startswith(("DATABASE_", "POSTGRES_", "POSTGRESQL_"))
    )
    # An empty value is the important case and it looked identical to a set
    # one: a platform reference that does not resolve is saved as the empty
    # string, so the name is present, the value is not, and a list of bare
    # names says "it is set" when the truth is "it is set to nothing".
    labelled = []
    for name in names:
        if not os.environ[name].strip():
            labelled.append(f"{name} (PRESENT BUT EMPTY)")
        elif name in _BAD_URLS:
            labelled.append(f"{name} (SET, BUT NAMES NO HOST - its references "
                            f"did not resolve)")
        else:
            labelled.append(name)
    return labelled


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret_key: str
    secure_cookies: bool
    session_hours: int
    base_url: str
    debug: bool

    @property
    def is_configured(self) -> bool:
        return bool(self.database_url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv()
    return Settings(
        database_url=_database_url(),
        # Encrypts per-org secrets at rest. Generate once with:
        #   python -c "from cryptography.fernet import Fernet; \
        #              print(Fernet.generate_key().decode())"
        # Changing it makes every stored secret undecryptable, so set it and keep it.
        secret_key=os.environ.get("FORGE_SECRET_KEY", ""),
        # Railway terminates TLS, so the session cookie must be marked Secure there.
        # Leave it on over plain http://localhost and the browser silently drops the
        # cookie, which looks exactly like sign-in failing for no reason.
        secure_cookies=_bool("FORGE_SECURE_COOKIES", False),
        session_hours=_int("FORGE_SESSION_HOURS", 12),
        base_url=os.environ.get("FORGE_BASE_URL", "http://127.0.0.1:8000"),
        debug=_bool("FORGE_DEBUG", False),
    )
