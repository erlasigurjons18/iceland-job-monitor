from __future__ import annotations

import json
import os
import re
import smtplib
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright


SITES = [
    {
        "company": "Eirr Medical",
        "url": "https://www.eirrmedical.com/careers",
        "kind": "eirr",
    },
    {
        "company": "Nox Medical",
        "url": "https://ats.rippling.com/nox-medical/jobs",
        "kind": "nox",
    },
    {
        "company": "Sidekick Health",
        "url": "https://sidekickhealth.jobs.personio.com/?language=en",
        "kind": "sidekick",
    },
]

STATE_PATH = Path("job_monitor_state.json")
RESET_DAYS = 30

# Nox keeps an evergreen General Application open. It is not a specific vacancy,
# so suppress it by default.
EXCLUDED_TITLES = {
    "general application",
    "general applications",
}

GENERIC_LINK_TEXT = {
    "view job",
    "view role & apply",
    "view role and apply",
    "apply",
    "apply now",
    "apply for this job",
}

ICELAND_MARKERS = (
    "iceland",
    "reykjavík",
    "reykjavik",
    "kópavogur",
    "kopavogur",
    "hafnarfjörður",
    "hafnarfjordur",
    "akureyri",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return clean_text(value).casefold()


def notification_key(company: str, title: str) -> str:
    # The key intentionally uses only company + position title, per the matching rule.
    return f"{normalize(company)}|||{normalize(title)}"


def is_iceland_text(value: str) -> bool:
    lowered = normalize(value)
    return any(marker.casefold() in lowered for marker in ICELAND_MARKERS)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"notifications": {}}

    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Could not read {STATE_PATH}: {exc}") from exc

    if not isinstance(data, dict):
        data = {}
    data.setdefault("notifications", {})
    return data


def save_state(state: dict) -> None:
    temp_path = STATE_PATH.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(STATE_PATH)


def prune_expired_notifications(state: dict, now: datetime) -> int:
    cutoff = now - timedelta(days=RESET_DAYS)
    notifications = state["notifications"]
    expired_keys: list[str] = []

    for key, item in notifications.items():
        try:
            notified_at = datetime.fromisoformat(item["notified_at"])
            if notified_at.tzinfo is None:
                notified_at = notified_at.replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            expired_keys.append(key)
            continue

        if notified_at <= cutoff:
            expired_keys.append(key)

    for key in expired_keys:
        notifications.pop(key, None)

    return len(expired_keys)


def is_job_url(kind: str, href: str) -> bool:
    parsed = urlparse(href)
    host = parsed.netloc.casefold()
    path = parsed.path.rstrip("/")

    if kind == "eirr":
        return host.endswith("eirrmedical.com") and path.startswith("/careers/")

    if kind == "nox":
        return (
            host == "ats.rippling.com"
            and re.search(r"/nox-medical/jobs/[0-9a-f-]{20,}$", path, re.IGNORECASE)
            is not None
        )

    if kind == "sidekick":
        return host == "sidekickhealth.jobs.personio.com" and re.fullmatch(
            r"/job/\d+", path
        ) is not None

    return False


def _candidate_title_from_text(source: str) -> str | None:
    for raw_line in (source or "").splitlines():
        line = clean_text(raw_line)
        if not line:
            continue

        # Some boards visually separate fields without a newline. Trim the text
        # at a known location marker when that happens.
        line = re.split(
            r"(?i)(?:remote\s*\(|reykjav[ií]k|iceland|k[oó]pavogur|hafnarfj[oö]r(?:ð|d)ur|akureyri)",
            line,
            maxsplit=1,
        )[0].strip(" -–—·|")

        if not line:
            continue
        if normalize(line) in GENERIC_LINK_TEXT:
            continue
        if len(line) > 180:
            continue
        return line

    return None


def extract_title(anchor_texts: list[str], contexts: list[str]) -> str | None:
    # Prefer the actual job link text. On all three boards this is either the
    # position title itself or a card whose first line is the position title.
    for source in anchor_texts:
        candidate = _candidate_title_from_text(source)
        if candidate:
            return candidate

    # Fall back to the closest location-bearing card/container.
    for source in contexts:
        candidate = _candidate_title_from_text(source)
        if candidate:
            return candidate

    return None


