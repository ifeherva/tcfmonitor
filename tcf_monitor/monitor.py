from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import smtplib
import ssl
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOGGER = logging.getLogger("tcf_monitor")
DEFAULT_CONFIG = Path("config/monitors.json")
DEFAULT_STATE = Path(".monitor-state/state.json")
USER_AGENT = "TCFExamAvailabilityMonitor/1.0 (+https://github.com/ifeherva/tcfmonitor)"


@dataclass(frozen=True)
class Session:
    key: str
    title: str
    schedule: str
    registration_dates: str
    location: str
    spots: str
    price: str
    status: str
    is_open: bool
    booking_url: str


def normalized_text(node: Tag | None) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def session_key(title: str, schedule: str, location: str) -> str:
    identity = "\n".join((title.casefold(), schedule.casefold(), location.casefold()))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def parse_oncord_exam_table(page_html: str, page_url: str) -> list[Session]:
    """Parse the Oncord exam table used by Alliance Francaise Vancouver."""
    soup = BeautifulSoup(page_html, "html.parser")
    table = soup.select_one("table#s8-datatable1")
    if table is None:
        raise ValueError("exam table #s8-datatable1 was not found")

    row_nodes = table.select("tr.tableRow")
    if not row_nodes:
        raise ValueError("exam table was found but contained no exam rows")

    sessions: list[Session] = []
    for row_number, row in enumerate(row_nodes, start=1):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 7:
            raise ValueError(
                f"exam row {row_number} contained {len(cells)} cells; expected at least 7"
            )

        title = normalized_text(row.select_one(".es-exam-title"))
        if not title:
            raise ValueError(f"exam row {row_number} did not contain an exam title")

        schedule = normalized_text(cells[1])
        registration_dates = normalized_text(cells[2])
        location = normalized_text(cells[3])
        spots = normalized_text(cells[4])
        price = normalized_text(cells[5])
        status_node = row.select_one(".es-status")
        held_node = row.select_one(".es-held-card")

        if status_node is not None:
            status = normalized_text(status_node)
            if not status:
                raise ValueError(
                    f"exam row {row_number} contained an empty booking status"
                )
            status_classes = set(status_node.get("class", []))
            status_lower = status.casefold()
            is_open = "es-status-available" in status_classes or status_lower in {
                "available",
                "book now",
                "open",
                "register",
                "register now",
            }
            booking_link = status_node.find("a", href=True)
            booking_url = (
                urljoin(page_url, str(booking_link["href"]))
                if booking_link
                else page_url
            )
        elif held_node is not None:
            # The site temporarily replaces the normal status element while one
            # or more seats are reserved in another visitor's checkout session.
            # A held seat is not bookable, but its release should later produce
            # the normal unavailable -> available transition and an alert.
            status = "Spots held"
            is_open = False
            booking_url = page_url
        else:
            raise ValueError(f"exam row {row_number} did not contain a booking status")

        sessions.append(
            Session(
                key=session_key(title, schedule, location),
                title=title,
                schedule=schedule,
                registration_dates=registration_dates,
                location=location,
                spots=spots,
                price=price,
                status=status,
                is_open=is_open,
                booking_url=booking_url,
            )
        )

    return sessions


PARSERS = {"oncord_exam_table": parse_oncord_exam_table}


def build_http_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-CA,en;q=0.9",
        }
    )
    return session


def fetch_sessions(
    http: requests.Session, monitor: dict[str, Any], timeout_seconds: int = 30
) -> list[Session]:
    parser_name = monitor.get("parser", "oncord_exam_table")
    try:
        parser = PARSERS[parser_name]
    except KeyError as exc:
        raise ValueError(f"unknown parser {parser_name!r}") from exc

    response = http.get(monitor["url"], timeout=timeout_seconds)
    response.raise_for_status()
    return parser(response.text, monitor["url"])


def load_config(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    monitors = data.get("monitors")
    if not isinstance(monitors, list) or not monitors:
        raise ValueError("configuration must contain a non-empty 'monitors' list")

    seen_ids: set[str] = set()
    for monitor in monitors:
        missing = {"id", "name", "url"} - monitor.keys()
        if missing:
            raise ValueError(f"monitor is missing required fields: {sorted(missing)}")
        if monitor["id"] in seen_ids:
            raise ValueError(f"duplicate monitor id: {monitor['id']}")
        seen_ids.add(monitor["id"])
    return monitors


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "monitors": {}}
    with path.open(encoding="utf-8") as handle:
        state = json.load(handle)
    if state.get("version") != 1 or not isinstance(state.get("monitors"), dict):
        raise ValueError(f"unsupported or invalid state file: {path}")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary_path.replace(path)


def newly_open_sessions(
    sessions: list[Session], previous_monitor_state: dict[str, Any]
) -> list[Session]:
    previous_sessions = previous_monitor_state.get("sessions", {})
    return [
        session
        for session in sessions
        if session.is_open
        and not previous_sessions.get(session.key, {}).get("is_open", False)
    ]


def split_addresses(raw_addresses: str) -> list[str]:
    addresses = re.split(r"[,;\n]", raw_addresses)
    return list(
        dict.fromkeys(address.strip() for address in addresses if address.strip())
    )


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def notification_addresses() -> tuple[str, list[str]]:
    recipients = split_addresses(os.getenv("NOTIFY_EMAILS", ""))
    sender = (
        os.getenv("EMAIL_FROM", "").strip() or os.getenv("SMTP_USERNAME", "").strip()
    )
    if not recipients:
        raise ValueError("NOTIFY_EMAILS is empty")
    if not sender:
        raise ValueError("EMAIL_FROM (or SMTP_USERNAME) is empty")
    return sender, recipients


