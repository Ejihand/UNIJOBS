#!/usr/bin/env python3
"""Scrape UNjobs listings, filter by keyword groups, apply eligibility rules, email new rows.

Use https://unjobs.org/new as the listing source (UNJOBS_URL / --url). The path
/latest is invalid on the live site and returns a resource-not-found error.
"""

from __future__ import annotations

import argparse
import html as html_module
import json
import logging
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# UNjobs "Latest jobs" feed — use this URL (not https://unjobs.org/latest).
DEFAULT_UNJOBS_LISTING_URL = "https://unjobs.org/new"

DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

# Groups A / B / C — union match: any phrase anywhere in searched text matches.
KEYWORD_PHRASES: Dict[str, List[str]] = {
    "A": [
        "medical laboratory",
        "clinical laboratory",
        "pathology",
        "histopathology",
        "microbiology",
        "laboratory",
    ],
    "B": [
        "research",
        "epidemiology",
        "public health",
        "surveillance",
        "outbreak",
        "immunization",
    ],
    "C": [
        "ai evaluation",
        "model evaluation",
        "ml evaluation",
        "algorithm validation",
        "clinical ai",
        "digital health",
        "health data",
        "bioinformatics",
        "machine learning",
        "deep learning",
        "large language",
        "llm",
        "natural language",
        "nlp",
        "responsible ai",
        "algorithm impact",
        "performance evaluation",
        "benchmark",
    ],
}


def _flatten_keywords_sorted() -> List[str]:
    """Longer phrases first so substrings do not steal matches."""
    phrases: List[str] = []
    for items in KEYWORD_PHRASES.values():
        phrases.extend(items)
    return sorted(set(phrases), key=len, reverse=True)


ALL_KEYWORDS_SORTED: List[str] = _flatten_keywords_sorted()

CLOSING_MS_RE = re.compile(r"var f\w+pi\s*=\s*(\d+)\s*;var f\w+pd\s*=\s*(\d+)")


@dataclass
class ParsedJob:
    """Vacancy from listing pages plus optional detail-page enrichment."""

    url: str
    title: str
    organization: str
    updated_at: Optional[str] = None
    detail_text: Optional[str] = None
    closing_display: Optional[str] = None


def normalize_job_url(href: str, page_url: str) -> str:
    """
    Build an absolute https URL for a vacancy on unjobs.org.

    Args:
        href: Raw href from a listing anchor (may be relative).
        page_url: The listing page URL used with ``urljoin`` for relative links.

    Returns:
        Canonicalized absolute URL without fragment.
    """
    absolute = urljoin(page_url, href)
    parts = urlparse(absolute)
    netloc = parts.netloc or "unjobs.org"
    scheme = "https" if parts.scheme in ("", "http", "https") else parts.scheme
    return urlunparse((scheme, netloc, parts.path, parts.params, parts.query, ""))


