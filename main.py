import asyncio
import csv
import io
import json
import logging
import os
import re
import tempfile
import time
import urllib.request
from html import unescape
from collections import deque
from threading import Lock
from urllib.parse import parse_qs, quote_plus, urldefrag, urljoin, urlparse

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, HttpUrl
from dotenv import load_dotenv
from scrapling.fetchers import Fetcher
from scrapling.engines._browsers._stealth import StealthySession
from scrapling.engines.toolbelt.ad_domains import AD_DOMAINS

try:
    import pydub
except ImportError:  # pragma: no cover
    pydub = None

try:
    import speech_recognition as sr
except ImportError:  # pragma: no cover
    sr = None

load_dotenv()

app = FastAPI()
logger = logging.getLogger("scrapling_api")

SALARY_PATTERN = re.compile(
    r"""
    \$\s?\d[\d,]*(?:\.\d{1,2})?(?:[KMB])?
    (?:
        \s*(?:-|to|–|—)\s*\$?\s?\d[\d,]*(?:\.\d{1,2})?(?:[KMB])?
    )?
    (?:
        \s*(?:/|per\s+|an\s+|a\s+)?
        (?:hr|hour|hourly|yr|year|yearly|annually|annual|mo|month|monthly|wk|week|weekly|day|daily)
    )?
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)

TITLE_SPLIT_PATTERN = re.compile(r"\s+[|\-–—:]\s+")
CURRENCY_SYMBOLS = {
    "USD": "$",
    "AUD": "A$",
    "CAD": "C$",
    "EUR": "EUR ",
    "GBP": "GBP ",
}
BLOCKED_TITLE_PATTERNS = (
    "just a moment",
    "attention required",
    "access denied",
)
BLOCKED_BODY_PATTERNS = (
    "please enable cookies",
    "verify you are human",
    "checking your browser",
    "checking if the site connection is secure",
    "please wait while we verify you are human",
    "performance & security by cloudflare",
    "sorry, you have been blocked",
    "ray id:",
    "verification required",
    "slide right to secure your access",
    "we detected unusual activity from your device or network",
    "rapid taps or clicks",
    "automated (bot) activity on your network",
    "reason=bot-detection",
    "complete the security check",
    "unusual traffic from your computer network",
)
SALARY_LABEL_PATTERNS = (
    "summary pay range",
    "pay range",
    "salary range",
    "compensation",
    "pay:",
    "salary:",
)
NOISY_SALARY_CONTEXT_PATTERNS = (
    "image:",
    "share this job",
    "most popular",
    "job seekers",
    "small & medium businesses",
    "enterprise businesses",
    "partner with us",
    "company",
    "frequently asked questions",
)
DIRECT_DYNAMIC_DOMAINS = (
    "ziprecruiter.com",
    "glassdoor.com",
    "jobcase.com",
    "snagajob.com",
    "job.com",
    "remotive.com",
    "archinect.com"
)
GOOGLE_RESULT_EXCLUDED_HOSTS = (
    "google.com",
    "www.google.com",
    "accounts.google.com",
    "support.google.com",
    "maps.google.com",
    "policies.google.com",
)
GOOGLE_RECAPTCHA_MAX_ATTEMPTS = 3
PROMPT_RESPONSE_STATUS_CODES = {401, 429}
CLOUDFLARE_MAX_CHALLENGE_ROUNDS = 2
CLOUDFLARE_CHALLENGE_TIMEOUT_MS = 240_000
CLOUDFLARE_CHALLENGE_READY_TIMEOUT_MS = 30_000
CLOUDFLARE_CHALLENGE_ROUND_1_SETTLE_MS = 60_000
CLOUDFLARE_CHALLENGE_ROUND_2_SETTLE_MS = 60_000
BROWSER_BLOCKED_RESOURCE_TYPES = {
    "font",
    "image",
    "media",
    "beacon",
    "object",
    "imageset",
    "texttrack",
    "websocket",
    "csp_report",
    "stylesheet",
}
STEALTH_PROXY_POOL: deque[dict[str, str]] = deque()
STEALTH_PROXY_LOCK = Lock()
HTML_REMOVAL_PATTERNS = (
    re.compile(r"<!--.*?-->", flags=re.DOTALL),
    re.compile(r"<script\b[^>]*>.*?</script>", flags=re.IGNORECASE | re.DOTALL),
    re.compile(r"<style\b[^>]*>.*?</style>", flags=re.IGNORECASE | re.DOTALL),
    re.compile(r"<noscript\b[^>]*>.*?</noscript>", flags=re.IGNORECASE | re.DOTALL),
    re.compile(r"<iframe\b[^>]*>.*?</iframe>", flags=re.IGNORECASE | re.DOTALL),
    re.compile(r"<svg\b[^>]*>.*?</svg>", flags=re.IGNORECASE | re.DOTALL),
    re.compile(r"<template\b[^>]*>.*?</template>", flags=re.IGNORECASE | re.DOTALL),
    re.compile(r"<canvas\b[^>]*>.*?</canvas>", flags=re.IGNORECASE | re.DOTALL),
)
HTML_TAG_COMPACT_PATTERN = re.compile(r">\s+<")
HTML_WHITESPACE_PATTERN = re.compile(r"\s{2,}")
HTML_ATTRIBUTED_BOILERPLATE_PATTERN = re.compile(
    r"<(?P<tag>[a-z0-9]+)\b[^>]*\b(?:id|class|data-testid|aria-label)\s*=\s*"
    r'(?:"[^"]*(cookie|consent|banner|modal|popup|overlay|newsletter|subscribe|sign[\s_-]?in|login)[^"]*"'
    r"|'[^']*(cookie|consent|banner|modal|popup|overlay|newsletter|subscribe|sign[\s_-]?in|login)[^']*')[^>]*>"
    r".*?</(?P=tag)>",
    flags=re.IGNORECASE | re.DOTALL,
)
MAX_CLEAN_HTML_LENGTH = 100_000


class ScrapeRequest(BaseModel):
    url: HttpUrl
    dynamic: bool = False


class BatchScrapeRequest(BaseModel):
    urls: list[HttpUrl]
    dynamic: bool = False
    concurrency: int = 5


class JobLinkSearchRequest(BaseModel):
    start_url: HttpUrl
    job_title: str
    location: str | None = None
    dynamic: bool = False
    max_pages: int = 25


class BatchScrapeTestRequest(BaseModel):
    urls: list[HttpUrl]
    concurrency: int = 5


class GoogleSearchRequest(BaseModel):
    query: str
    limit: int = 10
    dynamic: bool = False
    solve_recaptcha: bool = False


def configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="[%(asctime)s] %(levelname)s: %(message)s",
        )


configure_logging()


class PlaywrightRecaptchaSolver:
    """Solve Google reCAPTCHA audio challenges on a Playwright page."""

    TIMEOUT_STANDARD_MS = 7000
    TIMEOUT_SHORT_MS = 1000
    TIMEOUT_DETECTION_MS = 500

    def __init__(self, page) -> None:
        self.page = page

    def solve_captcha(self) -> None:
        logger.info("recaptcha solve start url=%s", self.page.url)
        self._log_frame_inventory("recaptcha solve initial")

        recaptcha_iframe = self.page.frame_locator("iframe[title='reCAPTCHA']")
        recaptcha_iframe.locator(".rc-anchor-content").first.wait_for(
            state="visible",
            timeout=self.TIMEOUT_STANDARD_MS,
        )
        recaptcha_iframe.locator(".rc-anchor-content").first.click()
        self.page.wait_for_timeout(300)

        if self.is_solved():
            logger.info("recaptcha solved by checkbox url=%s", self.page.url)
            return

        challenge_frame = self._get_challenge_frame()
        if challenge_frame is None:
            self._log_frame_inventory("recaptcha challenge frame missing")
            raise RuntimeError("recaptcha challenge frame not found")

        logger.info(
            "recaptcha challenge frame selected name=%s url=%s",
            challenge_frame.name or "",
            challenge_frame.url or "",
        )
        challenge_frame.locator("#recaptcha-audio-button").wait_for(
            state="visible",
            timeout=self.TIMEOUT_STANDARD_MS,
        )
        challenge_frame.locator("#recaptcha-audio-button").click()
        self.page.wait_for_timeout(500)

        if self.is_detected(challenge_frame):
            raise RuntimeError("captcha detected bot behavior")

        audio_url = self._get_audio_source_url(challenge_frame)
        if not audio_url:
            raise RuntimeError("recaptcha audio source missing")

        text_response = self._process_audio_challenge(audio_url)
        challenge_frame.locator("#audio-response").fill(text_response.lower())
        challenge_frame.locator("#recaptcha-verify-button").click()
        self.page.wait_for_timeout(1000)

        if not self.is_solved():
            raise RuntimeError("failed to solve the captcha")

        logger.info("recaptcha solve success url=%s", self.page.url)

    def _get_audio_source_url(self, challenge_frame) -> str:
        audio_source = challenge_frame.locator("#audio-source").first
        audio_source.wait_for(state="attached", timeout=self.TIMEOUT_STANDARD_MS)

        started_at = time.perf_counter()
        while (time.perf_counter() - started_at) * 1000 < self.TIMEOUT_STANDARD_MS:
            try:
                audio_url = audio_source.get_attribute("src") or ""
            except Exception:
                audio_url = ""

            if audio_url:
                return audio_url

            self.page.wait_for_timeout(250)

        return ""

    def _get_challenge_frame(self):
        for frame in self.page.frames:
            title = frame.name or ""
            url = frame.url or ""
            lower_title = title.lower()
            lower_url = url.lower()
            if (
                "recaptcha" in lower_title
                or "recaptcha" in lower_url
                or "google.com/recaptcha" in lower_url
            ):
                try:
                    if (
                        frame.locator("#recaptcha-audio-button").count()
                        or frame.locator("#audio-response").count()
                        or frame.locator("#audio-source").count()
                        or frame.locator("#recaptcha-verify-button").count()
                    ):
                        return frame
                except Exception:
                    continue
        return None

    def _log_frame_inventory(self, context: str) -> None:
        frames = []
        for frame in self.page.frames:
            try:
                frames.append(
                    {
                        "name": frame.name or "",
                        "url": frame.url or "",
                    }
                )
            except Exception:
                continue
        logger.info("recaptcha frames context=%s frames=%s", context, frames)

    def _process_audio_challenge(self, audio_url: str) -> str:
        if pydub is None or sr is None:
            raise RuntimeError(
                "audio captcha dependencies are missing; install pydub and SpeechRecognition"
            )

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as mp3_file:
            mp3_path = mp3_file.name
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as wav_file:
            wav_path = wav_file.name

        try:
            urllib.request.urlretrieve(audio_url, mp3_path)
            sound = pydub.AudioSegment.from_mp3(mp3_path)
            sound.export(wav_path, format="wav")

            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_path) as source:
                audio = recognizer.record(source)

            return recognizer.recognize_google(audio)
        finally:
            for path in (mp3_path, wav_path):
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    def is_solved(self) -> bool:
        try:
            checkbox = self.page.frame_locator("iframe[title='reCAPTCHA']").locator(
                "#recaptcha-anchor"
            ).first
            checkbox.wait_for(state="attached", timeout=self.TIMEOUT_SHORT_MS)
            return checkbox.get_attribute("aria-checked") == "true"
        except Exception:
            return False

    def is_detected(self, challenge_frame=None) -> bool:
        frame = challenge_frame or self._get_challenge_frame()
        if frame is None:
            return False

        try:
            locator = frame.get_by_text("Try again later", exact=False).first
            locator.wait_for(state="visible", timeout=self.TIMEOUT_DETECTION_MS)
            return True
        except Exception:
            return False


def maybe_solve_google_recaptcha(page) -> None:
    try:
        has_recaptcha_frame = page.locator("iframe[title='reCAPTCHA']").count() > 0
    except Exception:
        has_recaptcha_frame = False

    if "sorry/index" not in page.url and not has_recaptcha_frame:
        return

    logger.warning("google recaptcha challenge detected url=%s", page.url)
    solver = PlaywrightRecaptchaSolver(page)
    solver.solve_captcha()
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1000)


def detect_google_block(page) -> str:
    page_url = clean_text(getattr(page, "url", ""))
    status_code = get_page_status_code(page)

    if "google.com/sorry/index" in page_url:
        return "google_recaptcha_unsolved"
    if status_code == 403 and "google." in page_url:
        return "google_forbidden"
    if status_code == 429 and "google." in page_url:
        return "google_rate_limited"

    return ""


def clean_text(text: str) -> str:
    text = (text or "").replace("Ã¢â‚¬â€œ", " - ").replace("â€“", " - ").replace("â€”", " - ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_title(text: str) -> str:
    text = clean_text(text)
    if not text:
        return ""
    return TITLE_SPLIT_PATTERN.split(text, maxsplit=1)[0].strip()


def normalize_title_match(text: str) -> str:
    return re.sub(r"\s+", " ", normalize_title(text)).strip().casefold()


def normalize_location_match(text: str) -> str:
    text = clean_text(text)
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def format_amount(value) -> str:
    if value is None:
        return ""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return clean_text(str(value))

    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def format_salary_unit(unit: str) -> str:
    unit = clean_text(unit).lower()
    if unit in {"hour", "hourly"}:
        return " per hour"
    if unit in {"year", "yearly", "annual", "annually"}:
        return " per year"
    if unit in {"month", "monthly"}:
        return " per month"
    if unit in {"week", "weekly"}:
        return " per week"
    if unit in {"day", "daily"}:
        return " per day"
    return f" per {unit}" if unit else ""


def format_structured_salary(data: dict) -> str:
    if not isinstance(data, dict):
        return ""

    currency = clean_text(data.get("currency") or data.get("salaryCurrency") or "USD").upper()
    symbol = CURRENCY_SYMBOLS.get(currency, f"{currency} ")

    value = data.get("value", data)
    if isinstance(value, list):
        value = value[0] if value else {}

    if not isinstance(value, dict):
        amount = format_amount(value)
        return f"{symbol}{amount}" if amount else ""

    min_value = value.get("minValue")
    max_value = value.get("maxValue")
    exact_value = value.get("value")
    unit = format_salary_unit(value.get("unitText") or data.get("unitText") or "")

    if min_value is not None and max_value is not None:
        return f"{symbol}{format_amount(min_value)} - {symbol}{format_amount(max_value)}{unit}"
    if min_value is not None:
        return f"{symbol}{format_amount(min_value)}{unit}"
    if max_value is not None:
        return f"{symbol}{format_amount(max_value)}{unit}"
    if exact_value is not None:
        return f"{symbol}{format_amount(exact_value)}{unit}"
    return ""


def iter_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for item in value:
            yield from iter_dicts(item)


def extract_jobposting_data(page) -> tuple[str, str, str]:
    try:
        scripts = page.css('script[type="application/ld+json"]::text').getall()
    except Exception:
        return "", "", ""

    for script in scripts:
        try:
            payload = json.loads(script)
        except Exception:
            continue

        for item in iter_dicts(payload):
            item_type = item.get("@type", "")
            if isinstance(item_type, list):
                item_type = " ".join(str(part) for part in item_type)

            if "JobPosting" not in str(item_type):
                continue

            title = normalize_title(item.get("title", ""))
            salary = format_structured_salary(item.get("baseSalary", {}))
            location = extract_structured_job_location(item)

            if not salary:
                salary = format_structured_salary(item.get("estimatedSalary", {}))

            if title or salary or location:
                return title, clean_text(salary), location

    return "", "", ""


def extract_structured_job_location(item: dict) -> str:
    job_location = item.get("jobLocation")
    if isinstance(job_location, list):
        job_location = job_location[0] if job_location else {}

    if not isinstance(job_location, dict):
        return ""

    address = job_location.get("address", {})
    if not isinstance(address, dict):
        return ""

    parts = [
        address.get("addressLocality"),
        address.get("addressRegion"),
        address.get("addressCountry"),
    ]
    return clean_text(", ".join(str(part) for part in parts if part))


def host_matches_domain(host: str, domain: str) -> bool:
    normalized_host = host.lower()
    normalized_domain = domain.lower()
    return normalized_host == normalized_domain or normalized_host.endswith(f".{normalized_domain}")


def should_force_dynamic(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host_matches_domain(host, domain) for domain in DIRECT_DYNAMIC_DOMAINS)


def extract_title(page) -> str:
    selectors = [
        "h1::text",
        'meta[property="og:title"]::attr(content)',
        'meta[name="twitter:title"]::attr(content)',
        "title::text",
    ]

    for selector in selectors:
        try:
            value = page.css(selector).get()
        except Exception:
            value = ""

        value = normalize_title(value or "")
        if value:
            return value

    return ""


def extract_body_text(page) -> str:
    try:
        body_parts = page.css("body ::text").getall()
    except Exception:
        body_parts = []

    return clean_text(" ".join(body_parts))


def extract_location_from_body(body_text: str) -> str:
    patterns = (
        r"job location[:\s]+([a-zA-Z0-9\s,.-]{2,80})",
        r"location[:\s]+([a-zA-Z0-9\s,.-]{2,80})",
        r"based in\s+([a-zA-Z0-9\s,.-]{2,80})",
    )

    for pattern in patterns:
        match = re.search(pattern, body_text, flags=re.IGNORECASE)
        if match:
            return clean_text(match.group(1))

    return ""


def find_salary_in_labeled_sections(body_text: str) -> str:
    normalized_body = clean_text(body_text)
    normalized_lower = normalized_body.lower()

    for label in SALARY_LABEL_PATTERNS:
        start = normalized_lower.find(label)
        if start == -1:
            continue

        snippet = normalized_body[start:start + 300]
        salary_match = SALARY_PATTERN.search(snippet)
        if salary_match:
            return clean_text(salary_match.group(0))

    return ""


def limit_to_job_section(body_text: str) -> str:
    normalized_body = clean_text(body_text)
    normalized_lower = normalized_body.lower()

    start_markers = ("job description", "position responsibilities", "company:")
    end_markers = (
        "what employees say",
        "share this job",
        "most popular",
        "frequently asked questions",
        "job seekers",
        "small & medium businesses",
        "enterprise businesses",
        "partner with us",
    )

    start_positions = [normalized_lower.find(marker) for marker in start_markers if normalized_lower.find(marker) != -1]
    start_index = min(start_positions) if start_positions else 0

    end_positions = [normalized_lower.find(marker, start_index) for marker in end_markers if normalized_lower.find(marker, start_index) != -1]
    end_index = min(end_positions) if end_positions else len(normalized_body)

    return normalized_body[start_index:end_index]


def extract_salary_near_title(body_text: str, title: str) -> str:
    if not body_text:
        return ""

    labeled_salary = find_salary_in_labeled_sections(body_text)
    if labeled_salary:
        return labeled_salary

    job_section = limit_to_job_section(body_text)
    normalized_title = clean_text(title)
    if normalized_title:
        for match in re.finditer(re.escape(normalized_title), job_section, flags=re.IGNORECASE):
            start = max(0, match.start() - 400)
            end = min(len(job_section), match.end() + 600)
            nearby = job_section[start:end]
            nearby_lower = nearby.lower()
            if any(pattern in nearby_lower for pattern in NOISY_SALARY_CONTEXT_PATTERNS):
                continue
            salary_match = SALARY_PATTERN.search(nearby)
            if salary_match:
                return clean_text(salary_match.group(0))

    salary_match = SALARY_PATTERN.search(job_section)
    if salary_match:
        snippet_start = max(0, salary_match.start() - 80)
        snippet_end = min(len(job_section), salary_match.end() + 80)
        snippet = job_section[snippet_start:snippet_end].lower()
        if not any(pattern in snippet for pattern in NOISY_SALARY_CONTEXT_PATTERNS):
            return clean_text(salary_match.group(0))

    return ""


def detect_blocked_page(page, title: str, body_text: str) -> str:
    page_meta = getattr(page, "meta", {})
    if isinstance(page_meta, dict) and page_meta.get("cloudflare_challenge_exhausted"):
        return "cloudflare_challenge_exhausted"

    page_url = clean_text(getattr(page, "url", "")).lower()
    normalized_title = clean_text(title).lower()
    normalized_body = clean_text(body_text).lower()

    if "glassdoor.com/member/profile/login" in page_url and "reason=bot-detection" in page_url:
        return "blocked_by_bot_detection"
    if "glassdoor.com/member/profile/login" in page_url:
        return "blocked_by_login"

    for pattern in BLOCKED_TITLE_PATTERNS:
        if pattern in normalized_title:
            return "blocked_by_cloudflare"

    for pattern in BLOCKED_BODY_PATTERNS:
        if pattern in normalized_body:
            return "blocked_by_cloudflare"

    return ""


def is_cloudflare_challenge_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    return hostname == "challenges.cloudflare.com" or parsed.path.startswith("/cdn-cgi/challenge-platform/")


def is_ad_domain(hostname: str) -> bool:
    hostname = hostname.lower().strip(".")
    while hostname:
        if hostname in AD_DOMAINS:
            return True
        _, separator, hostname = hostname.partition(".")
        if not separator:
            break
    return False


def load_stealth_proxies(raw_proxies: str | None = None) -> deque[dict[str, str]]:
    """Parse STEALTH_PROXIES entries in host:port:username:password format."""
    raw_proxies = raw_proxies if raw_proxies is not None else os.getenv("STEALTH_PROXIES", "")
    proxies: deque[dict[str, str]] = deque()

    for entry in re.split(r"[\s,]+", raw_proxies.strip()):
        if not entry:
            continue

        host, separator, credentials = entry.partition(":")
        port, separator_2, credentials = credentials.partition(":")
        username, separator_3, password = credentials.partition(":")
        if not (host and separator and port.isdigit() and separator_2 and username and separator_3 and password):
            raise ValueError(
                "Each STEALTH_PROXIES entry must use host:port:username:password format"
            )

        proxies.append(
            {
                "server": f"http://{host}:{port}",
                "username": username,
                "password": password,
            }
        )

    return proxies


def get_next_stealth_proxy() -> dict[str, str] | None:
    """Return the next configured proxy without logging credentials."""
    with STEALTH_PROXY_LOCK:
        if not STEALTH_PROXY_POOL:
            STEALTH_PROXY_POOL.extend(load_stealth_proxies())
        if not STEALTH_PROXY_POOL:
            return None

        proxy = STEALTH_PROXY_POOL[0]
        STEALTH_PROXY_POOL.rotate(-1)
        return proxy.copy()


def setup_browser_request_blocking(page) -> None:
    def handle_route(route) -> None:
        request = route.request
        if is_cloudflare_challenge_url(request.url):
            route.continue_()
            return

        hostname = urlparse(request.url).hostname or ""
        if request.resource_type in BROWSER_BLOCKED_RESOURCE_TYPES or is_ad_domain(hostname):
            route.abort()
            return

        route.continue_()

    page.route("**/*", handle_route)


class BoundedCloudflareSession(StealthySession):
    """Run Cloudflare challenges without Scrapling's recursive retry loop."""

    def __init__(self, *args, **kwargs):
        self.cloudflare_challenge_attempts = 0
        self.cloudflare_challenge_exhausted = False
        super().__init__(*args, **kwargs)

    @staticmethod
    def _has_cloudflare_challenge(page) -> bool:
        try:
            if any("challenges.cloudflare.com" in (frame.url or "").lower() for frame in page.frames):
                return True
        except Exception:
            pass

        try:
            title = (page.title() or "").lower()
            if "just a moment" in title or "verifying you are human" in title:
                return True
        except Exception:
            pass

        try:
            content = (page.content() or "").lower()
            return any(
                marker in content
                for marker in (
                    "cf-chl-widget",
                    "cf-challenge-running",
                    "challenge-platform/h/",
                    "verify you are human",
                )
            )
        except Exception:
            return False

    @staticmethod
    def _is_cloudflare_checkbox_ready(page) -> bool:
        challenge_frames = []
        try:
            challenge_frames = [
                frame
                for frame in page.frames
                if "challenges.cloudflare.com" in (frame.url or "").lower()
            ]
        except Exception:
            pass

        for frame in reversed(challenge_frames):
            for selector in ("input[type='checkbox']", "[role='checkbox']"):
                try:
                    checkbox = frame.locator(selector).first
                    if checkbox.count() and checkbox.is_visible():
                        return True
                except Exception:
                    continue

        return False

    def _wait_for_cloudflare_widget_ready(self, page, deadline: float) -> bool:
        """Wait for a visible Turnstile checkbox before dispatching a click."""
        ready_deadline = min(
            deadline,
            time.perf_counter() + (CLOUDFLARE_CHALLENGE_READY_TIMEOUT_MS / 1000),
        )

        while time.perf_counter() < ready_deadline:
            if self._is_cloudflare_checkbox_ready(page):
                return True

            remaining_ms = max(1, int((ready_deadline - time.perf_counter()) * 1000))
            page.wait_for_timeout(min(250, remaining_ms))

        return self._is_cloudflare_checkbox_ready(page)

    @staticmethod
    def _click_cloudflare_challenge(page, timeout_ms: int) -> bool:
        challenge_frames = []
        try:
            challenge_frames = [
                frame
                for frame in page.frames
                if "challenges.cloudflare.com" in (frame.url or "").lower()
            ]
        except Exception:
            pass

        for frame in reversed(challenge_frames):
            for selector in ("input[type='checkbox']", "[role='checkbox']"):
                try:
                    checkbox = frame.locator(selector).first
                    checkbox.wait_for(state="visible", timeout=timeout_ms)
                    checkbox.click(timeout=timeout_ms)
                    return True
                except Exception:
                    continue

            try:
                box = frame.frame_element().bounding_box()
                if box:
                    page.mouse.click(box["x"] + 27, box["y"] + 27, delay=150)
                    return True
            except Exception:
                continue

        return False

    def _wait_for_cloudflare_clearance(
        self,
        page,
        deadline: float,
        settle_timeout_ms: int,
    ) -> bool:
        """Poll until Cloudflare leaves the interstitial, or the settle window ends."""
        settle_deadline = min(
            deadline,
            time.perf_counter() + (settle_timeout_ms / 1000),
        )

        while time.perf_counter() < settle_deadline:
            if not self._has_cloudflare_challenge(page):
                return True

            remaining_ms = max(1, int((settle_deadline - time.perf_counter()) * 1000))
            page.wait_for_timeout(min(250, remaining_ms))

        return not self._has_cloudflare_challenge(page)

    def _cloudflare_solver(self, page) -> None:
        deadline = time.perf_counter() + (CLOUDFLARE_CHALLENGE_TIMEOUT_MS / 1000)

        while self._has_cloudflare_challenge(page):
            if (
                self.cloudflare_challenge_attempts >= CLOUDFLARE_MAX_CHALLENGE_ROUNDS
                or time.perf_counter() >= deadline
            ):
                self.cloudflare_challenge_exhausted = True
                logger.warning(
                    "cloudflare challenge exhausted attempts=%s url=%s",
                    self.cloudflare_challenge_attempts,
                    page.url,
                )
                return

            self.cloudflare_challenge_attempts += 1
            remaining_ms = max(1, int((deadline - time.perf_counter()) * 1000))
            click_timeout_ms = min(2_000, remaining_ms)
            logger.warning(
                "cloudflare challenge round=%s/%s waiting_for_widget url=%s",
                self.cloudflare_challenge_attempts,
                CLOUDFLARE_MAX_CHALLENGE_ROUNDS,
                page.url,
            )

            page.wait_for_timeout(min(2_000, remaining_ms))

            widget_ready = self._wait_for_cloudflare_widget_ready(page, deadline)
            click_dispatched = (
                self._click_cloudflare_challenge(page, click_timeout_ms)
                if widget_ready
                else False
            )
            logger.warning(
                "cloudflare challenge round=%s/%s widget_ready=%s click_dispatched=%s url=%s",
                self.cloudflare_challenge_attempts,
                CLOUDFLARE_MAX_CHALLENGE_ROUNDS,
                widget_ready,
                click_dispatched,
                page.url,
            )

            settle_timeout_ms = (
                CLOUDFLARE_CHALLENGE_ROUND_1_SETTLE_MS
                if self.cloudflare_challenge_attempts == 1
                else CLOUDFLARE_CHALLENGE_ROUND_2_SETTLE_MS
            )
            if click_dispatched and self._wait_for_cloudflare_clearance(
                page,
                deadline,
                settle_timeout_ms,
            ):
                logger.info(
                    "cloudflare challenge cleared attempts=%s url=%s",
                    self.cloudflare_challenge_attempts,
                    page.url,
                )
                return


def fetch_stealthy_page(
    url: str,
    wait: int = 2000,
    solve_recaptcha: bool = False,
):
    page_action = maybe_solve_google_recaptcha if solve_recaptcha else None
    solve_cloudflare = "google." not in urlparse(url).netloc.lower()
    proxy = get_next_stealth_proxy()
    if proxy:
        proxy_host = urlparse(proxy["server"]).hostname or "configured"
        provider = "decodo" if proxy_host.endswith("decodo.com") else "custom"
        logger.info("stealth proxy selected provider=%s host=%s", provider, proxy_host)
    with BoundedCloudflareSession(
        headless=True,
        solve_cloudflare=solve_cloudflare,
        disable_resources=False,
        block_ads=False,
        network_idle=False,
        timeout=90000,
        wait=wait,
        retries=1,
        dns_over_https=True,
        block_webrtc=True,
        load_dom=True,
        page_action=page_action,
        page_setup=setup_browser_request_blocking,
        proxy=proxy,
    ) as session:
        response = session.fetch(url)
        response.meta["cloudflare_challenge_attempts"] = session.cloudflare_challenge_attempts
        response.meta["cloudflare_challenge_exhausted"] = session.cloudflare_challenge_exhausted
        return response


def summarize_page(page) -> dict:
    summary = {}

    for attribute in ("status", "status_code", "url"):
        value = getattr(page, attribute, None)
        if value:
            summary[attribute] = value

    return summary


def get_page_status_code(page) -> int | None:
    for attribute in ("status_code", "status"):
        value = getattr(page, attribute, None)
        if isinstance(value, int):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def extract_links(page, base_url: str) -> list[str]:
    try:
        hrefs = page.css("a::attr(href)").getall()
    except Exception:
        hrefs = []

    base_host = urlparse(base_url).netloc.lower()
    links: list[str] = []
    seen: set[str] = set()

    for href in hrefs:
        href = clean_text(href)
        if not href:
            continue

        absolute_url = urldefrag(urljoin(base_url, href)).url
        parsed = urlparse(absolute_url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc.lower() != base_host:
            continue
        if absolute_url in seen:
            continue

        seen.add(absolute_url)
        links.append(absolute_url)

    return links


def extract_google_result_url(href: str) -> str:
    href = clean_text(href)
    if not href:
        return ""

    if href.startswith("/url?"):
        parsed = urlparse(href)
        href = parse_qs(parsed.query).get("q", [""])[0]

    parsed = urlparse(href)
    if parsed.scheme not in {"http", "https"}:
        return ""
    if parsed.netloc.lower() in GOOGLE_RESULT_EXCLUDED_HOSTS:
        return ""

    return urldefrag(href).url


def extract_google_result_snippet(result_card) -> str:
    selectors = (
        ".VwiC3b::text",
        ".VwiC3b *::text",
        ".yXK7lf::text",
        ".yXK7lf *::text",
        ".s3v9rd::text",
        ".s3v9rd *::text",
    )

    for selector in selectors:
        try:
            texts = result_card.css(selector).getall()
        except Exception:
            texts = []

        snippet = clean_text(" ".join(texts))
        if snippet:
            return snippet

    return ""


def extract_google_result_title(result_card) -> str:
    selectors = (
        "h3::text",
        ".LC20lb::text",
        "a h3::text",
    )

    for selector in selectors:
        try:
            title = clean_text(" ".join(result_card.css(selector).getall()))
        except Exception:
            title = ""
        if title:
            return title

    return ""


def extract_google_result_link(result_card) -> str:
    try:
        hrefs = result_card.css("a[href]::attr(href)").getall()
    except Exception:
        hrefs = []

    for href in hrefs:
        normalized = extract_google_result_url(href)
        if normalized:
            return normalized

    return ""


def build_google_search_url(query: str, limit: int) -> str:
    return f"https://www.google.com/search?q={quote_plus(query)}&num={limit}&hl=en"


def search_google(query: str, limit: int = 10, dynamic: bool = False, solve_recaptcha: bool = False) -> dict:
    normalized_query = clean_text(query)
    if not normalized_query:
        return {
            "query": query,
            "results": [],
            "total": 0,
            "error": "query is empty",
        }

    limit = max(1, min(limit, 20))
    search_url = build_google_search_url(normalized_query, limit)
    started_at = time.perf_counter()

    logger.info(
        "google search start query=%s limit=%s dynamic=%s solve_recaptcha=%s",
        normalized_query,
        limit,
        dynamic,
        solve_recaptcha,
    )

    try:
        max_attempts = GOOGLE_RECAPTCHA_MAX_ATTEMPTS if solve_recaptcha else 1
        attempts = 0

        while True:
            attempts += 1
            page = fetch_page(
                search_url,
                dynamic=dynamic,
                solve_recaptcha=solve_recaptcha,
            )
            status_code = get_page_status_code(page)
            blocked_error = detect_google_block(page)
            should_retry = (
                blocked_error == "google_recaptcha_unsolved"
                and solve_recaptcha
                and attempts < max_attempts
            )

            if should_retry:
                logger.warning(
                    "google search retry query=%s attempt=%s/%s status=%s error=%s",
                    normalized_query,
                    attempts,
                    max_attempts,
                    status_code,
                    blocked_error,
                )
                continue

            if blocked_error:
                duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
                logger.warning(
                    "google search blocked query=%s attempts=%s status=%s error=%s duration_ms=%s",
                    normalized_query,
                    attempts,
                    status_code,
                    blocked_error,
                    duration_ms,
                )
                return {
                    "query": normalized_query,
                    "results": [],
                    "total": 0,
                    "limit": limit,
                    "status_code": status_code,
                    "source": search_url,
                    "duration_ms": duration_ms,
                    "attempts": attempts,
                    "error": blocked_error,
                }

            break

        results: list[dict] = []
        seen: set[str] = set()

        try:
            result_cards = page.css("div.tF2Cxc")
        except Exception:
            result_cards = []

        for result_card in result_cards:
            try:
                href = extract_google_result_link(result_card)
                if not href or href in seen:
                    continue

                title = extract_google_result_title(result_card)
                if not title:
                    continue

                snippet = extract_google_result_snippet(result_card)

                seen.add(href)
                results.append(
                    {
                        "title": title,
                        "url": href,
                        "snippet": snippet,
                    }
                )
                if len(results) >= limit:
                    break
            except Exception:
                continue

        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        logger.info(
            "google search complete query=%s total=%s attempts=%s status=%s duration_ms=%s",
            normalized_query,
            len(results),
            attempts,
            status_code,
            duration_ms,
        )

        return {
            "query": normalized_query,
            "results": results,
            "total": len(results),
            "limit": limit,
            "status_code": status_code,
            "source": search_url,
            "duration_ms": duration_ms,
            "attempts": attempts,
        }
    except Exception as exc:
        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        logger.exception(
            "google search failed query=%s dynamic=%s solve_recaptcha=%s duration_ms=%s error=%s",
            normalized_query,
            dynamic,
            solve_recaptcha,
            duration_ms,
            exc,
        )
        return {
            "query": normalized_query,
            "results": [],
            "total": 0,
            "limit": limit,
            "status_code": None,
            "source": search_url,
            "duration_ms": duration_ms,
            "attempts": 0,
            "error": str(exc),
        }


def search_google_html(
    query: str,
    limit: int = 10,
    dynamic: bool = False,
    solve_recaptcha: bool = False,
) -> dict:
    normalized_query = clean_text(query)
    if not normalized_query:
        return {
            "content": "",
            "status_code": 400,
            "error": "query is empty",
        }

    limit = max(1, min(limit, 20))
    search_url = build_google_search_url(normalized_query, limit)
    started_at = time.perf_counter()

    logger.info(
        "google html search start query=%s limit=%s dynamic=%s solve_recaptcha=%s",
        normalized_query,
        limit,
        dynamic,
        solve_recaptcha,
    )

    page = fetch_page(
        search_url,
        dynamic=dynamic,
        solve_recaptcha=solve_recaptcha,
    )

    status_code = get_page_status_code(page)
    blocked_error = detect_google_block(page)
    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)

    try:
        content = str(page.html_content)
    except Exception:
        content = str(page)

    result = {
        "content": content,
        "status_code": status_code or 200,
        "source": search_url,
        "duration_ms": duration_ms,
    }
    if blocked_error:
        result["error"] = blocked_error
    return result


def scrape_html_result(url: str, dynamic: bool = False) -> dict:
    started_at = time.perf_counter()
    page, title, salary, location, blocked_error = fetch_page_with_fallback(
        url,
        dynamic=dynamic,
    )
    status_code = get_page_status_code(page)
    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    content = get_page_html(page)

    result = {
        "content": content,
        "status_code": status_code or 200,
        "source": url,
        "duration_ms": duration_ms,
        "title": title,
        "salary": salary,
        "location": location,
    }
    if blocked_error:
        result["error"] = blocked_error
    return result


def sort_links_for_job_search(links: list[str]) -> list[str]:
    priority_markers = (
        "/jobs",
        "/job",
        "/careers",
        "/career",
        "/positions",
        "/vacancies",
        "/opportunities",
    )

    return sorted(
        links,
        key=lambda link: (
            0 if any(marker in link.lower() for marker in priority_markers) else 1,
            len(link),
            link,
        ),
    )


def fetch_page(
    url: str,
    dynamic: bool = False,
    solve_recaptcha: bool = False,
):
    if dynamic:
        return fetch_stealthy_page(
            url,
            solve_recaptcha=solve_recaptcha,
        )

    return Fetcher.get(
        url,
        stealthy_headers=True,
        retries=1,
    )


def get_page_html(page) -> str:
    try:
        return str(page.html_content)
    except Exception:
        return str(page)


def clean_html_fragment(html: str) -> str:
    html = (html or "").strip()
    if not html:
        return ""

    cleaned = html
    for pattern in HTML_REMOVAL_PATTERNS:
        cleaned = pattern.sub("", cleaned)

    cleaned = HTML_ATTRIBUTED_BOILERPLATE_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"\s(?:on[a-z]+|style)=('([^']*)'|\"([^\"]*)\")", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s(?:data-[a-z0-9_-]+)=('([^']*)'|\"([^\"]*)\")", "", cleaned, flags=re.IGNORECASE)
    cleaned = HTML_TAG_COMPACT_PATTERN.sub("><", cleaned)
    cleaned = HTML_WHITESPACE_PATTERN.sub(" ", cleaned)
    cleaned = cleaned.strip()

    if len(cleaned) > MAX_CLEAN_HTML_LENGTH:
        cleaned = cleaned[:MAX_CLEAN_HTML_LENGTH].rstrip() + "\n<!-- truncated -->"

    return cleaned


