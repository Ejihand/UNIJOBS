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

# Health-focused keyword groups (primary match).
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
        "health officer",
        "health emergency",
        "mental health",
        "nutrition",
        "vaccination",
        "malaria",
        "tuberculosis",
        "hiv",
        "infectious disease",
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
    ],
}

# Generic IT / software roles are dropped unless an allowed tech exception appears.
TECH_JOB_PHRASES: List[str] = [
    "software engineer",
    "software developer",
    "software and database",
    "database development",
    "database developer",
    "associate software",
    "full stack",
    "full-stack",
    "backend developer",
    "frontend developer",
    "web developer",
    "devops",
    "systems administrator",
    "system administrator",
    "network engineer",
    "cyber security",
    "cybersecurity",
    "information technology",
    " IT ",
    "erp oracle",
    "oracle functional",
    "oracle technical",
    "cloud engineer",
    "data engineer",
    "programmer",
    "machine learning engineer",
    "deep learning",
    "computer scientist",
    "ICT ",
    "informatics officer",
]

# Tech postings are kept only when the text explicitly mentions these roles/topics.
TECH_ALLOWED_EXCEPTIONS: List[str] = [
    "ai evaluation",
    "qa engineer",
    "quality assurance engineer",
]

# African countries / territories commonly used on UNjobs duty-station lines.
AFRICAN_COUNTRIES: frozenset[str] = frozenset(
    {
        "algeria",
        "angola",
        "benin",
        "botswana",
        "burkina faso",
        "burundi",
        "cabo verde",
        "cameroon",
        "cape verde",
        "central african republic",
        "chad",
        "comoros",
        "congo",
        "cote d'ivoire",
        "côte d'ivoire",
        "democratic republic of the congo",
        "djibouti",
        "egypt",
        "equatorial guinea",
        "eritrea",
        "eswatini",
        "ethiopia",
        "gabon",
        "gambia",
        "ghana",
        "guinea",
        "guinea-bissau",
        "kenya",
        "lesotho",
        "liberia",
        "libya",
        "madagascar",
        "malawi",
        "mali",
        "mauritania",
        "mauritius",
        "morocco",
        "mozambique",
        "namibia",
        "niger",
        "nigeria",
        "rwanda",
        "sao tome and principe",
        "senegal",
        "seychelles",
        "sierra leone",
        "somalia",
        "south africa",
        "south sudan",
        "sudan",
        "swaziland",
        "tanzania",
        "togo",
        "tunisia",
        "uganda",
        "zambia",
        "zimbabwe",
        "the gambia",
        "drc",
    }
)

# Asia — always discarded (not in scope).
ASIAN_COUNTRIES: frozenset[str] = frozenset(
    {
        "afghanistan",
        "armenia",
        "azerbaijan",
        "bahrain",
        "bangladesh",
        "bhutan",
        "brunei",
        "brunei darussalam",
        "cambodia",
        "china",
        "georgia",
        "india",
        "indonesia",
        "iran",
        "iraq",
        "israel",
        "japan",
        "jordan",
        "kazakhstan",
        "kuwait",
        "kyrgyzstan",
        "laos",
        "lao pdr",
        "lebanon",
        "malaysia",
        "maldives",
        "mongolia",
        "myanmar",
        "nepal",
        "north korea",
        "oman",
        "pakistan",
        "palestine",
        "philippines",
        "qatar",
        "republic of korea",
        "saudi arabia",
        "singapore",
        "south korea",
        "sri lanka",
        "syria",
        "taiwan",
        "tajikistan",
        "thailand",
        "timor-leste",
        "turkey",
        "türkiye",
        "turkmenistan",
        "united arab emirates",
        "uzbekistan",
        "vietnam",
        "viet nam",
        "yemen",
    }
)

ASIAN_LOCATION_HINTS: List[str] = sorted(
    {
        "bangkok",
        "beijing",
        "dhaka",
        "hanoi",
        "ho chi minh",
        "hong kong",
        "islamabad",
        "jakarta",
        "kathmandu",
        "kuala lumpur",
        "manila",
        "mumbai",
        "new delhi",
        "seoul",
        "shanghai",
        "singapore",
        "taipei",
        "tehran",
        "tokyo",
        "yangon",
    },
    key=len,
    reverse=True,
)

