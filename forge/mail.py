"""Sending email, when there is somewhere to send it.

This app has one thing to send: a password reset link. That makes the design
simple, and it makes one decision important.

With SMTP configured the link is emailed and nothing else happens to it. With
no SMTP - which is every local run, and any deployment before the mail
variables are set - the link is written to the **server log** and to the audit
trail, and never to the browser. That last part is the whole point: showing a
reset link to whoever typed an address into a form would let anyone take over
any account by asking. A log line needs access to the server, which is a person
who could reset the password anyway.

So the flow works from the first minute without a mail account, without ever
being a way in.

Configure with:
    FORGE_SMTP_HOST, FORGE_SMTP_PORT (default 587), FORGE_SMTP_USER,
    FORGE_SMTP_PASSWORD, FORGE_MAIL_FROM, FORGE_SMTP_TLS (default true)
"""

from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Outcome:
    sent: bool
    detail: str = ""


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def configured() -> bool:
    return bool(_env("FORGE_SMTP_HOST") and _env("FORGE_MAIL_FROM"))


def send(to: str, subject: str, body: str) -> Outcome:
    """Send one plain-text message. Never raises."""
    if not configured():
        # Deliberately the whole body: the operator needs the link itself, and
        # this is the only place it can safely appear.
        log.warning(
            "email not configured - not sent. To: %s\nSubject: %s\n\n%s",
            to, subject, body,
        )
        return Outcome(False, "Email is not configured on this server.")

    message = EmailMessage()
    message["From"] = _env("FORGE_MAIL_FROM")
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    host = _env("FORGE_SMTP_HOST")
    port = int(_env("FORGE_SMTP_PORT", "587") or 587)
    user = _env("FORGE_SMTP_USER")
    password = _env("FORGE_SMTP_PASSWORD")
    use_tls = _env("FORGE_SMTP_TLS", "true").lower() not in ("0", "false", "no")

    try:
        # 465 is implicit TLS; 587 is STARTTLS. Getting this pair wrong is the
        # usual reason a correct set of credentials appears to be rejected.
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as server:
                if user:
                    server.login(user, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                if use_tls:
                    server.starttls()
                if user:
                    server.login(user, password)
                server.send_message(message)
    except Exception as exc:  # noqa: BLE001
        # A failure to send must not break the request. The caller shows the
        # same message either way, so the person is told to check their inbox
        # while the operator gets the reason here.
        log.exception("could not send email to %s", to)
        return Outcome(False, f"{type(exc).__name__}: {exc}"[:200])

    return Outcome(True)


def reset_email(name: str, link: str, hours: int) -> tuple[str, str]:
    """The reset message. Returned rather than sent so it can be tested."""
    greeting = f"Hello {name}," if name else "Hello,"
    return (
        "Reset your Model Forge password",
        f"""{greeting}

Someone asked to reset the password for this Model Forge account. If that was
you, open the link below and choose a new one:

{link}

The link works once and expires in {hours} hours.

If it was not you, you can ignore this message - nothing has changed, and your
current password still works.
""",
    )