def fetch_html(session: requests.Session, url: str, timeout: int = 30) -> str:
    """
    GET HTML with retries.

    Args:
        session: Shared ``requests.Session``.
        url: Full URL to fetch.
        timeout: Socket timeout in seconds.

    Returns:
        Response body as text.

    Raises:
        The last exception if all attempts fail.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, 4):
        try:
            response = session.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.text
        except Exception as exc:
            last_exc = exc
            logger.warning("HTTP fetch attempt %s failed for %s: %s", attempt, url, exc)
            time.sleep(1.5 * attempt)
    assert last_exc is not None
    raise last_exc


def _extract_org_from_job_div(div: Tag) -> str:
    """Return organization line from a ``div.job`` text block."""
    lines = [
        line.strip()
        for line in div.get_text("\n").splitlines()
        if line.strip() and not line.strip().lower().startswith("updated:")
    ]
    if len(lines) >= 2:
        return lines[1]
    return ""


def parse_listing_html(page_html: str, page_url: str) -> List[ParsedJob]:
    """
    Parse vacancy rows from a UNjobs listing.

    Uses ``div.job`` + ``a.jtitle`` (current site) with fallbacks to ``a.jheading``.

    Args:
        page_html: Raw HTML from the listing request.
        page_url: Final URL of this page (for resolving links).

    Returns:
        List of :class:`ParsedJob` entries (may be empty if the layout changed).
    """
    soup = BeautifulSoup(page_html, "lxml")
    jobs: List[ParsedJob] = []
    seen_urls: Set[str] = set()

    for div in soup.select("div.job"):
        if div.find("ins", class_=lambda c: bool(c) and "adsbygoogle" in c):
            continue
        if div.find(class_="adsbygoogle"):
            continue

        link = div.select_one("a.jtitle") or div.select_one("a.jheading")
        if not link or not link.get("href"):
            continue
        href = str(link["href"])
        lowered = href.lower()
        if "flexjobs" in lowered or "flexjob" in lowered:
            continue

        title = link.get_text(" ", strip=True)
        if not title:
            continue

        organization = _extract_org_from_job_div(div) or "Unknown organization"
        if "flexjob" in organization.lower():
            continue

        time_el = div.select_one("time.upd") or div.select_one("time[datetime]")
        updated_at = time_el.get("datetime") if time_el else None

        url = normalize_job_url(href, page_url)
        if url in seen_urls:
            continue
        seen_urls.add(url)

        jobs.append(
            ParsedJob(
                url=url,
                title=title,
                organization=organization,
                updated_at=updated_at,
            )
        )

    if jobs:
        return jobs

    # Fallback: orphan title links
    for link in soup.select("a.jtitle, a.jheading"):
        href = link.get("href")
        if not href or "/vacancies/" not in str(href):
            continue
        title = link.get_text(" ", strip=True)
        if not title:
            continue
        url = normalize_job_url(str(href), page_url)
        if url not in seen_urls:
            seen_urls.add(url)
            jobs.append(ParsedJob(url=url, title=title, organization="", updated_at=None))

    return jobs


def listing_page_url(start_url: str, page_index: int) -> str:
    """
    Build the URL for page ``page_index`` (1-based).

    UNjobs exposes numbered paths such as ``https://unjobs.org/new/2``.

    Args:
        start_url: User-configured listing root (first page).
        page_index: Page number, starting at 1.

    Returns:
        Fully qualified URL for that page.
    """
    start_url = start_url.strip()
    if page_index <= 1:
        return start_url.rstrip("/")

    parsed = urlparse(start_url)
    if parsed.query:
        logger.warning(
            "Pagination for URLs with query strings is ambiguous; repeating: %s", start_url
        )
        return start_url

    path = parsed.path.rstrip("/")
    path = re.sub(r"/\d+$", "", path)
    stem = urlunparse((parsed.scheme or "https", parsed.netloc, path, "", "", "")).rstrip("/")
    return f"{stem}/{page_index}"


def fix_legacy_latest_listing_url(url: str) -> str:
    """
    Rewrite an obsolete ``/latest`` listing path to ``/new``.

    The live UNjobs host does not publish listings at ``/latest``; that path
    returns a "Resource not found" site error. The current "Latest" list is
    served from ``/new``.

    Args:
        url: User-supplied ``UNJOBS_URL`` or ``--url`` value.

    Returns:
        The same URL, or a corrected URL with the last path segment ``latest``
        replaced by ``new``.
    """
    parsed = urlparse(url.strip())
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments or segments[-1].lower() != "latest":
        return url.strip()

    segments[-1] = "new"
    new_path = "/" + "/".join(segments)
    fixed = urlunparse(
        (
            parsed.scheme or "https",
            parsed.netloc,
            new_path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )
    logger.warning(
        "UNjobs has no /latest job listing (that path returns a site error). Using %s",
        fixed,
    )
    return fixed


def scrape_listings(
    session: requests.Session,
    start_url: str,
    max_pages: int,
    delay: float,
) -> List[ParsedJob]:
    """
    Download up to ``max_pages`` listing pages and merge unique vacancies.

    Args:
        session: HTTP session.
        start_url: First-page listing URL.
        max_pages: Maximum number of pages (each ~25 rows on UNjobs).
        delay: Politeness delay between successful page fetches.

    Returns:
        Deduplicated list of parsed jobs (by vacancy URL).
    """
    all_rows: Dict[str, ParsedJob] = {}
    for page in range(1, max_pages + 1):
        url = listing_page_url(start_url, page)
        logger.info("Fetching listing page %s: %s", page, url)
        try:
            listing_html = fetch_html(session, url)
        except Exception:
            logger.exception("Failed to fetch listing page; stopping pagination.")
            break

        rows = parse_listing_html(listing_html, url)
        if not rows:
            logger.info("No job rows on page %s; stopping.", page)
            break

        new_in_page = 0
        for row in rows:
            if row.url not in all_rows:
                all_rows[row.url] = row
                new_in_page += 1

        if new_in_page == 0:
            logger.info("No new unique rows on page %s; stopping pagination.", page)
            break

        time.sleep(delay)

    return list(all_rows.values())


def parse_closing_from_detail(page_html: str) -> Optional[str]:
    """
    Recover a closing timestamp from embedded JavaScript (best effort).

    Args:
        page_html: Raw HTML from a vacancy detail page.

    Returns:
        ISO-like display string in UTC, or None if not found.
    """
    match = CLOSING_MS_RE.search(page_html)
    if not match:
        return None
    try:
        total_ms = int(match.group(1)) + int(match.group(2))
        closing_dt = datetime.fromtimestamp(total_ms / 1000.0, tz=timezone.utc)
        return closing_dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OSError):
        return None


def parse_detail_snippet(page_html: str, max_chars: int = 8000) -> str:
    """
    Extract visible text from the job description area.

    Args:
        page_html: Raw HTML from a vacancy detail page.
        max_chars: Safety cap on stored characters.

    Returns:
        Plain text suitable for keyword and guardrail checks.
    """
    soup = BeautifulSoup(page_html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    node = soup.select_one("div.fp-snippet") or soup.select_one('div[id^="job"]')
    if not node:
        return ""
    text = node.get_text("\n", strip=True)
    return text[:max_chars]


def enrich_job_detail(session: requests.Session, job: ParsedJob, timeout: int = 30) -> None:
    """
    Fill ``detail_text`` and ``closing_display`` by fetching the detail page.

    Args:
        session: HTTP session.
        job: Job row to enrich in place.
        timeout: Request timeout in seconds.
    """
    try:
        page_html = fetch_html(session, job.url, timeout=timeout)
    except Exception as exc:
        logger.warning("Detail fetch failed for %s: %s", job.url, exc)
        return

    job.detail_text = parse_detail_snippet(page_html)
    job.closing_display = parse_closing_from_detail(page_html)


def matches_keyword_matrix(text: str) -> bool:
    """
    Return True if ``text`` contains any configured keyword phrase (case-insensitive).

    Args:
        text: Concatenated fields to search.

    Returns:
        Whether a hit was found in the union of groups A, B, and C.
    """
    lower = text.lower()
    for phrase in ALL_KEYWORDS_SORTED:
        if phrase.lower() in lower:
            return True
    return False


def passes_nigeria_guardrail(job: ParsedJob) -> bool:
    """
    Nigerian eligibility guardrail for locally restricted postings.

    International Professional (P/D) and similar globally advertised posts pass
    unless the text clearly signals local-only hiring. When ``local
    recruitment``, ``national officer``, certain national consultant patterns,
    or explicit ``(NO)`` grades appear, the posting is kept only if the
    location context indicates **Nigeria** or **remote** / home-based work.

    Args:
        job: Parsed row with title, organization, optional detail text.

    Returns:
        True if the row should be included; False if excluded as local-only abroad.
    """
    title_lower = job.title.lower()
    combined = " ".join(
        fragment
        for fragment in (job.title, job.organization, job.detail_text or "")
        if fragment
    ).lower()

    national_consultant_local = (
        "national consultant" in title_lower and "international" not in title_lower
    )

    needs_local_gate = (
        "local recruitment" in combined
        or "national officer" in combined
        or national_consultant_local
        or re.search(r"\(no\)", title_lower) is not None
    )

    if not needs_local_gate:
        return True

    location_blob = (job.title + " " + (job.detail_text or "")).lower()

    ok_place = (
        "nigeria" in location_blob
        or re.search(r"\bremote\b", location_blob) is not None
        or "home-based" in location_blob
        or "home based" in location_blob
    )

    if ok_place:
        return True

    logger.info("Guardrail excluded (local/NO-style, not Nigeria/remote): %s", job.title)
    return False


def load_state(path: Path) -> Dict[str, Any]:
    """
    Load the deduplication map from disk.

    Args:
        path: JSON file path.

    Returns:
        Mapping of vacancy URL to metadata; empty if missing or corrupt.
    """
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return dict(data)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read state file %s: %s", path, exc)
    return {}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    """
    Persist deduplication state atomically.

    Args:
        path: Target JSON path (parent dirs created if needed).
        state: Full map to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    tmp_path.replace(path)