def get_clean_page_html(page) -> str:
    return clean_html_fragment(get_page_html(page))


def extract_page_data(page) -> tuple[str, str, str, str]:
    structured_title, structured_salary, structured_location = extract_jobposting_data(page)
    title = structured_title or extract_title(page)
    body_text = extract_body_text(page)
    salary = structured_salary or extract_salary_near_title(body_text, title)
    location = structured_location or extract_location_from_body(body_text)
    blocked_error = detect_blocked_page(page, title, body_text)
    return title, salary, location, blocked_error


def fetch_page_with_fallback(url: str, dynamic: bool = False):
    effective_dynamic = dynamic or should_force_dynamic(url)
    page = fetch_page(url, dynamic=effective_dynamic)
    title, salary, location, blocked_error = extract_page_data(page)
    status_code = get_page_status_code(page)

    if effective_dynamic or status_code in PROMPT_RESPONSE_STATUS_CODES:
        return page, title, salary, location, blocked_error

    if status_code != 403 and blocked_error in {"blocked_by_bot_detection", "blocked_by_login"}:
        logger.warning("skip blocked url=%s reason=%s", url, blocked_error)
        return page, title, salary, location, blocked_error

    if status_code == 403 or blocked_error:
        logger.warning(
            "chromium fallback triggered url=%s status=%s reason=%s",
            url,
            status_code,
            blocked_error or "none",
        )
        page = fetch_stealthy_page(url)
        title, salary, location, blocked_error = extract_page_data(page)

    return page, title, salary, location, blocked_error


