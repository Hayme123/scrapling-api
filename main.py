import json
import re

from fastapi import FastAPI
from pydantic import BaseModel, HttpUrl
from scrapling.fetchers import Fetcher, StealthyFetcher

app = FastAPI()

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
    "cf-mitigated",
    "cloudflare",
    "please enable cookies",
    "verify you are human",
    "checking your browser",
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


class ScrapeRequest(BaseModel):
    url: HttpUrl
    dynamic: bool = False


def clean_text(text: str) -> str:
    text = (text or "").replace("Ã¢â‚¬â€œ", " - ").replace("â€“", " - ").replace("â€”", " - ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_title(text: str) -> str:
    text = clean_text(text)
    if not text:
        return ""
    return TITLE_SPLIT_PATTERN.split(text, maxsplit=1)[0].strip()


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


def extract_jobposting_data(page) -> tuple[str, str]:
    try:
        scripts = page.css('script[type="application/ld+json"]::text').getall()
    except Exception:
        return "", ""

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

            if not salary:
                salary = format_structured_salary(item.get("estimatedSalary", {}))

            if title or salary:
                return title, clean_text(salary)

    return "", ""


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


def detect_blocked_page(title: str, body_text: str) -> str:
    normalized_title = clean_text(title).lower()
    normalized_body = clean_text(body_text).lower()

    for pattern in BLOCKED_TITLE_PATTERNS:
        if pattern in normalized_title:
            return "blocked_by_cloudflare"

    for pattern in BLOCKED_BODY_PATTERNS:
        if pattern in normalized_body:
            return "blocked_by_cloudflare"

    return ""


def fetch_page(url: str, dynamic: bool = False):
    if dynamic:
        return StealthyFetcher.fetch(
            url,
            headless=True,
            solve_cloudflare=True,
            google_search=False,
            network_idle=True,
            timeout=60000,
        )

    return Fetcher.get(
        url,
        stealthy_headers=True,
    )


def extract_page_data(page) -> tuple[str, str, str]:
    structured_title, structured_salary = extract_jobposting_data(page)
    title = structured_title or extract_title(page)
    body_text = extract_body_text(page)
    salary = structured_salary or extract_salary_near_title(body_text, title)
    blocked_error = detect_blocked_page(title, body_text)
    return title, salary, blocked_error


@app.get("/")
def health_check():
    return {
        "status": "ok",
        "message": "Scrapling API is running",
    }


@app.post("/scrape")
def scrape(req: ScrapeRequest):
    url = str(req.url)

    try:
        page = fetch_page(url, dynamic=req.dynamic)
        title, salary, blocked_error = extract_page_data(page)

        if blocked_error and not req.dynamic:
            page = fetch_page(url, dynamic=True)
            title, salary, blocked_error = extract_page_data(page)

        if blocked_error:
            return {
                "title": "",
                "salary": "",
                "source": url,
                "error": blocked_error,
            }

        return {
            "title": title,
            "salary": salary,
            "source": url,
        }

    except Exception as exc:
        return {
            "title": "",
            "salary": "",
            "source": url,
            "error": str(exc),
        }