# Regions kept for remote, worldwide, Nigeria-eligible roles outside Africa.
REMOTE_FOCUS_COUNTRIES: frozenset[str] = frozenset(
    {
        # North America
        "canada",
        "mexico",
        "united states",
        "usa",
        "u.s.",
        "us",
        # Western Europe
        "austria",
        "belgium",
        "denmark",
        "finland",
        "france",
        "germany",
        "greece",
        "iceland",
        "ireland",
        "italy",
        "luxembourg",
        "monaco",
        "netherlands",
        "norway",
        "portugal",
        "spain",
        "sweden",
        "switzerland",
        "united kingdom",
        # Oceania (remote worldwide only; on-site still excluded)
        "australia",
    }
)

REMOTE_FOCUS_LOCATION_HINTS: List[str] = sorted(
    {
        "athens",
        "australia",
        "berlin",
        "boston",
        "brussels",
        "california",
        "canada",
        "canberra",
        "chicago",
        "copenhagen",
        "geneva",
        "germany",
        "greece",
        "helsinki",
        "lisbon",
        "london",
        "melbourne",
        "montreal",
        "munich",
        "new york",
        "oslo",
        "ottawa",
        "paris",
        "portugal",
        "rome",
        "stockholm",
        "sydney",
        "toronto",
        "united kingdom",
        "united states",
        "usa",
        "vienna",
        "washington",
        "zurich",
    },
    key=len,
    reverse=True,
)


def _flatten_health_keywords_sorted() -> List[str]:
    """Longer health phrases first so substrings do not steal matches."""
    phrases: List[str] = []
    for items in KEYWORD_PHRASES.values():
        phrases.extend(items)
    return sorted(set(phrases), key=len, reverse=True)


HEALTH_KEYWORDS_SORTED: List[str] = _flatten_health_keywords_sorted()
TECH_JOB_PHRASES_SORTED: List[str] = sorted(TECH_JOB_PHRASES, key=len, reverse=True)

CLOSING_MS_RE = re.compile(r"var f\w+pi\s*=\s*(\d+)\s*;var f\w+pd\s*=\s*(\d+)")


def load_email_settings() -> tuple[str, str, str, str, int]:
    """
    Load SMTP settings from the environment.

    Supports ``GMAIL_USER`` / ``GMAIL_APP_PASSWORD`` / ``NOTIFY_TO_EMAIL`` and
    legacy ``SENDER_EMAIL`` / ``SENDER_PASSWORD`` / ``RECEIVER_EMAIL`` names.

    Returns:
        Tuple of sender email, app password, recipient, SMTP host, SMTP port.
    """
    sender = os.environ.get("GMAIL_USER") or os.environ.get("SENDER_EMAIL", "")
    password = os.environ.get("GMAIL_APP_PASSWORD") or os.environ.get(
        "SENDER_PASSWORD", ""
    )
    receiver = os.environ.get("NOTIFY_TO_EMAIL") or os.environ.get(
        "RECEIVER_EMAIL", ""
    )
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    return sender, password, receiver, smtp_host, smtp_port


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


def matches_health_keywords(text: str) -> bool:
    """
    Return True if ``text`` matches any health-focused keyword (groups A, B, C).

    Args:
        text: Concatenated title, organization, and optional detail body.

    Returns:
        Whether a health-related phrase was found.
    """
    lower = text.lower()
    for phrase in HEALTH_KEYWORDS_SORTED:
        if phrase.lower() in lower:
            return True
    return False


def is_tech_job(text: str) -> bool:
    """
    Detect generic IT / software postings that are outside the health focus.

    Args:
        text: Concatenated searchable fields.

    Returns:
        True if the text looks like a software or infrastructure role.
    """
    lower = f" {text.lower()} "
    for phrase in TECH_JOB_PHRASES_SORTED:
        if phrase.lower() in lower:
            return True
    if re.search(r"\bsoftware\b", lower) and re.search(
        r"\b(engineer|developer|development|consultant)\b", lower
    ):
        return True
    return False