def fetch_page_with_mode_detection(url: str) -> tuple[object, str, str, str, str, str]:
    effective_dynamic = should_force_dynamic(url)

    if effective_dynamic:
        page = fetch_stealthy_page(url)
        title, salary, location, blocked_error = extract_page_data(page)
        return page, title, salary, location, blocked_error, "dynamic"

    page = fetch_page(url, dynamic=False)
    title, salary, location, blocked_error = extract_page_data(page)
    status_code = get_page_status_code(page)

    if status_code in PROMPT_RESPONSE_STATUS_CODES:
        return page, title, salary, location, blocked_error, "static"

    if blocked_error or status_code == 403:
        logger.warning("test fallback triggered url=%s status=%s blocked=%s", url, status_code, blocked_error or "none")
        page = fetch_stealthy_page(url)
        title, salary, location, blocked_error = extract_page_data(page)
        status_code = get_page_status_code(page)

        if blocked_error or status_code == 403:
            return page, title, salary, location, blocked_error, "403"

        return page, title, salary, location, blocked_error, "dynamic"

    return page, title, salary, location, blocked_error, "static"


def scrape_url(url: str, dynamic: bool = False) -> dict:
    host = urlparse(url).netloc
    started_at = time.perf_counter()

    try:
        logger.info(
            "scrape start host=%s dynamic=%s url=%s",
            host,
            dynamic,
            url,
        )

        fetch_started_at = time.perf_counter()
        page, title, salary, location, blocked_error = fetch_page_with_fallback(url, dynamic=dynamic)
        fetch_duration_ms = round((time.perf_counter() - fetch_started_at) * 1000, 2)
        logger.info(
            "fetch complete host=%s mode=%s duration_ms=%s page=%s",
            host,
            "dynamic" if dynamic else "static",
            fetch_duration_ms,
            summarize_page(page),
        )
        logger.info(
            "extract complete host=%s mode=%s blocked=%s title_found=%s salary_found=%s",
            host,
            "dynamic" if dynamic else "static",
            blocked_error or "none",
            bool(title),
            bool(salary),
        )

        if blocked_error:
            total_duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.warning(
                "scrape blocked host=%s error=%s total_duration_ms=%s",
                host,
                blocked_error,
                total_duration_ms,
            )
            return {
                "title": "",
                "salary": "",
                "source": url,
                "error": blocked_error,
            }

        total_duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        logger.info(
            "scrape success host=%s title_found=%s salary_found=%s total_duration_ms=%s",
            host,
            bool(title),
            bool(salary),
            total_duration_ms,
        )
        return {
            "title": title,
            "salary": salary,
            "source": url,
        }

    except Exception as exc:
        total_duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        logger.exception(
            "scrape failed host=%s dynamic=%s total_duration_ms=%s error=%s",
            host,
            dynamic,
            total_duration_ms,
            exc,
        )
        return {
            "title": "",
            "salary": "",
            "source": url,
            "error": str(exc),
        }