def extract_jobs(page: Page, site: dict) -> list[dict]:
    page.goto(site["url"], wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3_000)

    body_text = page.locator("body").inner_text(timeout=15_000)
    body_lower = normalize(body_text)
    if any(marker in body_lower for marker in ("access denied", "captcha", "verify you are human")):
        raise RuntimeError("The careers page appears to be blocking automated access")

    anchors = page.evaluate(
        """
        () => [...document.querySelectorAll('a[href]')].map(a => {
            const contexts = [];
            let node = a;
            for (let i = 0; i < 7 && node; i++, node = node.parentElement) {
                const text = (node.innerText || '').trim();
                if (text && text.length <= 2000 && !contexts.includes(text)) {
                    contexts.push(text);
                }
            }
            return {
                href: a.href,
                text: (a.innerText || '').trim(),
                contexts,
            };
        })
        """
    )

    grouped: dict[str, dict] = {}
    for anchor in anchors:
        href = anchor.get("href", "")
        if not is_job_url(site["kind"], href):
            continue

        contexts = anchor.get("contexts", []) or []
        iceland_contexts = [text for text in contexts if is_iceland_text(text)]
        if not iceland_contexts:
            continue

        item = grouped.setdefault(
            href,
            {
                "anchor_texts": [],
                "contexts": [],
            },
        )
        if anchor.get("text"):
            item["anchor_texts"].append(anchor["text"])
        item["contexts"].extend(iceland_contexts)

    jobs: list[dict] = []
    seen_keys: set[str] = set()

    for href, data in grouped.items():
        title = extract_title(data["anchor_texts"], data["contexts"])
        if not title:
            print(f"[{site['company']}] Skipping a job link because no title could be extracted: {href}")
            continue

        if normalize(title) in {normalize(x) for x in EXCLUDED_TITLES}:
            print(f"[{site['company']}] Ignoring evergreen listing: {title}")
            continue

        key = notification_key(site["company"], title)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        jobs.append(
            {
                "company": site["company"],
                "title": title,
                "url": href,
            }
        )

    return sorted(jobs, key=lambda item: normalize(item["title"]))


def send_email(job: dict) -> None:
    smtp_host = os.getenv("SMTP_HOST") or "smtp.gmail.com"
    smtp_port = int(os.getenv("SMTP_PORT") or "465")
    smtp_username = os.environ["SMTP_USERNAME"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    email_from = os.getenv("EMAIL_FROM") or smtp_username
    email_to = os.environ["EMAIL_TO"]

    message = EmailMessage()
    message["From"] = email_from
    message["To"] = email_to
    message["Subject"] = f"Job alert: {job['company']} — {job['title']}"
    message.set_content(
        f"A new Iceland position was found.\n\n"
        f"Company: {job['company']}\n"
        f"Position: {job['title']}\n"
        f"Link: {job['url']}\n\n"
        f"The same company + position title will not trigger another email for {RESET_DAYS} days.\n"
    )

    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)


def main() -> int:
    now = utc_now()
    state = load_state()
    pruned = prune_expired_notifications(state, now)
    if pruned:
        print(f"Expired {pruned} notification record(s) older than {RESET_DAYS} days.")
        save_state(state)

    failures: list[str] = []
    total_jobs = 0
    new_alerts = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 1000},
        )

        for site in SITES:
            try:
                jobs = extract_jobs(page, site)
            except Exception as exc:  # Keep checking the other companies.
                message = f"{site['company']}: {exc}"
                failures.append(message)
                print(f"ERROR: {message}", file=sys.stderr)
                continue

            total_jobs += len(jobs)
            print(f"[{site['company']}] Found {len(jobs)} Iceland position(s).")

            for job in jobs:
                key = notification_key(job["company"], job["title"])
                if key in state["notifications"]:
                    notified_at = state["notifications"][key].get("notified_at", "unknown")
                    print(
                        f"[{job['company']}] Already notified within {RESET_DAYS} days: "
                        f"{job['title']} ({notified_at})"
                    )
                    continue

                print(f"[{job['company']}] NEW: {job['title']}")
                send_email(job)
                new_alerts += 1

                # Record only after the email succeeds.
                state["notifications"][key] = {
                    "company": job["company"],
                    "title": job["title"],
                    "url": job["url"],
                    "notified_at": now.isoformat(),
                }
                save_state(state)

        browser.close()

    print(f"Done. {total_jobs} Iceland position(s) found; {new_alerts} new email alert(s) sent.")

    if failures:
        print("One or more sites failed and should be reviewed:", file=sys.stderr)
        for failure in failures:
            print(f" - {failure}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