def has_allowed_tech_exception(text: str) -> bool:
    """
    Allow select tech-adjacent roles the user cares about (AI evaluation, QA).

    Args:
        text: Concatenated searchable fields.

    Returns:
        True if AI evaluation or QA engineer phrasing is present.
    """
    lower = text.lower()
    return any(exc in lower for exc in TECH_ALLOWED_EXCEPTIONS)


def passes_focus_filter(
    text: str = "",
    *,
    title: str = "",
    organization: str = "",
    detail_text: str = "",
) -> bool:
    """
    Health-first filter: drop generic tech jobs unless they mention allowed exceptions.

    Tech detection uses **title and organization only** so long job descriptions on
    detail pages (which often mention IT systems) do not drop health roles.

    Health keywords are matched in title, organization, and optional detail text.

    Args:
        text: Legacy single blob (used when title/organization not passed separately).
        title: Vacancy title from the listing.
        organization: Organization line from the listing.
        detail_text: Optional detail-page snippet.

    Returns:
        Whether the vacancy should continue through the pipeline.
    """
    title_org = f"{title} {organization}".strip() or text
    health_blob = f"{title} {organization} {detail_text}".strip() or text

    if has_allowed_tech_exception(health_blob):
        return True
    if is_tech_job(title_org):
        return False
    return matches_health_keywords(health_blob)


def passes_focus_after_detail(job: ParsedJob) -> bool:
    """
    Re-check a listing match after detail enrichment without false tech drops.

    Rows already matched on the listing are kept unless the title/organization
    is clearly a tech role. Detail text may add keyword matches but must not
    trigger tech exclusion.

    Args:
        job: Parsed row with optional ``detail_text``.

    Returns:
        Whether the row should remain in the candidate set.
    """
    title_org = f"{job.title} {job.organization}"
    if has_allowed_tech_exception(title_org) or has_allowed_tech_exception(
        job.detail_text or ""
    ):
        return True
    if is_tech_job(title_org):
        return False
    health_blob = f"{title_org} {job.detail_text or ''}"
    return matches_health_keywords(health_blob) or matches_health_keywords(title_org)


def is_remote_work(text: str) -> bool:
    """
    Detect remote or home-based work arrangements.

    Args:
        text: Concatenated title, organization, and detail fields.

    Returns:
        True when the posting is explicitly remote or home-based.
    """
    lower = text.lower()
    return (
        re.search(r"\bremote\b", lower) is not None
        or "home-based" in lower
        or "home based" in lower
        or "work from home" in lower
        or "telework" in lower
        or "telecommute" in lower
    )


def extract_country_from_title(title: str) -> str:
    """
    Parse the trailing country from a UNjobs title line (``City, Country``).

    Args:
        title: Full vacancy title from the listing.

    Returns:
        Lowercased country string, or empty if not parseable.
    """
    if "," not in title:
        return ""
    return title.rsplit(",", 1)[-1].strip().lower()


def _job_location_blob(job: ParsedJob) -> str:
    """Lowercased title plus optional detail text for location matching."""
    return f"{job.title} {job.detail_text or ''}".lower()


def _country_in_set(country: str, allowed: frozenset[str]) -> bool:
    """Match a parsed country name against a region set (exact or substring)."""
    if not country:
        return False
    normalized = country.strip().lower()
    if normalized in allowed:
        return True
    return any(name in normalized for name in allowed if len(name) > 4)


def _blob_has_hint(blob: str, hints: List[str]) -> bool:
    """Return True if any location hint appears in the blob (longest first)."""
    return any(hint in blob for hint in hints)


def is_african_country(country: str) -> bool:
    """
    Return whether a parsed country name is treated as an African duty station.

    Args:
        country: Lowercased country from the title tail.

    Returns:
        True if the country is in the African allowlist.
    """
    return _country_in_set(country, AFRICAN_COUNTRIES)


def is_asian_location(job: ParsedJob) -> bool:
    """
    Detect Asian duty stations (always excluded from the digest).

    Args:
        job: Parsed vacancy row.

    Returns:
        True when the posting is clearly based in Asia.
    """
    country = extract_country_from_title(job.title)
    if _country_in_set(country, ASIAN_COUNTRIES):
        return True
    blob = _job_location_blob(job)
    return _blob_has_hint(blob, ASIAN_LOCATION_HINTS)