def build_html_email(jobs: List[ParsedJob], banner: str = "") -> str:
    """
    Build an HTML email body with inline styles.

    Args:
        jobs: Vacancies to render.
        banner: Optional short header note.

    Returns:
        HTML string suitable for ``MIMEText``.
    """
    rows_html: List[str] = []
    for job in jobs:
        title_esc = html_module.escape(job.title)
        org_esc = html_module.escape(job.organization)
        closing_esc = html_module.escape(job.closing_display or "see listing")
        updated_esc = html_module.escape(job.updated_at or "unknown")
        url_esc = html_module.escape(job.url, quote=True)
        rows_html.append(
            f"""
<tr><td style="padding:8px 0;">
<table style="width:100%;border-left:4px solid #2563eb;background:#ffffff;margin:0 0 12px 0;">
<tr><td style="padding:12px 16px;font-family:Arial,Helvetica,sans-serif;">
<div style="font-size:16px;font-weight:700;margin-bottom:6px;">
<a href="{url_esc}" style="color:#1d4ed8;text-decoration:none;">{title_esc}</a>
</div>
<div style="font-size:13px;color:#111827;margin-bottom:4px;">{org_esc}</div>
<div style="font-size:12px;color:#6b7280;font-style:italic;">Updated: {updated_esc}</div>
<div style="font-size:12px;color:#6b7280;font-style:italic;">Closing (best effort): {closing_esc}</div>
</td></tr></table>
</td></tr>"""
        )

    banner_esc = html_module.escape(banner) if banner else ""
    return f"""\
<html><body style="margin:0;padding:0;background:#f3f4f6;">
<table role="presentation" style="width:100%;background:#f3f4f6;padding:24px 0;">
<tr><td align="center">
<table style="max-width:720px;width:100%;background:#f3f4f6;">
<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:18px;font-weight:700;padding:0 16px 16px 16px;color:#111827;">
UNjobs digest{(" — " + banner_esc) if banner_esc else ""}
</td></tr>
{"".join(rows_html)}
<tr><td style="padding:16px;font-size:11px;color:#9ca3af;font-family:Arial,Helvetica,sans-serif;">
Curated automatically. Verify each posting on UNjobs before applying.
</td></tr>
</table></td></tr></table></body></html>"""


