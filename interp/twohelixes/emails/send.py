"""Send HTML mail through Amazon SES SMTP.

Mirrors netwrck's path: `AWS_SMTP_USERNAME` / `AWS_SMTP_PASSWORD`, from-address
`SES_FROM_EMAIL` with fallback `lee.penkman@netwrck.com`. Missing credentials
are not an error for the request path — callers degrade (forgot-password still
returns the generic success) so a laptop without SES stays usable.
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import lru_cache
from typing import Any

from twohelixes import config

log = logging.getLogger("twohelixes.emails")

FROM_EMAIL = "lee.penkman@netwrck.com"
FROM_NAME = "twoHelixes"
TEMPLATES = config.REPO_ROOT / "emails"


@lru_cache(maxsize=1)
def _mail_config() -> dict[str, Any]:
    path = TEMPLATES / "config.json"
    if not path.is_file():
        return {"from_email": FROM_EMAIL, "from_name": FROM_NAME}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"from_email": FROM_EMAIL, "from_name": FROM_NAME}


def from_address() -> str:
    return (
        config.get("SES_FROM_EMAIL")
        or str(_mail_config().get("from_email") or FROM_EMAIL)
    )


def available() -> bool:
    return bool(config.get("AWS_SMTP_USERNAME") and config.get("AWS_SMTP_PASSWORD"))


def render(name: str, **values: str) -> str:
    path = TEMPLATES / name
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def send_html(to_email: str, subject: str, html_body: str, text_body: str = "") -> bool:
    """Send one message. Returns False when SMTP is unset or the send fails."""
    user = config.get("AWS_SMTP_USERNAME") or ""
    password = config.get("AWS_SMTP_PASSWORD") or ""
    if not user or not password:
        log.info("smtp unset; skipped mail to %s (%s)", to_email, subject)
        return False

    region = config.get("AWS_REGION") or "us-east-1"
    host = f"email-smtp.{region}.amazonaws.com"
    sender = from_address()
    plain = text_body or _strip_tags(html_body)

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = f"{FROM_NAME} <{sender}>"
    message["To"] = to_email
    message.attach(MIMEText(plain, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))
    payload = message.as_string()

    try:
        context = ssl.create_default_context()
        try:
            with smtplib.SMTP_SSL(host, 465, context=context, timeout=20) as smtp:
                smtp.login(user, password)
                smtp.sendmail(sender, [to_email], payload)
        except OSError:
            with smtplib.SMTP(host, 587, timeout=20) as smtp:
                smtp.starttls(context=context)
                smtp.login(user, password)
                smtp.sendmail(sender, [to_email], payload)
        return True
    except Exception:  # noqa: BLE001 - mail must not sink the request
        log.exception("failed to send mail to %s (%s)", to_email, subject)
        return False


def send_password_reset(to_email: str, reset_url: str) -> bool:
    html_body = render(
        "password-reset.html", email=to_email, reset_url=reset_url
    )
    return send_html(
        to_email,
        "Reset your twoHelixes password",
        html_body,
        text_body=(
            f"Reset your twoHelixes password:\n\n{reset_url}\n\n"
            "This link expires in one hour."
        ),
    )


def send_welcome(to_email: str) -> bool:
    app_url = config.site_url().rstrip("/") + "/app"
    html_body = render("welcome.html", email=to_email, app_url=app_url)
    return send_html(
        to_email,
        "Welcome to twoHelixes",
        html_body,
        text_body=f"Welcome to twoHelixes.\n\nOpen the app: {app_url}\n",
    )


def _strip_tags(html_body: str) -> str:
    out: list[str] = []
    in_tag = False
    for char in html_body:
        if char == "<":
            in_tag = True
            continue
        if char == ">":
            in_tag = False
            continue
        if not in_tag:
            out.append(char)
    return "".join(out).strip()