def is_african_location(job: ParsedJob) -> bool:
    """
    Detect African duty stations.

    Args:
        job: Parsed vacancy row.

    Returns:
        True when the posting is clearly based in Africa.
    """
    country = extract_country_from_title(job.title)
    if country and is_african_country(country):
        return True
    blob = _job_location_blob(job)
    africa_hints = ("nigeria", "kenya", "ethiopia", "senegal", "uganda", "angola", "ghana")
    return any(hint in blob for hint in africa_hints)


def is_remote_focus_location(job: ParsedJob) -> bool:
    """
    Detect remote-focus regions outside Africa (NA, Western Europe, Australia).

    Greece, Portugal, and Australia are included here. Such postings are only
    kept when also remote/home-based and Nigeria-eligible.

    Args:
        job: Parsed vacancy row.

    Returns:
        True when the posting is tied to an allowed remote-focus region.
    """
    country = extract_country_from_title(job.title)
    if _country_in_set(country, REMOTE_FOCUS_COUNTRIES):
        return True
    return _blob_has_hint(_job_location_blob(job), REMOTE_FOCUS_LOCATION_HINTS)


def is_international_job(job: ParsedJob) -> bool:
    """
    Detect internationally recruited UN/ agency posts (not local/national streams).

    Used for all **Africa** and **Nigeria** duty stations: only international
    grades and contracts pass (P/D, international consultant, etc.). Excludes
    national consultant, national officer, NO, GS, SSA, and local recruitment.

    Args:
        job: Parsed vacancy row.

    Returns:
        True when the vacancy is clearly internationally recruited.
    """
    title_lower = job.title.lower()
    combined = " ".join(
        fragment
        for fragment in (job.title, job.organization, job.detail_text or "")
        if fragment
    ).lower()

    if "national consultant" in title_lower and "international" not in title_lower:
        return False

    if "national officer" in combined:
        return False

    if re.search(r"\(no\)", title_lower):
        return False

    if re.search(r"\(gs\)|\(g[1-7]\)", title_lower):
        return False

    if re.search(r"\(ssa\)", title_lower):
        return False

    if "local recruitment" in combined:
        return False

    if re.search(r"\bnationals of\b", combined) and "international" not in combined:
        return False

    if re.search(r"\b(p[1-5]|d[1-2])\b", combined) or re.search(r"\([pd][1-5]\)", combined):
        return True

    if "international consultant" in combined:
        return True

    if "consultant" in combined and "national consultant" not in combined:
        return True

    if "international professional" in combined:
        return True

    if "international recruitment" in combined:
        return True

    if "full competitive recruitment : yes" in combined:
        return True

    return False


def is_nigeria_eligible(job: ParsedJob) -> bool:
    """
    Estimate whether a Nigerian applicant can apply (international or Nigeria-based).

    Blocks explicit local-only patterns. International grades (P/D), most
    consultants, and remote roles without a local-recruitment lock are treated
    as eligible.

    Args:
        job: Parsed vacancy row.

    Returns:
        True when Nigerian eligibility is plausible; False when clearly excluded.
    """
    title_lower = job.title.lower()
    combined = " ".join(
        fragment
        for fragment in (job.title, job.organization, job.detail_text or "")
        if fragment
    ).lower()

    if "nigeria" in combined:
        return True

    national_consultant_local = (
        "national consultant" in title_lower and "international" not in title_lower
    )
    if national_consultant_local:
        return False

    if "national officer" in combined and "nigeria" not in combined:
        return False

    if re.search(r"\(no\)", title_lower) and "nigeria" not in combined:
        return False

    if "local recruitment" in combined and not is_remote_work(combined):
        return False

    if re.search(r"\bnationals of\b", combined) and "nigeria" not in combined:
        return False

    if re.search(r"\b(p[1-5]|d[1-2])\b", title_lower) or re.search(
        r"\([pd][1-5]\)", title_lower
    ):
        return True

    if "international consultant" in title_lower:
        return True

    if "consultant" in title_lower and "national consultant" not in title_lower:
        return True

    if "international professional" in combined or "full competitive recruitment" in combined:
        return True

    if is_remote_work(combined):
        return "local recruitment" not in combined

    return True