def test_scrape_url(url: str) -> dict:
    host = urlparse(url).netloc
    started_at = time.perf_counter()

    try:
        logger.info("scrape test start host=%s url=%s", host, url)

        page, title, salary, location, blocked_error, mode = fetch_page_with_mode_detection(url)
        status_code = get_page_status_code(page)
        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)

        logger.info(
            "scrape test complete host=%s mode=%s status=%s blocked=%s duration_ms=%s",
            host,
            mode,
            status_code,
            blocked_error or "none",
            duration_ms,
        )

        error = blocked_error
        if mode == "403" and not error:
            error = "http_403"

        return {
            "source": url,
            "result": mode,
            "status_code": status_code or "",
            "title": title,
            "salary": salary,
            "location": location,
            "error": error or "",
            "duration_ms": duration_ms,
        }
    except Exception as exc:
        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        logger.exception(
            "scrape test failed host=%s total_duration_ms=%s error=%s",
            host,
            duration_ms,
            exc,
        )
        return {
            "source": url,
            "result": "error",
            "status_code": "",
            "title": "",
            "salary": "",
            "location": "",
            "error": str(exc),
            "duration_ms": duration_ms,
        }


def find_job_link(
    start_url: str,
    job_title: str,
    location: str | None = None,
    dynamic: bool = False,
    max_pages: int = 25,
) -> dict:
    normalized_target = normalize_title_match(job_title)
    normalized_location = normalize_location_match(location or "")
    if not normalized_target:
        return {
            "job_title": job_title,
            "found": False,
            "match": None,
            "visited": 0,
            "error": "job_title is empty",
        }

    max_pages = max(1, min(max_pages, 100))
    queue = deque([start_url])
    visited: set[str] = set()
    matches: list[dict] = []

    logger.info(
        "job link search start start_url=%s job_title=%s dynamic=%s max_pages=%s",
        start_url,
        job_title,
        dynamic,
        max_pages,
    )

    while queue and len(visited) < max_pages:
        current_url = queue.popleft()
        if current_url in visited:
            continue

        visited.add(current_url)

        try:
            page, extracted_title, _, extracted_location, blocked_error = fetch_page_with_fallback(current_url, dynamic=dynamic)
            if blocked_error:
                logger.warning("job link page blocked url=%s error=%s", current_url, blocked_error)
                continue

            normalized_found = normalize_title_match(extracted_title)
            normalized_found_location = normalize_location_match(extracted_location)
            location_matches = (
                True
                if not normalized_location
                else normalized_location in normalized_found_location
            )
            if normalized_found and normalized_found == normalized_target and location_matches:
                result = {
                    "job_title": extracted_title,
                    "url": current_url,
                    "source": current_url,
                }
                matches.append(result)
                logger.info("job link match found url=%s title=%s location=%s", current_url, extracted_title, extracted_location)
                return {
                    "job_title": job_title,
                    "found": True,
                    "match": result,
                    "visited": len(visited),
                }

            discovered_links = sort_links_for_job_search(extract_links(page, current_url))
            for link in discovered_links:
                if link not in visited:
                    queue.append(link)
        except Exception as exc:
            logger.exception("job link page failed url=%s error=%s", current_url, exc)

    logger.info(
        "job link search complete start_url=%s found=%s visited=%s",
        start_url,
        False,
        len(visited),
    )
    return {
        "job_title": job_title,
        "found": False,
        "match": None,
        "visited": len(visited),
    }