def send_html_email(
    jobs: List[ParsedJob],
    sender: str,
    receiver: str,
    password: str,
    smtp_host: str,
    smtp_port: int,
) -> None:
    """
    Send multipart email via SMTP STARTTLS.

    Args:
        jobs: Rows to include in the digest.
        sender: SMTP login and From address.
        receiver: Recipient address.
        password: App password (Gmail) or SMTP secret.
        smtp_host: SMTP server hostname.
        smtp_port: SMTP port (587 for Gmail STARTTLS).
    """
    if not jobs:
        logger.info("No jobs to email.")
        return

    msg: MIMEMultipart = MIMEMultipart("alternative")
    msg["Subject"] = f"UNjobs digest: {len(jobs)} new match(es)"
    msg["From"] = sender
    msg["To"] = receiver

    plain_lines = [f"{j.title}\n{j.url}\n" for j in jobs]
    plain_body = "\n".join(plain_lines)
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(build_html_email(jobs), "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(sender, password)
            server.sendmail(sender, [receiver], msg.as_string())
        logger.info("Email sent to %s", receiver)
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP authentication failed (check App Password): %s", exc)
        raise
    except Exception as exc:
        logger.error("SMTP error: %s", exc)
        raise


def run_pipeline(
    *,
    dry_run: bool,
    max_pages: int,
    start_url: str,
    state_path: Path,
    max_detail_fetches: int,
    delay: float,
    force_resend: bool,
) -> int:
    """
    Execute scrape, filter, optional mail, and state update.

    Returns:
        Exit code (0 success).
    """
    load_dotenv()
    sender = os.environ.get("SENDER_EMAIL", "")
    password = os.environ.get("SENDER_PASSWORD", "")
    receiver = os.environ.get("RECEIVER_EMAIL", "")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    start_url = fix_legacy_latest_listing_url(start_url)

    session = requests.Session()
    all_jobs = scrape_listings(session, start_url, max_pages, delay)

    list_matches = [job for job in all_jobs if matches_keyword_matrix(f"{job.title} {job.organization}")]
    logger.info("Keyword matches on listing text: %s", len(list_matches))

    detail_used = 0
    for job in list_matches:
        if detail_used >= max_detail_fetches:
            logger.warning(
                "Reached MAX_DETAIL_FETCHES (%s); remaining rows skip detail enrichment.",
                max_detail_fetches,
            )
            break
        enrich_job_detail(session, job)
        detail_used += 1
        time.sleep(delay)

    full_text_matches: List[ParsedJob] = []
    for job in list_matches:
        blob = f"{job.title} {job.organization} {job.detail_text or ''}"
        if matches_keyword_matrix(blob):
            full_text_matches.append(job)

    logger.info("Keyword matches after detail text: %s", len(full_text_matches))

    gated = [job for job in full_text_matches if passes_nigeria_guardrail(job)]
    logger.info("Rows after Nigerian eligibility guardrail: %s", len(gated))

    state = load_state(state_path)
    if force_resend:
        fresh_jobs = gated
    else:
        fresh_jobs = [job for job in gated if job.url not in state]

    logger.info("New vacancies (not in state file): %s", len(fresh_jobs))

    if dry_run:
        for job in fresh_jobs:
            print(f"{job.title}\n  {job.url}\n  {job.organization}\n")
        return 0

    if not fresh_jobs:
        logger.info("Nothing new to email.")
        return 0

    if not sender or not password or not receiver:
        logger.error("Set SENDER_EMAIL, SENDER_PASSWORD, and RECEIVER_EMAIL for email delivery.")
        return 1

    try:
        send_html_email(fresh_jobs, sender, receiver, password, smtp_host, smtp_port)
    except Exception:
        logger.exception("Failed to send email; state file not updated.")
        return 1

    now = datetime.now(timezone.utc).isoformat()
    for job in fresh_jobs:
        state[job.url] = {"first_seen": now}
    save_state(state_path, state)
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments and environment fallbacks."""
    parser = argparse.ArgumentParser(
        description="Scrape UNjobs, filter by keywords, email new vacancies.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("UNJOBS_URL", DEFAULT_UNJOBS_LISTING_URL),
        help=(
            f"Listing page URL. Use {DEFAULT_UNJOBS_LISTING_URL} for the main Latest feed; "
            "/latest is not valid on unjobs.org."
        ),
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=int(os.environ.get("MAX_PAGES", "3")),
        help="Maximum listing pages to fetch.",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path(os.environ.get("STATE_PATH", "data/seen_jobs.json")),
        help="JSON path for deduplication.",
    )
    parser.add_argument(
        "--max-detail-fetches",
        type=int,
        default=int(os.environ.get("MAX_DETAIL_FETCHES", "50")),
        help="Cap on detail-page requests for enrichment.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=float(os.environ.get("REQUEST_DELAY_SECONDS", "0.5")),
        help="Delay between HTTP calls (seconds).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print matches; do not send email or update state.",
    )
    parser.add_argument(
        "--force-resend",
        action="store_true",
        help="Email all current matches and refresh state (use sparingly).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    """CLI entrypoint."""
    args = parse_args(argv)
    code = run_pipeline(
        dry_run=bool(args.dry_run),
        max_pages=args.max_pages,
        start_url=args.url,
        state_path=args.state_path,
        max_detail_fetches=args.max_detail_fetches,
        delay=args.delay,
        force_resend=bool(args.force_resend),
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