def build_email(openings: list[tuple[dict[str, Any], Session]]) -> EmailMessage:
    sender, recipients = notification_addresses()

    count = len(openings)
    noun = "opening" if count == 1 else "openings"
    subject = f"[TCF Monitor] {count} exam {noun} found"

    text_parts = ["TCF exam availability has changed:", ""]
    html_parts = [
        "<h2>TCF exam availability has changed</h2>",
        "<p>Book quickly; availability can disappear at any time.</p>",
        "<ul>",
    ]
    for monitor, session in openings:
        text_parts.extend(
            [
                f"{session.title}",
                f"Monitor: {monitor['name']}",
                f"Schedule: {session.schedule}",
                f"Location: {session.location}",
                f"Spots: {session.spots or 'Available'}",
                f"Price: {session.price}",
                f"Book: {session.booking_url}",
                "",
            ]
        )
        html_parts.append(
            "<li>"
            f"<strong>{html.escape(session.title)}</strong><br>"
            f"{html.escape(session.schedule)}<br>"
            f"{html.escape(session.location)}<br>"
            f"Spots: {html.escape(session.spots or 'Available')} &middot; "
            f"Price: {html.escape(session.price)}<br>"
            f'<a href="{html.escape(session.booking_url, quote=True)}">Open booking page</a>'
            "</li>"
        )
    html_parts.append("</ul>")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    # Keep the notification list private if unrelated recipients are configured.
    message["To"] = sender
    message["Bcc"] = ", ".join(recipients)
    message.set_content("\n".join(text_parts))
    message.add_alternative("\n".join(html_parts), subtype="html")
    return message


def send_message(message: EmailMessage, recipients: list[str]) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        raise ValueError("SMTP_HOST is empty")
    try:
        port = int(os.getenv("SMTP_PORT", "").strip() or "587")
    except ValueError as exc:
        raise ValueError("SMTP_PORT must be an integer") from exc

    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    use_ssl = env_bool("SMTP_USE_SSL", port == 465)
    use_starttls = env_bool("SMTP_STARTTLS", not use_ssl)

    if use_ssl:
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(
            host, port, timeout=30, context=ssl.create_default_context()
        )
    else:
        smtp = smtplib.SMTP(host, port, timeout=30)

    with smtp:
        smtp.ehlo()
        if use_starttls:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if username:
            smtp.login(username, password)
        smtp.send_message(message, to_addrs=recipients)


def send_email(openings: list[tuple[dict[str, Any], Session]]) -> None:
    _, recipients = notification_addresses()
    send_message(build_email(openings), recipients)


def send_test_email() -> None:
    sender, recipients = notification_addresses()
    message = EmailMessage()
    message["Subject"] = "[TCF Monitor] Test email"
    message["From"] = sender
    message["To"] = sender
    message["Bcc"] = ", ".join(recipients)
    message.set_content(
        "Your TCF monitor email settings work. You will receive a separate email "
        "when an exam becomes available."
    )
    send_message(message, recipients)


def monitor_once(config_path: Path, state_path: Path, *, dry_run: bool = False) -> int:
    monitors = load_config(config_path)
    previous_state = load_state(state_path)
    next_state: dict[str, Any] = {"version": 1, "monitors": {}}

    http = build_http_session()
    all_openings: list[tuple[dict[str, Any], Session]] = []
    errors: list[str] = []
    checked_at = datetime.now(timezone.utc).isoformat()

    for monitor in monitors:
        try:
            sessions = fetch_sessions(http, monitor)
            previous_monitor_state = previous_state["monitors"].get(monitor["id"], {})
            openings = newly_open_sessions(sessions, previous_monitor_state)
            all_openings.extend((monitor, session) for session in openings)
            next_state["monitors"][monitor["id"]] = {
                "name": monitor["name"],
                "url": monitor["url"],
                "checked_at": checked_at,
                "sessions": {session.key: asdict(session) for session in sessions},
            }
            open_count = sum(session.is_open for session in sessions)
            LOGGER.info(
                "%s: checked %d sessions; %d open; %d newly open",
                monitor["name"],
                len(sessions),
                open_count,
                len(openings),
            )
        except Exception as exc:  # Preserve prior state for a failed monitor.
            previous_monitor_state = previous_state["monitors"].get(monitor["id"])
            if previous_monitor_state is not None:
                next_state["monitors"][monitor["id"]] = previous_monitor_state
            message = f"{monitor['name']}: {exc}"
            LOGGER.exception("Monitor failed: %s", message)
            errors.append(message)

    if dry_run:
        for monitor, session in all_openings:
            print(f"OPEN: {monitor['name']} — {session.title} — {session.booking_url}")
        LOGGER.info("Dry run: no email sent and state was not changed")
    else:
        if all_openings:
            # State is saved only after delivery, so a transient SMTP error is retried.
            send_email(all_openings)
            LOGGER.info(
                "Sent one notification email for %d opening(s)", len(all_openings)
            )
        save_state(state_path, next_state)

    if errors:
        LOGGER.error("%d monitor(s) failed", len(errors))
        return 1
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor TCF exam availability")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and report openings without sending email or saving state",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="send a test notification before performing the availability check",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        if args.test_email:
            send_test_email()
            LOGGER.info("Sent a test email")
        return monitor_once(args.config, args.state, dry_run=args.dry_run)
    except Exception:
        LOGGER.exception("TCF monitor failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