async def scrape_url_with_limit(url: str, dynamic: bool, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        return await run_in_threadpool(scrape_url, url, dynamic)


async def scrape_html_with_limit(
    url: str,
    dynamic: bool,
    semaphore: asyncio.Semaphore,
) -> dict:
    async with semaphore:
        result = await run_in_threadpool(scrape_html_result, url, dynamic)
        return {
            "content": clean_html_fragment(result.get("content", "")),
            "status_code": result.get("status_code"),
            "source": result.get("source"),
            "duration_ms": result.get("duration_ms"),
            **({"error": result["error"]} if "error" in result else {}),
        }


async def test_scrape_url_with_limit(url: str, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        return await run_in_threadpool(test_scrape_url, url)


@app.get("/")
def health_check():
    return {
        "status": "ok",
        "message": "Scrapling API is running",
    }


@app.post("/scrape")
def scrape(req: ScrapeRequest):
    return scrape_url(str(req.url), req.dynamic)


@app.post("/scrape/batch")
async def scrape_batch(req: BatchScrapeRequest):
    urls = [str(url) for url in req.urls]
    if not urls:
        return []

    concurrency = max(1, min(req.concurrency, len(urls)))
    semaphore = asyncio.Semaphore(concurrency)
    started_at = time.perf_counter()

    logger.info(
        "batch scrape start total=%s concurrency=%s dynamic=%s",
        len(urls),
        concurrency,
        req.dynamic,
    )

    tasks = [
        scrape_html_with_limit(url, req.dynamic, semaphore)
        for url in urls
    ]
    results = await asyncio.gather(*tasks)
    total_duration_ms = round((time.perf_counter() - started_at) * 1000, 2)

    logger.info(
        "batch scrape complete total=%s concurrency=%s duration_ms=%s",
        len(urls),
        concurrency,
        total_duration_ms,
    )

    return results


@app.post("/scrape/test")
async def scrape_test(req: BatchScrapeTestRequest):
    urls = [str(url) for url in req.urls]
    if not urls:
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=["source", "result", "status_code", "title", "salary", "location", "error", "duration_ms"],
        )
        writer.writeheader()
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="scrape_test_results.csv"'},
        )

    concurrency = max(1, min(req.concurrency, len(urls)))
    semaphore = asyncio.Semaphore(concurrency)
    started_at = time.perf_counter()

    logger.info(
        "scrape test batch start total=%s concurrency=%s",
        len(urls),
        concurrency,
    )

    tasks = [
        test_scrape_url_with_limit(url, semaphore)
        for url in urls
    ]
    results = await asyncio.gather(*tasks)
    total_duration_ms = round((time.perf_counter() - started_at) * 1000, 2)

    logger.info(
        "scrape test batch complete total=%s concurrency=%s duration_ms=%s",
        len(results),
        concurrency,
        total_duration_ms,
    )

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["source", "result", "status_code", "title", "salary", "location", "error", "duration_ms"],
    )
    writer.writeheader()
    writer.writerows(results)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="scrape_test_results.csv"'},
    )