def passes_nigeria_guardrail(job: ParsedJob) -> bool:
    """
    Location and Nigerian eligibility guardrail.

    - **Asia:** always discarded.
    - **Africa / Nigeria:** only **international** posts (P/D, international
      consultant, etc.); national/local/SSA/GS roles are excluded.
    - **Remote-focus regions** (North America, Western Europe, Greece, Portugal,
      Australia): kept only when **remote/home-based** and **Nigeria-eligible**.
    - **All other regions** (Latin America, Eastern Europe, Middle East, Fiji,
      on-site posts outside Africa/Australia remote-focus): discarded.

    Args:
        job: Parsed row with title, organization, optional detail text.

    Returns:
        True if the row should be included; False if excluded by location rules.
    """
    combined = " ".join(
        fragment
        for fragment in (job.title, job.organization, job.detail_text or "")
        if fragment
    )

    if is_asian_location(job):
        logger.info("Guardrail excluded (Asian duty station): %s", job.title)
        return False

    location_blob = _job_location_blob(job)
    in_africa_or_nigeria = is_african_location(job) or "nigeria" in location_blob

    if in_africa_or_nigeria:
        if is_international_job(job):
            return True
        logger.info(
            "Guardrail excluded (Africa/Nigeria, not international): %s",
            job.title,
        )
        return False

    if is_remote_focus_location(job):
        if not is_remote_work(combined):
            logger.info(
                "Guardrail excluded (remote-focus region, on-site): %s",
                job.title,
            )
            return False
        if not is_nigeria_eligible(job):
            logger.info(
                "Guardrail excluded (remote-focus region, Nigeria not eligible): %s",
                job.title,
            )
            return False
        return True

    logger.info(
        "Guardrail excluded (outside Africa; not remote-focus region): %s",
        job.title,
    )
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
    sender, password, receiver, smtp_host, smtp_port = load_email_settings()

    start_url = fix_legacy_latest_listing_url(start_url)

    session = requests.Session()
    all_jobs = scrape_listings(session, start_url, max_pages, delay)

    list_matches = [
        job
        for job in all_jobs
        if passes_focus_filter(f"{job.title} {job.organization}")
    ]
    logger.info("Health/focus matches on listing text: %s", len(list_matches))

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

    full_text_matches: List[ParsedJob] = [
        job for job in list_matches if passes_focus_after_detail(job)
    ]
    dropped_after_detail = len(list_matches) - len(full_text_matches)
    if dropped_after_detail:
        logger.info(
            "Dropped %s listing match(es) after detail (tech in title/org only)",
            dropped_after_detail,
        )
    logger.info("Health/focus matches after detail text: %s", len(full_text_matches))

    gated = [job for job in full_text_matches if passes_nigeria_guardrail(job)]
    logger.info("Rows after eligibility guardrail: %s", len(gated))

    state = load_state(state_path)
    if force_resend:
        fresh_jobs = gated
    else:
        fresh_jobs = [job for job in gated if job.url not in state]

    logger.info("New vacancies (not in state file): %s", len(fresh_jobs))

    if dry_run:
        if fresh_jobs:
            print(f"\n--- {len(fresh_jobs)} new match(es) for digest ---\n")
            for job in fresh_jobs:
                print(f"{job.title}\n  {job.url}\n  {job.organization}\n")
        else:
            print("\n--- No new matches in this run ---\n")
            print(
                f"Pipeline: scraped={len(all_jobs)}, "
                f"health_listing={len(list_matches)}, "
                f"after_detail={len(full_text_matches)}, "
                f"after_guardrail={len(gated)}, "
                f"new_vs_state={len(fresh_jobs)}"
            )
            if full_text_matches and not gated:
                print("\nMatched health filters but excluded by location/eligibility:\n")
                for job in full_text_matches:
                    print(f"  - {job.title}")
        return 0

    if not fresh_jobs:
        logger.info("Nothing new to email.")
        return 0

    if not sender or not password or not receiver:
        logger.error(
            "Set GMAIL_USER, GMAIL_APP_PASSWORD, and NOTIFY_TO_EMAIL "
            "(or SENDER_EMAIL / SENDER_PASSWORD / RECEIVER_EMAIL) for email delivery."
        )
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
        default=int(os.environ.get("MAX_PAGES", "20")),
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