@app.post("/scrape/html")
def scrape_html(req: ScrapeRequest):
    result = scrape_html_result(
        url=str(req.url),
        dynamic=req.dynamic,
    )
    return HTMLResponse(
        content=result["content"],
        status_code=result.get("status_code") or 200,
        headers={
            "X-Scrape-Source": result.get("source", ""),
            "X-Scrape-Duration-Ms": str(result.get("duration_ms", "")),
            "X-Scrape-Title": result.get("title", ""),
            "X-Scrape-Salary": result.get("salary", ""),
            "X-Scrape-Location": result.get("location", ""),
            "X-Scrape-Error": result.get("error", ""),
        },
    )


@app.post("/search/google")
def google_search(req: GoogleSearchRequest):
    return search_google(
        query=req.query,
        limit=req.limit,
        dynamic=req.dynamic,
        solve_recaptcha=req.solve_recaptcha,
    )


@app.post("/search/google/html")
def google_search_html(req: GoogleSearchRequest):
    result = search_google_html(
        query=req.query,
        limit=req.limit,
        dynamic=req.dynamic,
        solve_recaptcha=req.solve_recaptcha,
    )
    return HTMLResponse(
        content=result["content"],
        status_code=result.get("status_code") or 200,
        headers={
            "X-Search-Source": result.get("source", ""),
            "X-Search-Duration-Ms": str(result.get("duration_ms", "")),
            "X-Search-Error": result.get("error", ""),
        },
    )


@app.post("/jobs/find-link")
def jobs_find_link(req: JobLinkSearchRequest):
    return find_job_link(
        start_url=str(req.start_url),
        job_title=req.job_title,
        location=req.location,
        dynamic=req.dynamic,
        max_pages=req.max_pages,
    )
