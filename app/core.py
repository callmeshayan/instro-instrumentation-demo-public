"""Shared CSV and pipeline logic for the local dashboard."""

from __future__ import annotations

import csv
import io
import math
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from email import policy
from email.parser import BytesParser
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
BACKUP_DIR = DATA_DIR / "backups"
LEAD_COLUMNS = [
    "lead_id", "company", "contact_name", "email", "country", "sector",
    "status", "stage", "estimated_value_eur", "last_contact", "next_action_date", "notes",
]
OPEN_STATUSES = {"", "open", "new", "active", "qualified", "working", "in progress"}
RESTRICTED_DESTINATIONS = {"iran", "russia", "belarus", "north korea", "syria", "cuba"}
# Aliases and official names, matched as whole words anywhere in destination or end-user text.
RESTRICTED_PATTERNS = [
    r"\biran\b", r"\bpersia\b", r"\brussia\b", r"\brussian federation\b", r"\bbelarus\b",
    r"\bnorth korea\b", r"\bdprk\b", r"\bdemocratic people'?s republic of korea\b",
    r"\bsyria\b", r"\bsyrian\b", r"\bcuba\b", r"\bcrimea\b", r"\bdonetsk\b", r"\bluhansk\b",
    # Major cities / industrial hubs (not exhaustive; manual screening is still required).
    r"\btehran\b", r"\bisfahan\b", r"\besfahan\b", r"\btabriz\b", r"\bshiraz\b", r"\bmashhad\b",
    r"\bbandar abbas\b", r"\babadan\b", r"\bahvaz\b", r"\bahwaz\b", r"\bassaluyeh\b", r"\basaluyeh\b",
    r"\bkharg\b", r"\bbushehr\b", r"\bmoscow\b", r"\bst\.? petersburg\b", r"\bsaint petersburg\b",
    r"\bnovorossiysk\b", r"\bminsk\b", r"\bpyongyang\b", r"\bdamascus\b", r"\baleppo\b", r"\bhoms\b",
    r"\bhavana\b", r"\bsevastopol\b",
]


def _path_for(filename: str | Path) -> Path:
    path = Path(filename)
    return path if path.is_absolute() else DATA_DIR / path


def read_csv(filename: str | Path, columns: list[str] | None = None) -> pd.DataFrame:
    """Read a CSV, returning a correctly shaped empty frame for absent/empty files."""
    path = _path_for(filename)
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=columns or [])
    try:
        frame = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=columns or [])
    if columns:
        for column in columns:
            if column not in frame.columns:
                frame[column] = pd.NA
    return frame


def _timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f_%z")


def write_csv(filename: str | Path, frame: pd.DataFrame) -> Path:
    """Back up the prior CSV, then atomically replace it with the new contents."""
    path = _path_for(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"{path.stem}_{_timestamp()}{path.suffix}"
        # Preserve the original bytes in the backup before attempting a replacement.
        backup.write_bytes(path.read_bytes())
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            frame.to_csv(stream, index=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return path


def follow_up_days(stage: Any) -> int:
    """Return the follow-up interval in days for a lead's current stage."""
    text = str(stage or "").strip().lower()
    if any(word in text for word in ("negotiat", "decision", "closing", "procurement")):
        return 14
    if any(word in text for word in ("rfq", "quote", "proposal", "evaluation", "qualified")):
        return 7
    return 3


def next_follow_up(stage: Any, from_date: date | datetime | str | None = None) -> date:
    if from_date is None:
        start = date.today()
    elif isinstance(from_date, datetime):
        start = from_date.date()
    elif isinstance(from_date, date):
        start = from_date
    else:
        start = date.fromisoformat(str(from_date)[:10])
    return start + timedelta(days=follow_up_days(stage))


def due_follow_ups(leads: pd.DataFrame, today: date | None = None) -> pd.DataFrame:
    today = today or date.today()
    if leads.empty or "next_action_date" not in leads.columns:
        return leads.iloc[0:0].copy()
    statuses = leads.get("status", pd.Series("", index=leads.index)).fillna("").astype(str).str.lower()
    due = pd.to_datetime(leads["next_action_date"], errors="coerce").dt.date
    mask = due.notna() & (due <= today) & ~statuses.isin({"won", "lost"})
    return leads.loc[mask].copy()


def mark_follow_up_done(leads: pd.DataFrame, lead_id: Any, today: date | None = None) -> pd.DataFrame:
    result = leads.copy()
    if "lead_id" not in result.columns:
        raise KeyError("leads.csv needs a lead_id column")
    matches = result["lead_id"].astype(str) == str(lead_id)
    if not matches.any():
        raise KeyError(f"Lead {lead_id!r} was not found")
    today = today or date.today()
    result.loc[matches, "last_contact"] = today.isoformat()
    for idx in result.index[matches]:
        stage = result.at[idx, "stage"] if "stage" in result.columns else ""
        result.loc[idx, "next_action_date"] = next_follow_up(stage, today).isoformat()
    return result


def default_margin(order_value_eur: float) -> float:
    """Suggested gross margin: 35% below €1k, 25% standard, 18% above €20k."""
    value = float(order_value_eur)
    if value < 0:
        raise ValueError("Order value cannot be negative")
    if value < 1000:
        return 0.35
    if value > 20000:
        return 0.18
    return 0.25


def validate_margin(margin: float, approved_by_shayan: bool = False) -> None:
    value = float(margin)
    if not math.isfinite(value) or value < 0 or value >= 1:
        raise ValueError("Margin must be at least 0% and below 100%")
    if value < 0.12 and not approved_by_shayan:
        raise ValueError("Margins below 12% require Approved by Shayan")


def calculate_quote_line(cost: float, freight: float = 0, duty: float = 0,
                        margin: float = 0.25, approved_by_shayan: bool = False) -> dict[str, float]:
    """Calculate sell price from landed cost using margin as a share of sell price."""
    cost, freight, duty = float(cost), float(freight), float(duty)
    if any(not math.isfinite(x) or x < 0 for x in (cost, freight, duty)):
        raise ValueError("Cost, freight and duty must be non-negative finite values")
    validate_margin(margin, approved_by_shayan)
    landed = cost + freight + duty
    return {"cost": cost, "freight": freight, "duty": duty,
            "margin": float(margin), "price": round(landed / (1 - float(margin)), 2)}


def compliance_hold(destination: Any, end_user: Any) -> tuple[bool, str]:
    destination_text = str(destination or "").strip()
    end_user_text = str(end_user or "").strip()
    if not destination_text:
        return True, "COMPLIANCE HOLD: country of destination is required."
    if not end_user_text:
        return True, "COMPLIANCE HOLD: end user is required."
    normalized = re.sub(r"\s+", " ", destination_text).lower()
    if normalized in RESTRICTED_DESTINATIONS or any(re.search(p, normalized) for p in RESTRICTED_PATTERNS):
        return True, f"COMPLIANCE HOLD: destination {destination_text} is restricted."
    end_user_norm = re.sub(r"\s+", " ", end_user_text).lower()
    if any(re.search(p, end_user_norm) for p in RESTRICTED_PATTERNS):
        return True, "COMPLIANCE HOLD: end user references a restricted country. Manual review required."
    return False, "Compliance details present. Screen the end user against EU/UK/US sanctions lists before sending."


def valid_email(value: Any) -> bool:
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", str(value or "").strip()))


def open_count(frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    statuses = frame.get("status", pd.Series("", index=frame.index)).fillna("").astype(str).str.lower()
    return int((~statuses.isin({"won", "lost", "closed", "cancelled", "canceled"})).sum())



# ---------------------------------------------------------------------------
# Phase 2: RFQs, costs and quote generation
# ---------------------------------------------------------------------------

import json
import urllib.error
import urllib.parse
import urllib.request


def _read_json_request(request: urllib.request.Request, timeout: int) -> dict[str, Any]:
    """Read JSON over verified TLS, using macOS curl trust only if Python lacks the system CA chain."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        if sys.platform != "darwin" or not isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise
        command = ["/usr/bin/curl", "--fail", "--silent", "--show-error", "--max-time", str(timeout),
                   "--proto", "=https"]
        for name, value in request.header_items():
            command.extend(["--header", f"{name}: {value}"])
        if request.data is not None:
            command.extend(["--request", request.get_method(), "--data-binary", "@-"])
        command.append(request.full_url)
        try:
            result = subprocess.run(command, input=request.data, capture_output=True,
                                    text=request.data is None, timeout=timeout + 2, check=False)
        except (OSError, subprocess.TimeoutExpired) as curl_error:
            raise urllib.error.URLError(curl_error) from exc
        if result.returncode:
            detail = (result.stderr or "curl failed").strip()
            raise urllib.error.URLError(detail) from exc
        body = result.stdout
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        return json.loads(body)

RFQ_DIR = ROOT / "rfq"
INBOX_DIR = RFQ_DIR / "inbox"
PARSED_DIR = RFQ_DIR / "parsed"
QUOTES_DIR = ROOT / "quotes"
TEMPLATES_DIR = ROOT / "templates"

RFQ_COLUMNS = ["rfq_id", "customer", "received_date", "status", "value_eur", "notes"]
COST_COLUMNS = ["rfq_id", "line", "supplier_id", "model", "unit_cost_eur", "freight_eur",
                "duty_eur", "lead_time", "quote_ref", "valid_until"]
TECHNICAL_REVIEW_COLUMNS = ["rfq_id", "line", "field", "requested_value", "offered_value", "status",
                            "evidence_ref", "notes", "updated_at"]
TECHNICAL_REVIEW_FIELDS = [
    ("service", "Service / medium"), ("quantity", "Quantity"), ("process_connection", "Process connection"),
    ("range", "Range and units"), ("accuracy", "Accuracy"), ("wetted_material", "Wetted material"),
    ("output", "Output / protocol"), ("power", "Power"), ("hazardous_area_cert", "Hazardous-area certification"),
    ("ip", "IP rating"), ("ambient_temp", "Ambient temperature"), ("process_temp", "Process temperature"),
    ("display", "Display"), ("delivery_date", "Required delivery"), ("documents", "Required documents"),
]
TECHNICAL_REVIEW_STATUSES = ["Not reviewed", "Compliant", "Deviation", "Clarification needed", "Not applicable"]
SUPPLIER_COLUMNS = ["supplier_id", "company", "country", "contact", "email", "brands", "categories",
                    "authorized", "typical_lead_time", "payment_terms", "notes"]
QUOTE_LOG_COLUMNS = ["rfq_id", "sent_date", "value_eur", "margin", "quote_no", "version",
                     "generated_date", "md_file", "pdf_file", "destination", "end_user",
                     "approved_by_shayan", "currency", "fx_rate", "value_quote_ccy",
                     "outcome", "outcome_date", "lost_reason", "customer_company", "contact_name",
                     "customer_email", "valid_until", "followup_count", "last_followup_date",
                     "approval_status", "approver", "approval_date", "approval_note"]
QUOTE_LINE_COLUMNS = ["quote_no", "rfq_id", "version", "line", "tag", "description", "brand_model",
                      "qty", "supplier_id", "unit_cost_eur", "freight_eur", "duty_eur", "landed_unit_eur",
                      "margin", "unit_price_eur", "total_eur", "lead_time"]
SELLER_COLUMNS = ["seller_name", "seller_company", "seller_email", "seller_phone", "seller_address",
                  "seller_website", "seller_vat", "default_currency", "default_payment_terms",
                  "default_validity_days", "terms_text", "logo_file", "require_approval", "approver_name",
                  "approver_email"]
CURRENCIES = {"EUR": "€", "GBP": "£", "USD": "$"}
DEFAULT_TERMS = (
    "1. Prices are per unit in the stated currency, excluding VAT and local taxes unless stated.\n"
    "2. Lead times are estimates from date of purchase order and are subject to supplier confirmation.\n"
    "3. Delivery per the stated Incoterms (ICC Incoterms 2020).\n"
    "4. Orders are subject to export-control and sanctions screening of the end user and destination.\n"
    "5. Specifications not confirmed by the customer (marked TBC) must be confirmed before order.\n"
    "6. Cancellation of confirmed orders may incur supplier charges."
)
OUTCOMES = ["won", "lost"]
SNAPSHOT_DIR_NAME = "quote_snapshots"
MANUAL_LINE_FIELDS = ["line", "tag", "description", "quantity", "service", "range", "accuracy", "output",
                      "process_connection", "wetted_material", "hazardous_area_cert", "ip", "power", "ambient_temp",
                      "process_temp", "display", "documents", "delivery_date", "source_reference", "notes"]
RFQ_STATUSES = ["new", "parsed", "priced", "quoted", "won", "lost"]
FINAL_STATUSES = {"won", "lost"}
INCOTERMS = ["EXW", "FCA", "DAP"]

# --- public tender search (TED) ----------------------------------------------

TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
FIND_A_TENDER_URL = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
CONTRACTS_FINDER_URL = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
PUBLIC_CONTRACTS_SCOTLAND_URL = "https://api.publiccontractsscotland.gov.uk/v1/Notices"
TENDER_CPV_DEFAULT = ["38420000", "38410000", "38430000", "38540000", "42131000", "38550000"]
# Country filters are optional; an empty selection searches every country represented in TED.
TENDER_COUNTRY_OPTIONS = {
    "AUT": "Austria", "BEL": "Belgium", "BGR": "Bulgaria", "HRV": "Croatia",
    "CYP": "Cyprus", "CZE": "Czechia", "DNK": "Denmark", "EST": "Estonia",
    "FIN": "Finland", "FRA": "France", "DEU": "Germany", "GRC": "Greece",
    "HUN": "Hungary", "ISL": "Iceland", "IRL": "Ireland", "ITA": "Italy",
    "LVA": "Latvia", "LIE": "Liechtenstein", "LTU": "Lithuania", "LUX": "Luxembourg",
    "MLT": "Malta", "NLD": "Netherlands", "NOR": "Norway", "POL": "Poland",
    "PRT": "Portugal", "ROU": "Romania", "SVK": "Slovakia", "SVN": "Slovenia",
    "ESP": "Spain", "SWE": "Sweden", "CHE": "Switzerland", "GBR": "United Kingdom",
}
TENDER_KEYWORDS = (
    "transmitter", "flowmeter", "gas detect", "instrument", "scada", "analyser",
    "analyzer", "valve", "calibration",
)
TENDER_COLUMNS = ["notice_id", "title", "buyer", "country", "cpv", "published", "deadline", "url",
                  "status", "notes", "first_seen", "source", "ocid", "description", "documents_json"]
TENDER_FIELDS = ["publication-number", "notice-title", "organisation-name-buyer",
                 "organisation-country-buyer", "classification-cpv", "publication-date",
                 "deadline-receipt-tender-date-lot", "links"]
TENDER_STATUSES = ["new", "relevant", "ignored", "rfq_created"]


def build_tender_query(cpv_codes: list[str] | None = None,
                       countries: list[str] | None = None,
                       published_after: date | str | None = None,
                       published_before: date | str | None = None) -> str:
    """Build a TED Expert Search query for CPV/country and publication date."""
    cpv_codes = [str(code).strip() for code in (cpv_codes or TENDER_CPV_DEFAULT) if str(code).strip()]
    countries = [str(code).strip().upper() for code in (countries or []) if str(code).strip()]
    if not cpv_codes:
        raise ValueError("At least one CPV code is required")

    def ted_date(value: date | str) -> str:
        if isinstance(value, date):
            return value.strftime("%Y%m%d")
        return date.fromisoformat(str(value)[:10]).strftime("%Y%m%d")

    terms = [f"classification-cpv IN ({', '.join(cpv_codes)})"]
    if countries:
        terms.append(f"organisation-country-buyer IN ({', '.join(countries)})")
    if published_after is not None:
        terms.append(f"publication-date >= {ted_date(published_after)}")
    if published_before is not None:
        terms.append(f"publication-date <= {ted_date(published_before)}")
    return " AND ".join(terms)


def fetch_tenders(query: str, limit: int = 250, page: int = 1,
                  timeout: int = 20) -> dict[str, Any]:
    """Make one explicit, read-only TED Search API request."""
    if not 1 <= int(limit) <= 250:
        raise ValueError("TED page limit must be between 1 and 250")
    if int(page) < 1:
        raise ValueError("TED page number must be at least 1")
    payload = {"query": query, "fields": TENDER_FIELDS, "limit": int(limit), "page": int(page)}
    request = urllib.request.Request(
        TED_SEARCH_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    try:
        return _read_json_request(request, timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"TED Search API returned HTTP {exc.code}: {detail[:500]}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"TED Search API request failed: {exc}") from exc


def fetch_all_tenders(query: str, limit: int = 250, timeout: int = 20) -> dict[str, Any]:
    """Fetch every page TED's pagination mode makes available for a query (up to 15,000)."""
    first = fetch_tenders(query, limit=limit, page=1, timeout=timeout)
    notices = list(first.get("notices", []) or [])
    total = int(first.get("totalNoticeCount", len(notices)) or 0)
    retrievable = min(total, 15_000)
    page_count = (retrievable + limit - 1) // limit
    for page in range(2, page_count + 1):
        response = fetch_tenders(query, limit=limit, page=page, timeout=timeout)
        notices.extend(response.get("notices", []) or [])
    return {**first, "notices": notices, "totalNoticeCount": total,
            "pagesFetched": page_count, "truncated": total > len(notices)}


def fetch_find_a_tender(start_date: date, end_date: date, limit: int = 100,
                        timeout: int = 20, max_pages: int = 200) -> dict[str, Any]:
    """Fetch UK tender-stage OCDS releases using Find a Tender's public cursor API."""
    if start_date > end_date:
        raise ValueError("Find a Tender start date must be on or before the end date")
    if not 1 <= int(limit) <= 100:
        raise ValueError("Find a Tender page limit must be between 1 and 100")
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    params = {
        "updatedFrom": f"{start_date.isoformat()}T00:00:00",
        "updatedTo": f"{end_date.isoformat()}T23:59:59",
        "stages": "tender",
        "limit": int(limit),
    }
    url = f"{FIND_A_TENDER_URL}?{urllib.parse.urlencode(params)}"
    releases: list[dict[str, Any]] = []
    pages = 0
    next_url: str | None = url
    while next_url and pages < max_pages:
        parsed_url = urllib.parse.urlparse(next_url)
        expected = urllib.parse.urlparse(FIND_A_TENDER_URL)
        if parsed_url.scheme != "https" or parsed_url.netloc != expected.netloc or parsed_url.path != expected.path:
            raise RuntimeError("Find a Tender returned an unexpected pagination URL")
        request = urllib.request.Request(next_url, headers={"Accept": "application/json",
                                                             "User-Agent": "InstroDashboard/1.0"})
        try:
            payload = _read_json_request(request, timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Find a Tender returned HTTP {exc.code}: {detail[:500]}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Find a Tender request failed: {exc}") from exc
        releases.extend(payload.get("releases", []) or [])
        pages += 1
        candidate = payload.get("links", {}).get("next")
        next_url = str(candidate) if candidate else None
    return {"releases": releases, "pagesFetched": pages, "truncated": next_url is not None}


def fetch_contracts_finder(start_date: date, end_date: date, limit: int = 100,
                           timeout: int = 20, max_pages: int = 200) -> dict[str, Any]:
    """Fetch UK Contracts Finder tender-stage OCDS releases through its public cursor API."""
    if start_date > end_date:
        raise ValueError("Contracts Finder start date must be on or before the end date")
    if not 1 <= int(limit) <= 100:
        raise ValueError("Contracts Finder page limit must be between 1 and 100")
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    params = {
        "publishedFrom": f"{start_date.isoformat()}T00:00:00",
        "publishedTo": f"{end_date.isoformat()}T23:59:59",
        "stages": "tender", "limit": int(limit),
    }
    next_url: str | None = f"{CONTRACTS_FINDER_URL}?{urllib.parse.urlencode(params)}"
    releases: list[dict[str, Any]] = []
    pages = 0
    while next_url and pages < max_pages:
        parsed_url = urllib.parse.urlparse(next_url)
        expected = urllib.parse.urlparse(CONTRACTS_FINDER_URL)
        if parsed_url.scheme != "https" or parsed_url.netloc != expected.netloc or parsed_url.path != expected.path:
            raise RuntimeError("Contracts Finder returned an unexpected pagination URL")
        request = urllib.request.Request(next_url, headers={"Accept": "application/json",
                                                             "User-Agent": "InstroDashboard/1.0"})
        try:
            payload = _read_json_request(request, timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Contracts Finder returned HTTP {exc.code}: {detail[:500]}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Contracts Finder request failed: {exc}") from exc
        releases.extend(payload.get("releases", []) or [])
        pages += 1
        candidate = payload.get("links", {}).get("next")
        next_url = str(candidate) if candidate else None
    return {"releases": releases, "pagesFetched": pages, "truncated": next_url is not None}


def fetch_public_contracts_scotland(start_date: date, end_date: date,
                                    timeout: int = 20) -> dict[str, Any]:
    """Fetch tender notice releases from the official Scottish OCDS API, by calendar month."""
    if start_date > end_date:
        raise ValueError("Scotland start date must be on or before the end date")
    releases: list[dict[str, Any]] = []
    months: list[tuple[int, int]] = []
    year, month = start_date.year, start_date.month
    while (year, month) <= (end_date.year, end_date.month):
        months.append((year, month))
        month = 1 if month == 12 else month + 1
        if month == 1:
            year += 1
    for year, month in months:
        params = {"dateFrom": f"{month:02d}-{year}", "noticeType": "2", "outputType": "0"}
        url = f"{PUBLIC_CONTRACTS_SCOTLAND_URL}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                        "User-Agent": "InstroDashboard/1.0"})
        try:
            payload = _read_json_request(request, timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Public Contracts Scotland returned HTTP {exc.code}: {detail[:500]}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Public Contracts Scotland request failed: {exc}") from exc
        releases.extend(payload.get("releases", []) or [])
    return {"releases": releases, "pagesFetched": len(months), "truncated": False}


def _ted_value(value: Any) -> str:
    """Select a readable value from TED's multilingual and repeated fields."""
    if isinstance(value, dict):
        for lang in ("eng", "en", "MUL"):
            if lang in value:
                return _ted_value(value[lang])
        for item in value.values():
            text = _ted_value(item)
            if text:
                return text
        return ""
    if isinstance(value, (list, tuple)):
        return next((_ted_value(item) for item in value if _ted_value(item)), "")
    if value is None:
        return ""
    return str(value).strip()


def _ted_link(notice: dict[str, Any], notice_id: str) -> str:
    links = notice.get("links", {})
    for path in ("htmlDirect", "html"):
        link = _ted_value(links.get(path, {})) if isinstance(links, dict) else ""
        if link:
            return link
    return f"https://ted.europa.eu/en/notice/{notice_id}/html"


def _ocds_documents(tender: dict[str, Any]) -> str:
    """Keep only safe, user-openable document links from a public OCDS notice."""
    documents = []
    for document in tender.get("documents", []) or []:
        if not isinstance(document, dict):
            continue
        url = str(document.get("url") or "").strip()
        if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
            continue
        documents.append({"title": str(document.get("title") or document.get("documentType") or "Tender document"),
                          "url": url})
    return json.dumps(documents, ensure_ascii=False)


def parse_tenders(response: dict[str, Any], today: date | None = None,
                  apply_local_filters: bool = True,
                  countries: list[str] | None = None) -> pd.DataFrame:
    """Normalize TED results and optionally keep only matching, open tenders."""
    today = today or date.today()
    rows = []
    for notice in response.get("notices", []) or []:
        if not isinstance(notice, dict):
            continue
        notice_id = _ted_value(notice.get("publication-number"))
        if not notice_id:
            continue
        title = _ted_value(notice.get("notice-title"))
        buyer = _ted_value(notice.get("organisation-name-buyer"))
        country = _ted_value(notice.get("organisation-country-buyer"))
        cpv_value = notice.get("classification-cpv", [])
        cpv = ", ".join(str(item) for item in cpv_value) if isinstance(cpv_value, list) else _ted_value(cpv_value)
        published = _ted_value(notice.get("publication-date"))[:10]
        deadline_values = notice.get("deadline-receipt-tender-date-lot", [])
        if not isinstance(deadline_values, list):
            deadline_values = [deadline_values]
        deadlines = sorted(str(value)[:10] for value in deadline_values if str(value or "").strip())
        deadline = deadlines[0] if deadlines else ""
        rows.append({"notice_id": notice_id, "title": title, "buyer": buyer, "country": country,
                     "cpv": cpv, "published": published, "deadline": deadline,
                     "url": _ted_link(notice, notice_id), "status": "new", "notes": "",
                     "first_seen": today.isoformat(), "source": "TED", "ocid": "", "description": "",
                     "documents_json": "[]"})
    frame = pd.DataFrame(rows, columns=TENDER_COLUMNS)
    if frame.empty or not apply_local_filters:
        return frame
    frame = frame.drop_duplicates("notice_id", keep="first")
    country_mask = (frame["country"].astype(str).str.upper().isin({code.upper() for code in countries})
                    if countries else pd.Series(True, index=frame.index))
    titles = frame["title"].fillna("").astype(str).str.lower()
    keyword_mask = titles.apply(lambda title: any(keyword in title for keyword in TENDER_KEYWORDS))
    deadline_dates = pd.to_datetime(frame["deadline"], errors="coerce").dt.date
    deadline_mask = deadline_dates.notna() & (deadline_dates > today)
    return frame.loc[country_mask & keyword_mask & deadline_mask].reset_index(drop=True)


def parse_find_a_tender(response: dict[str, Any], today: date | None = None,
                        cpv_codes: list[str] | None = None) -> pd.DataFrame:
    """Normalize public UK OCDS releases, retaining matching instrumentation tenders."""
    today = today or date.today()
    start_date = today - timedelta(days=29)
    cpv_prefixes = {str(code).rstrip("0") for code in (cpv_codes or TENDER_CPV_DEFAULT)}
    rows = []
    for release in response.get("releases", []) or []:
        if not isinstance(release, dict):
            continue
        tender = release.get("tender", {}) or {}
        notice_id = str(release.get("id") or "").strip()
        if not notice_id or not isinstance(tender, dict):
            continue
        published = str(release.get("date") or "")[:10]
        try:
            published_day = date.fromisoformat(published)
        except ValueError:
            continue
        if not start_date <= published_day <= today:
            continue
        title = str(tender.get("title") or release.get("description") or "").strip()
        cpv_values = [str((tender.get("classification") or {}).get("id") or "").strip()]
        for lot in tender.get("lots", []) or []:
            lot_cpv = str((lot.get("classification") or {}).get("id") or "").strip()
            if lot_cpv:
                cpv_values.append(lot_cpv)
        cpv_values = list(dict.fromkeys(code for code in cpv_values if code))
        matching_cpv = [code for code in cpv_values if any(code.startswith(prefix) for prefix in cpv_prefixes)]
        if not matching_cpv:
            continue
        if not any(keyword in title.lower() for keyword in TENDER_KEYWORDS):
            continue
        deadline_values = [str((tender.get("tenderPeriod") or {}).get("endDate") or "")[:10]]
        deadline_values.extend(str((lot.get("tenderPeriod") or {}).get("endDate") or "")[:10]
                               for lot in tender.get("lots", []) or [])
        future_deadlines = []
        for value in deadline_values:
            try:
                deadline_date = date.fromisoformat(value)
                if deadline_date > today:
                    future_deadlines.append(deadline_date)
            except ValueError:
                continue
        if not future_deadlines:
            continue
        deadline = min(future_deadlines).isoformat()
        buyer = release.get("buyer", {}) or {}
        buyer_name = str(buyer.get("name") or "").strip()
        if not buyer_name:
            for party in release.get("parties", []) or []:
                if "buyer" in (party.get("roles") or []):
                    buyer_name = str(party.get("name") or "").strip()
                    if buyer_name:
                        break
        rows.append({"notice_id": f"FTS-{notice_id}", "title": title, "buyer": buyer_name,
                     "country": "GBR", "cpv": ", ".join(matching_cpv), "published": published, "deadline": deadline,
                     "url": f"https://www.find-tender.service.gov.uk/Notice/{notice_id}",
                     "status": "new", "notes": "", "first_seen": today.isoformat(),
                     "source": "Find a Tender", "ocid": str(release.get("ocid") or ""),
                     "description": str(tender.get("description") or "").strip(),
                     "documents_json": _ocds_documents(tender)})
    return pd.DataFrame(rows, columns=TENDER_COLUMNS).drop_duplicates("notice_id", keep="first").reset_index(drop=True)


def parse_uk_portal_tenders(response: dict[str, Any], source: str, today: date | None = None,
                            cpv_codes: list[str] | None = None) -> pd.DataFrame:
    """Normalize UK OCDS releases from Contracts Finder or Public Contracts Scotland."""
    if source not in {"Contracts Finder", "Public Contracts Scotland"}:
        raise ValueError(f"Unsupported UK portal: {source}")
    today = today or date.today()
    start_date = today - timedelta(days=29)
    cpv_prefixes = {str(code).rstrip("0") for code in (cpv_codes or TENDER_CPV_DEFAULT)}
    source_prefix = "CF" if source == "Contracts Finder" else "PCS"
    fallback_host = ("https://www.contractsfinder.service.gov.uk/Notice/" if source == "Contracts Finder"
                     else "https://www.publiccontractsscotland.gov.uk/search/show/search_view.aspx?ID=")
    rows: list[dict[str, Any]] = []
    for release in response.get("releases", []) or []:
        if not isinstance(release, dict) or "tender" not in (release.get("tag") or ["tender"]):
            continue
        tender = release.get("tender") or {}
        if not isinstance(tender, dict):
            continue
        published = str(tender.get("datePublished") or release.get("date") or "")[:10]
        try:
            published_day = date.fromisoformat(published)
        except ValueError:
            continue
        if not start_date <= published_day <= today:
            continue
        title = str(tender.get("title") or release.get("description") or "").strip()
        if not any(keyword in title.lower() for keyword in TENDER_KEYWORDS):
            continue
        cpv_values: list[str] = []
        classification = tender.get("classification") or {}
        if classification.get("id"):
            cpv_values.append(str(classification["id"]).strip())
        for lot in tender.get("lots", []) or []:
            lot_classification = lot.get("classification") or {}
            if lot_classification.get("id"):
                cpv_values.append(str(lot_classification["id"]).strip())
        for item in tender.get("items", []) or []:
            item_classification = item.get("classification") or {}
            if item_classification.get("id"):
                cpv_values.append(str(item_classification["id"]).strip())
        cpv_values = list(dict.fromkeys(code for code in cpv_values if code))
        matching_cpv = [code for code in cpv_values if any(code.startswith(prefix) for prefix in cpv_prefixes)]
        if not matching_cpv:
            continue
        deadline_values = [str((tender.get("tenderPeriod") or {}).get("endDate") or "")[:10]]
        deadline_values.extend(str((lot.get("tenderPeriod") or {}).get("endDate") or "")[:10]
                               for lot in tender.get("lots", []) or [])
        future_deadlines: list[date] = []
        for value in deadline_values:
            try:
                deadline_day = date.fromisoformat(value)
                if deadline_day > today:
                    future_deadlines.append(deadline_day)
            except ValueError:
                continue
        if not future_deadlines:
            continue
        buyer = release.get("buyer") or {}
        buyer_name = str(buyer.get("name") or "").strip()
        if not buyer_name:
            for party in release.get("parties", []) or []:
                if "buyer" in (party.get("roles") or []):
                    buyer_name = str(party.get("name") or "").strip()
                    if buyer_name:
                        break
        documents = tender.get("documents", []) or []
        url = next((str(doc.get("url")) for doc in documents
                    if doc.get("url") and ("notice" in str(doc.get("documentType", "")).lower()
                                            or source == "Public Contracts Scotland")), "")
        notice_ref = str(release.get("id") or release.get("ocid") or "").strip()
        if source == "Public Contracts Scotland" and documents:
            doc_id = str(next((doc.get("id") for doc in documents
                               if doc.get("url") and doc.get("id")), notice_ref))
            if doc_id:
                url = url or fallback_host + urllib.parse.quote(doc_id, safe="")
        if not url:
            url = fallback_host + urllib.parse.quote(notice_ref, safe="")
        if not notice_ref:
            continue
        rows.append({"notice_id": f"{source_prefix}-{notice_ref}", "title": title, "buyer": buyer_name,
                     "country": "GBR", "cpv": ", ".join(matching_cpv), "published": published,
                     "deadline": min(future_deadlines).isoformat(), "url": url, "status": "new", "notes": "",
                     "first_seen": today.isoformat(), "source": source, "ocid": str(release.get("ocid") or ""),
                     "description": str(tender.get("description") or "").strip(),
                     "documents_json": _ocds_documents(tender)})
    frame = pd.DataFrame(rows, columns=TENDER_COLUMNS)
    return frame.drop_duplicates("notice_id", keep="first").reset_index(drop=True)


def merge_tenders(existing: pd.DataFrame, incoming: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Append unseen notices, retaining user status/notes on notices already cached."""
    old = existing.copy()
    for column in TENDER_COLUMNS:
        if column not in old.columns:
            old[column] = pd.NA
    new = incoming.copy()
    for column in TENDER_COLUMNS:
        if column not in new.columns:
            new[column] = pd.NA
    # Backfill newly added OCDS IDs on cached notices so a later copy from another UK feed dedupes.
    old_ids = old["notice_id"].fillna("").astype(str)
    for _, row in new.iterrows():
        row_ocid = row.get("ocid")
        if pd.isna(row_ocid) or not str(row_ocid).strip():
            continue
        match = old_ids.eq(str(row.get("notice_id") or ""))
        if match.any() and old.loc[match, "ocid"].fillna("").astype(str).str.strip().eq("").all():
            old.loc[match, "ocid"] = row_ocid
        identity_match = match.copy()
        if str(row_ocid).strip():
            identity_match |= old["ocid"].fillna("").astype(str).str.strip().eq(str(row_ocid).strip())
        # Refresh descriptive notice fields when a portal returns them later; keep pipeline status and notes.
        if identity_match.any():
            for detail in ("description", "documents_json"):
                value = row.get(detail)
                if not pd.isna(value) and str(value).strip() and str(value).strip() not in {"[]", "{}"}:
                    old.loc[identity_match, detail] = value
    seen_ids = set(old["notice_id"].fillna("").astype(str))
    seen_ocids = set(old["ocid"].fillna("").astype(str))
    merge_key = new["ocid"].fillna("").astype(str).str.strip()
    merge_key = merge_key.where(merge_key.ne(""), "ID:" + new["notice_id"].fillna("").astype(str))
    new = new.assign(_merge_key=merge_key).drop_duplicates("_merge_key", keep="first").drop(columns="_merge_key")
    additions = new.loc[
        ~new["notice_id"].astype(str).isin(seen_ids)
        & ~new["ocid"].fillna("").astype(str).isin(seen_ocids - {""}), TENDER_COLUMNS]
    combined = pd.concat([old[TENDER_COLUMNS], additions], ignore_index=True)
    return combined, len(additions)


def update_tender_status(tenders: pd.DataFrame, notice_id: str, status: str,
                         notes: str | None = None) -> pd.DataFrame:
    if status not in TENDER_STATUSES:
        raise ValueError(f"Invalid tender status: {status}")
    result = tenders.copy()
    matches = result["notice_id"].astype(str) == str(notice_id)
    if not matches.any():
        raise KeyError(f"Tender {notice_id!r} was not found")
    result.loc[matches, "status"] = status
    if notes is not None:
        result.loc[matches, "notes"] = notes
    return result


def tender_is_held(tender: dict[str, Any] | pd.Series) -> bool:
    country = tender.get("country", "")
    buyer = tender.get("buyer", "")
    country = "" if pd.isna(country) else country
    buyer = "" if pd.isna(buyer) else buyer
    restricted_codes = {"IRN": "Iran", "RUS": "Russia", "BLR": "Belarus",
                        "PRK": "North Korea", "SYR": "Syria", "CUB": "Cuba"}
    country_text = str(country).strip()
    if country_text.upper() in restricted_codes:
        return True
    country_text = restricted_codes.get(country_text.upper(), country_text)
    held, _ = compliance_hold(country_text, buyer)
    return held
QUOTE_VALIDITY_DAYS = 30


class ComplianceHoldError(Exception):
    """Raised when a quote may not be generated for compliance reasons."""


# --- file helpers -------------------------------------------------------------

def write_bytes_atomic(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return path


def safe_filename(name: str) -> str:
    base = Path(str(name)).name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return base or "upload"


def safe_rfq_id(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip()).strip("-")
    if not text:
        raise ValueError("RFQ ID is required (letters, numbers, - and _)")
    return text


def save_upload(name: str, data: bytes, inbox: Path | None = None) -> Path:
    """Save an uploaded RFQ into rfq/inbox/ without ever overwriting an existing file."""
    inbox = inbox or INBOX_DIR
    inbox.mkdir(parents=True, exist_ok=True)
    target = inbox / safe_filename(name)
    counter = 1
    while target.exists():
        target = inbox / f"{target.stem.rsplit('__', 1)[0]}__{counter}{target.suffix}"
        counter += 1
    return write_bytes_atomic(target, data)


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip() == ""


def _num(value: Any, default: float = 0.0) -> float:
    if _is_blank(value):
        return default
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Not a finite number: {value!r}")
    return result


# --- parsed RFQs --------------------------------------------------------------

def list_inbox(inbox: Path | None = None) -> list[Path]:
    inbox = inbox or INBOX_DIR
    if not inbox.exists():
        return []
    return sorted(p for p in inbox.iterdir() if p.is_file() and not p.name.startswith("."))


def rfq_attachment_dir(rfq_id: str, base_dir: Path | None = None) -> Path:
    """Return the local source-document folder for one safely normalized RFQ ID."""
    return (base_dir or (RFQ_DIR / "sources")) / safe_rfq_id(rfq_id)


def save_rfq_attachment(rfq_id: str, name: str, data: bytes,
                        base_dir: Path | None = None) -> Path:
    """Save an RFQ source attachment locally without overwriting an earlier copy."""
    return save_upload(name, data, inbox=rfq_attachment_dir(rfq_id, base_dir))


def list_rfq_attachments(rfq_id: str, base_dir: Path | None = None) -> list[Path]:
    """List local attachments for an RFQ; an absent folder is an empty collection."""
    return list_inbox(rfq_attachment_dir(rfq_id, base_dir))


def _macos_vision_ocr_pdf(data: bytes, *, timeout: int = 300) -> dict[int, str]:
    """OCR scanned PDF pages with macOS Vision, without network access or extra Python packages."""
    if sys.platform != "darwin":
        return {}
    swift = shutil.which("swift")
    helper = ROOT / "scripts" / "mac_vision_pdf_ocr.swift"
    if not swift or not helper.exists():
        raise ValueError("macOS Vision OCR needs the Swift runtime and scripts/mac_vision_pdf_ocr.swift")
    try:
        with tempfile.TemporaryDirectory(prefix="instro-ocr-") as temp_dir:
            pdf_path = Path(temp_dir) / "attachment.pdf"
            pdf_path.write_bytes(data)
            result = subprocess.run([swift, str(helper), str(pdf_path)], capture_output=True, text=True,
                                     timeout=timeout, check=False)
        if result.returncode:
            detail = (result.stderr or "Vision OCR failed").strip()[-600:]
            raise ValueError(f"macOS Vision OCR failed: {detail}")
        raw = json.loads(result.stdout)
        if not isinstance(raw, list):
            raise ValueError("macOS Vision returned an invalid OCR result")
        return {int(page["page"]): str(page.get("text") or "") for page in raw if isinstance(page, dict)}
    except subprocess.TimeoutExpired as exc:
        raise ValueError("macOS Vision OCR took too long. Try splitting the PDF into smaller files.") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("macOS Vision returned unreadable OCR output") from exc


def extract_document_text(filename: str, data: bytes, *, max_chars: int = 100_000) -> dict[str, Any]:
    """Extract searchable text and source locations locally; never infer or populate specifications.

    Supported formats use installed offline libraries or the Python standard library. PDF page
    numbers and spreadsheet sheet/row numbers are preserved for manual citation.
    """
    if not data:
        raise ValueError("The attachment is empty")
    if len(data) > 25 * 1024 * 1024:
        raise ValueError("Attachments larger than 25 MB are not extracted")
    suffix = Path(filename).suffix.lower()
    chunks: list[str] = []
    method = "text"
    ocr_error = ""
    if suffix == ".pdf":
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
            if len(reader.pages) > 1000:
                raise ValueError("PDFs over 1,000 pages are not extracted")
            page_texts = []
            for page_no, page in enumerate(reader.pages, start=1):
                page_text = page.extract_text() or ""
                page_texts.append(page_text.strip())
            if any(not text for text in page_texts) and sys.platform == "darwin":
                try:
                    vision_text = _macos_vision_ocr_pdf(data)
                except ValueError as exc:
                    ocr_error = str(exc)
                else:
                    for page_no, page_text in enumerate(page_texts, start=1):
                        if not page_text:
                            page_texts[page_no - 1] = vision_text.get(page_no, "").strip()
                            if page_texts[page_no - 1]:
                                method = "PDF text + on-device macOS Vision OCR"
            for page_no, page_text in enumerate(page_texts, start=1):
                if page_text:
                    chunks.append(f"[Page {page_no}]\n{page_text}")
            if method == "text":
                method = "PDF text extraction"
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Could not read this PDF: {exc}") from exc
    elif suffix in {".docx"}:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                _check_document_archive(archive)
                xml_data = archive.read("word/document.xml")
            root = ET.fromstring(xml_data)
            namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            body = root.find(f"{namespace}body")
            if body is not None:
                for position, paragraph in enumerate(body.iter(f"{namespace}p"), start=1):
                    text = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t")).strip()
                    if text:
                        chunks.append(f"[Paragraph {position}] {text}")
            method = "DOCX paragraph extraction"
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Could not read this DOCX: {exc}") from exc
    elif suffix in {".xlsx", ".xlsm"}:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                _check_document_archive(archive)
            sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str, nrows=10_000)
            if len(sheets) > 50:
                raise ValueError("Workbooks with more than 50 sheets are not extracted")
            for sheet_name, frame in sheets.items():
                for row_no, row in enumerate(frame.itertuples(index=False, name=None), start=1):
                    cells = [str(value).strip() for value in row if not _is_blank(value)]
                    if cells:
                        chunks.append(f"[Sheet {sheet_name} · Row {row_no}] " + " | ".join(cells))
            method = "spreadsheet text extraction"
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Could not read this workbook: {exc}") from exc
    elif suffix in {".txt", ".csv"}:
        decoded = data.decode("utf-8-sig", errors="replace")
        if suffix == ".csv":
            method = "CSV row extraction"
            for row_no, row in enumerate(csv.reader(io.StringIO(decoded)), start=1):
                values = [value.strip() for value in row if value.strip()]
                if values:
                    chunks.append(f"[Row {row_no}] " + " | ".join(values))
        else:
            method = "plain text extraction"
            chunks = [f"[Line {line_no}] {line}" for line_no, line in enumerate(decoded.splitlines(), start=1)
                      if line.strip()]
    elif suffix == ".eml":
        try:
            message = BytesParser(policy=policy.default).parsebytes(data)
            headers = [f"{name}: {message.get(name)}" for name in ("Subject", "From", "To", "Date")
                       if message.get(name)]
            if headers:
                chunks.append("[Email headers]\n" + "\n".join(headers))
            for part in message.walk():
                if part.get_content_type() == "text/plain" and not part.get_filename():
                    body = part.get_content()
                    if isinstance(body, str) and body.strip():
                        chunks.append("[Email body]\n" + body.strip())
            method = "email text extraction"
        except Exception as exc:
            raise ValueError(f"Could not read this email: {exc}") from exc
    else:
        raise ValueError("Offline extraction supports PDF, DOCX, XLSX, XLSM, CSV, TXT, and EML files")

    combined = "\n\n".join(chunks)
    truncated = len(combined) > max_chars
    if truncated:
        combined = combined[:max_chars] + "\n\n[Text preview truncated; use the original document for the rest.]"
    return {"text": combined, "method": method, "truncated": truncated, "has_text": bool(combined.strip()),
            "ocr_error": ocr_error,
            "characters": len(combined)}


def _check_document_archive(archive: zipfile.ZipFile) -> None:
    entries = archive.infolist()
    if len(entries) > 10_000 or sum(item.file_size for item in entries) > 100 * 1024 * 1024:
        raise ValueError("This compressed document expands beyond the local extraction limit")


def list_parsed_ids(parsed_dir: Path | None = None) -> list[str]:
    parsed_dir = parsed_dir or PARSED_DIR
    if not parsed_dir.exists():
        return []
    return sorted(p.stem for p in parsed_dir.glob("*.json"))


def load_parsed(rfq_id: str, parsed_dir: Path | None = None) -> tuple[list[dict], list[str]]:
    """Return (line items, missing questions) from rfq/parsed/<rfq_id>.json.

    Accepts a list of line objects, or an object with "lines"/"items"/"line_items" plus "missing".
    """
    parsed_dir = parsed_dir or PARSED_DIR
    path = parsed_dir / f"{rfq_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"No parsed RFQ at {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    missing: list[str] = []
    if isinstance(raw, dict):
        missing = [str(m) for m in raw.get("missing", []) or []]
        items = raw.get("lines") or raw.get("items") or raw.get("line_items") or []
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("Parsed RFQ must be a JSON list or object")
    lines: list[dict] = []
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        entry.setdefault("line", position)
        for note in entry.get("missing", []) or []:
            missing.append(f"Line {entry['line']}: {note}")
        lines.append(entry)
    return lines, missing


def parsed_rfq_is_draft(rfq_id: str, parsed_dir: Path | None = None) -> bool:
    """Return whether a parsed RFQ still requires explicit human review."""
    parsed_dir = parsed_dir or PARSED_DIR
    path = parsed_dir / f"{safe_rfq_id(rfq_id)}.json"
    if not path.exists():
        return False
    raw = json.loads(path.read_text(encoding="utf-8"))
    return bool(raw.get("draft", False)) if isinstance(raw, dict) else False


def tender_draft_line(title: str, source_description: str = "") -> tuple[list[dict], list[str]]:
    """Create one clearly provisional notice summary line without inventing a purchasable spec."""
    line = {field: "TBC" for field in MANUAL_LINE_FIELDS}
    line.update({"line": 1,
                 "description": f"Tender notice summary (not a line-item specification): {title.strip() or 'TBC'}",
                 "notes": source_description.strip() or "Review the full tender notice and attachments; replace this summary with actual line items."})
    checklist = ["Expand the notice into the actual requested line items using the notice and attachments.",
                 "Confirm tag / item reference, service or medium, process connection, range and units, accuracy, wetted materials, output / protocol, power, ATEX/IECEx zone, gas group and temperature class, IP rating, ambient and process temperatures, display, quantity, delivery date, and documents required.",
                 "Confirm destination country, end user, and any intermediaries before pricing or supplier outreach."]
    return [line], checklist


TENDER_REVIEW_FIELDS = ["tag", "service", "process_connection", "range", "accuracy", "wetted_material",
                        "output", "power", "hazardous_area_cert", "ip", "ambient_temp", "process_temp",
                        "display", "quantity", "delivery_date", "documents", "source_reference"]


def tender_review_complete(lines: list[dict], questions: list[str]) -> bool:
    """Only release a tender-origin RFQ after line items and every review field are explicitly resolved."""
    if not lines or any(str(question).strip() for question in questions):
        return False
    line_ids: set[str] = set()
    for line in lines:
        description = str(line.get("description") or "").lower()
        if not description.strip() or "tender notice summary (not a line-item specification)" in description:
            return False
        if any(_is_blank(line.get(field)) or str(line.get(field)).strip().upper() == "TBC"
               or (field == "source_reference" and re.search(r"\bTBC\b", str(line.get(field)), re.I))
               for field in TENDER_REVIEW_FIELDS):
            return False
        try:
            if _num(line.get("quantity"), default=0) <= 0:
                return False
        except (TypeError, ValueError):
            return False
        line_id = str(line.get("line") or "").strip()
        if not line_id or line_id in line_ids:
            return False
        line_ids.add(line_id)
    return True


def tbc_fields(line: dict) -> list[str]:
    return [key for key, value in line.items()
            if isinstance(value, str) and value.strip().upper() == "TBC"]


def line_quantity(line: dict) -> float:
    for key in ("quantity", "qty"):
        if key in line and not _is_blank(line[key]):
            try:
                return _num(line[key])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def line_description(line: dict) -> str:
    for key in ("description", "item", "service"):
        if not _is_blank(line.get(key)) and str(line[key]).strip().upper() != "TBC":
            text = str(line[key]).strip()
            break
    else:
        text = "Item"
    extras = [str(line[k]).strip() for k in ("range", "output", "process_connection", "hazardous_area_cert")
              if not _is_blank(line.get(k)) and str(line[k]).strip().upper() != "TBC"]
    return text + (" — " + ", ".join(extras) if extras else "")


# --- RFQ status ---------------------------------------------------------------

def quote_versions(rfq_id: str, quotes_dir: Path | None = None, include_drafts: bool = False) -> list[int]:
    quotes_dir = quotes_dir or QUOTES_DIR
    if not quotes_dir.exists():
        return []
    suffix = r"(?:_DRAFT)?" if include_drafts else ""
    pattern = re.compile(rf"^{re.escape(rfq_id)}_v(\d+){suffix}\.md$")
    return sorted({int(m.group(1)) for p in quotes_dir.iterdir() if (m := pattern.match(p.name))})


def next_version(rfq_id: str, quotes_dir: Path | None = None) -> int:
    versions = quote_versions(rfq_id, quotes_dir, include_drafts=True)
    return (max(versions) + 1) if versions else 1


def derive_status(rfq_id: str, stored: Any, costs: pd.DataFrame,
                  parsed_ids: set[str], quotes_dir: Path | None = None) -> str:
    stored_text = "" if _is_blank(stored) else str(stored).strip().lower()
    if stored_text in FINAL_STATUSES:
        return stored_text
    if quote_versions(rfq_id, quotes_dir):
        return "quoted"
    if not costs.empty and (costs["rfq_id"].astype(str) == rfq_id).any():
        return "priced"
    if rfq_id in parsed_ids:
        return "parsed"
    return stored_text if stored_text in RFQ_STATUSES else "new"


def rfq_overview(rfqs: pd.DataFrame, costs: pd.DataFrame, parsed_dir: Path | None = None,
                 quotes_dir: Path | None = None) -> pd.DataFrame:
    """rfqs.csv rows plus any parsed RFQ not yet registered, with a derived status."""
    parsed_ids = set(list_parsed_ids(parsed_dir))
    frame = rfqs.copy()
    for column in RFQ_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    known = set(frame["rfq_id"].dropna().astype(str))
    extra = [{"rfq_id": rid} for rid in sorted(parsed_ids - known)]
    if extra:
        frame = pd.concat([frame, pd.DataFrame(extra)], ignore_index=True)
    frame["status"] = [derive_status(str(r["rfq_id"]), r["status"], costs, parsed_ids, quotes_dir)
                       for _, r in frame.iterrows()]
    return frame


def upsert_rfq(rfqs: pd.DataFrame, rfq_id: str, **fields: Any) -> pd.DataFrame:
    result = rfqs.copy()
    for column in RFQ_COLUMNS:
        if column not in result.columns:
            result[column] = pd.NA
    match = result["rfq_id"].astype(str) == str(rfq_id)
    unknown = set(fields) - set(result.columns)
    if unknown:
        raise KeyError(f"Unknown RFQ fields: {sorted(unknown)}")
    if match.any():
        for key, value in fields.items():
            result[key] = result[key].astype(object)
            result.loc[match, key] = value
    else:
        row = {column: pd.NA for column in result.columns}
        row.update({"rfq_id": str(rfq_id), **fields})
        result = pd.concat([result, pd.DataFrame([row])], ignore_index=True)
    return result


# --- costs --------------------------------------------------------------------

def upsert_cost(costs: pd.DataFrame, row: dict) -> pd.DataFrame:
    """Add or replace the supplier cost for one (rfq_id, line, supplier_id). Amounts are per unit."""
    for key in ("rfq_id", "line", "supplier_id"):
        if _is_blank(row.get(key)):
            raise ValueError(f"{key} is required")
    clean = {column: row.get(column, pd.NA) for column in COST_COLUMNS}
    clean["rfq_id"] = str(clean["rfq_id"]).strip()
    clean["line"] = str(clean["line"]).strip()
    clean["supplier_id"] = str(clean["supplier_id"]).strip()
    unit_cost = _num(clean["unit_cost_eur"], default=-1)
    if unit_cost <= 0:
        raise ValueError("Unit cost must be greater than 0")
    for key in ("freight_eur", "duty_eur"):
        if _num(clean[key]) < 0:
            raise ValueError(f"{key} cannot be negative")
    result = costs.copy()
    for column in COST_COLUMNS:
        if column not in result.columns:
            result[column] = pd.NA
    if not result.empty:
        same = ((result["rfq_id"].astype(str) == clean["rfq_id"]) &
                (result["line"].astype(str) == clean["line"]) &
                (result["supplier_id"].astype(str) == clean["supplier_id"]))
        result = result.loc[~same]
    return pd.concat([result, pd.DataFrame([clean])], ignore_index=True)


def landed_unit(cost_row: dict | pd.Series) -> float:
    return round(_num(cost_row.get("unit_cost_eur")) + _num(cost_row.get("freight_eur"))
                 + _num(cost_row.get("duty_eur")), 2)


# --- quote building -----------------------------------------------------------

def build_quote_lines(lines: list[dict], costs: pd.DataFrame, rfq_id: str) -> pd.DataFrame:
    """One row per RFQ line using the cheapest landed supplier cost; margin left blank."""
    rows = []
    rfq_costs = costs[costs["rfq_id"].astype(str) == str(rfq_id)] if not costs.empty else costs
    for line in lines:
        line_no = str(line.get("line"))
        options = rfq_costs[rfq_costs["line"].astype(str) == line_no] if not rfq_costs.empty else rfq_costs
        best = None
        if options is not None and not options.empty:
            best = min((r for _, r in options.iterrows()), key=landed_unit)
        rows.append({
            "line": line_no,
            "tag": "" if _is_blank(line.get("tag")) else str(line.get("tag")),
            "description": line_description(line),
            "brand_model": "" if best is None or _is_blank(best.get("model")) else str(best.get("model")),
            "qty": line_quantity(line),
            "supplier_id": "" if best is None else str(best.get("supplier_id")),
            "unit_cost_eur": None if best is None else _num(best.get("unit_cost_eur")),
            "freight_eur": None if best is None else _num(best.get("freight_eur")),
            "duty_eur": None if best is None else _num(best.get("duty_eur")),
            "lead_time": "" if best is None or _is_blank(best.get("lead_time")) else str(best.get("lead_time")),
            "margin": None,
        })
    return pd.DataFrame(rows)


def order_landed_total(table: pd.DataFrame) -> float:
    total = 0.0
    for _, row in table.iterrows():
        if _is_blank(row.get("unit_cost_eur")):
            continue
        total += landed_unit(row) * _num(row.get("qty"))
    return round(total, 2)


def apply_default_margins(table: pd.DataFrame) -> pd.DataFrame:
    """Fill blank margins with the AGENTS.md tier for the order's estimated sell value."""
    result = table.copy()
    est_value = order_landed_total(result) / (1 - 0.25)
    tier = default_margin(est_value)
    result["margin"] = [tier if _is_blank(m) else float(m) for m in result.get("margin", [None] * len(result))]
    return result


def price_quote(table: pd.DataFrame, approved_by_shayan: bool = False) -> pd.DataFrame:
    """Compute landed, unit price and line total. Raises ValueError on missing cost/qty or margin floor."""
    priced = []
    for _, row in table.iterrows():
        line = row.get("line")
        if _is_blank(row.get("unit_cost_eur")):
            raise ValueError(f"Line {line}: no supplier cost entered")
        qty = _num(row.get("qty"))
        if qty <= 0:
            raise ValueError(f"Line {line}: quantity must be greater than 0")
        if _is_blank(row.get("margin")):
            raise ValueError(f"Line {line}: margin missing")
        try:
            calc = calculate_quote_line(row["unit_cost_eur"], _num(row.get("freight_eur")),
                                        _num(row.get("duty_eur")), float(row["margin"]),
                                        approved_by_shayan)
        except ValueError as exc:
            raise ValueError(f"Line {line}: {exc}") from exc
        entry = dict(row)
        entry.update({"qty": qty, "landed_unit_eur": landed_unit(row), "margin": float(row["margin"]),
                      "unit_price_eur": calc["price"], "total_eur": round(calc["price"] * qty, 2)})
        priced.append(entry)
    return pd.DataFrame(priced)


def quote_summary(priced: pd.DataFrame) -> dict[str, float]:
    revenue = float(priced["total_eur"].sum()) if not priced.empty else 0.0
    cost = float((priced["landed_unit_eur"] * priced["qty"]).sum()) if not priced.empty else 0.0
    margin = (revenue - cost) / revenue if revenue else 0.0
    return {"subtotal": round(revenue, 2), "landed_cost": round(cost, 2),
            "gross_profit": round(revenue - cost, 2), "margin": round(margin, 4)}


CUSTOMER_LINE_FIELDS = ("line", "tag", "description", "brand_model", "qty", "unit_price", "total", "lead_time")


def customer_lines(priced: pd.DataFrame, fx_rate: float = 1.0) -> list[dict]:
    """Whitelist of fields allowed in customer documents: never cost, freight, duty, margin or supplier.

    Prices are converted from EUR at fx_rate (1 EUR = fx_rate units of the quote currency).
    """
    rate = float(fx_rate)
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("Exchange rate must be greater than 0")
    rows = []
    for _, row in priced.iterrows():
        unit = round(_num(row.get("unit_price_eur")) * rate, 2)
        qty = _num(row.get("qty"))
        rows.append({"line": row.get("line", ""), "tag": row.get("tag", ""),
                     "description": row.get("description", ""), "brand_model": row.get("brand_model", ""),
                     "qty": qty, "unit_price": unit, "total": round(unit * qty, 2),
                     "lead_time": row.get("lead_time", "")})
    return rows


def _fmt_money(value: Any) -> str:
    return f"{_num(value):,.2f}"


def _fmt_qty(value: Any) -> str:
    number = _num(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _md_cell(value: Any) -> str:
    return ("" if _is_blank(value) else str(value)).replace("|", "/").replace("\n", " ")


def render_quote_markdown(template_text: str, context: dict, lines: list[dict]) -> str:
    from jinja2 import Environment

    rows = "\n".join(
        f"| {_md_cell(l['line'])} | {_md_cell(l['tag'])} | {_md_cell(l['description'])} | "
        f"{_md_cell(l['brand_model'])} | {_fmt_qty(l['qty'])} | {_fmt_money(l['unit_price'])} | "
        f"{_fmt_money(l['total'])} | {_md_cell(l['lead_time'])} |"
        for l in lines
    )
    env = Environment(autoescape=False, keep_trailing_newline=True)
    return env.from_string(template_text).render(**context, lines=rows)


def render_quote_pdf(context: dict, lines: list[dict], logo: Path | None = None, draft: bool = False) -> bytes:
    from io import BytesIO
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    navy = colors.HexColor("#1f3a5f")
    buffer = BytesIO()
    footer_text = " | ".join(str(context.get(k)) for k in ("seller_company", "seller_website", "seller_vat")
                             if context.get(k))

    def on_page(canvas, doc):
        canvas.saveState()
        if draft:
            width, height = landscape(A4)
            canvas.setFillColor(colors.Color(0.85, 0.2, 0.2, alpha=0.12))
            canvas.setFont("Helvetica-Bold", 46)
            canvas.translate(width / 2, height / 2)
            canvas.rotate(25)
            canvas.drawCentredString(0, 0, "DRAFT – PENDING APPROVAL")
            canvas.rotate(-25)
            canvas.translate(-width / 2, -height / 2)
            canvas.setFillColor(colors.HexColor("#b42318"))
            canvas.setFont("Helvetica-Bold", 9)
            canvas.drawString(15 * mm, height - 8 * mm, "DRAFT FOR INTERNAL APPROVAL – NOT VALID FOR ORDER")
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#6b7785"))
        canvas.drawString(15 * mm, 9 * mm, footer_text)
        canvas.drawRightString(landscape(A4)[0] - 15 * mm, 9 * mm,
                               f"{context.get('quote_no', '')}  ·  page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=13 * mm, bottomMargin=16 * mm,
                            title=f"Quotation {context.get('quote_no', '')}",
                            author=str(context.get("seller_company") or context.get("seller_name") or ""))
    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=8.5, leading=10.5)
    right = small.clone("right", alignment=2)
    head = small.clone("head", textColor=colors.white, fontName="Helvetica-Bold")
    title = styles["Title"].clone("qt", fontSize=18, leading=22, alignment=2, textColor=navy, spaceAfter=0)
    label = small.clone("label", textColor=colors.HexColor("#6b7785"))
    bold = small.clone("bold", fontName="Helvetica-Bold")

    def p(text: Any, style=small) -> Paragraph:
        return Paragraph(escape("" if _is_blank(text) else str(text)).replace("\n", "<br/>"), style)

    symbol = context.get("currency_symbol", "€")
    seller_lines = [context.get(k, "") for k in ("seller_company", "seller_address", "seller_email",
                                                   "seller_phone", "seller_website") if context.get(k)]
    left_cell: Any = p("\n".join(seller_lines), bold if not logo else small)
    if logo:
        try:
            from reportlab.lib.utils import ImageReader
            width, height = ImageReader(str(logo)).getSize()
            scale = min(55 * mm / width, 18 * mm / height)
            img = Image(str(logo), width=width * scale, height=height * scale)
            img.hAlign = "LEFT"
            left_cell = [img, Spacer(1, 2 * mm), p("\n".join(seller_lines))]
        except Exception:
            left_cell = p("\n".join(seller_lines))
    top = Table([[left_cell, [Paragraph(f"QUOTATION", title), p(context.get("quote_no", ""), right)]]],
                colWidths=[150 * mm, 117 * mm], hAlign="LEFT")
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story = [top, Spacer(1, 5 * mm)]

    info = [
        [p("To", label), p(f"{context.get('customer_company', '')}\n{context.get('contact_name', '')}"),
         p("Date", label), p(context.get("date", ""))],
        [p("Your ref", label), p(context.get("customer_rfq_ref", "")),
         p("Valid until", label), p(context.get("valid_until", ""))],
        [p("Currency", label), p(context.get("currency", "")),
         p("Incoterms", label), p(context.get("incoterms", ""))],
    ]
    info_table = Table(info, colWidths=[25 * mm, 110 * mm, 25 * mm, 107 * mm], hAlign="LEFT")
    info_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                    ("LINEBELOW", (0, -1), (-1, -1), 0.4, colors.HexColor("#c9d1da"))]))
    story += [info_table, Spacer(1, 5 * mm)]

    data = [[p(h, head) for h in ("Line", "Tag", "Description", "Brand / Model", "Qty",
                                  f"Unit price ({symbol})", f"Total ({symbol})", "Lead time")]]
    for l in lines:
        data.append([p(l["line"]), p(l["tag"]), p(l["description"]), p(l["brand_model"]),
                     p(_fmt_qty(l["qty"]), right), p(_fmt_money(l["unit_price"]), right),
                     p(_fmt_money(l["total"]), right), p(l["lead_time"])])
    data.append([p(""), p(""), p(""), p(""), p(""), p("Subtotal", bold),
                 p(f"{symbol} {context.get('subtotal', '')}", bold), p("excl. VAT", label)])
    table = Table(data, repeatRows=1, colWidths=[12 * mm, 24 * mm, 88 * mm, 45 * mm, 14 * mm,
                                                 26 * mm, 28 * mm, 30 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), navy),
        ("GRID", (0, 0), (-1, -2), 0.4, colors.HexColor("#9aa5b1")),
        ("LINEABOVE", (5, -1), (6, -1), 0.8, navy),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f2f5f8")]),
    ]))
    story += [table, Spacer(1, 5 * mm)]

    terms = [
        [p("Payment terms", label), p(context.get("payment_terms", ""))],
        [p("Documents included", label), p(context.get("documents", ""))],
        [p("Notes / deviations", label), p(context.get("deviations", ""))],
    ]
    story.append(Table(terms, colWidths=[40 * mm, 227 * mm], hAlign="LEFT"))
    story.append(Spacer(1, 5 * mm))
    sign = [p("Prices exclude VAT unless stated. Subject to final confirmation of specifications and export "
              "compliance."), Spacer(1, 3 * mm), p(context.get("seller_name", ""), bold),
            p(context.get("seller_company", ""))]
    story.append(KeepTogether(sign))
    if context.get("terms"):
        story.append(Spacer(1, 6 * mm))
        story.append(KeepTogether([p("Terms and conditions", bold), Spacer(1, 1.5 * mm),
                                   p(context.get("terms"), label)]))
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buffer.getvalue()


def default_deviations(lines: list[dict]) -> str:
    notes = []
    for line in lines:
        fields = tbc_fields(line)
        if fields:
            readable = ", ".join(f.replace("_", " ") for f in fields)
            notes.append(f"Line {line.get('line')}: {readable} to be confirmed by customer.")
    return "\n".join(notes) if notes else "None."


def generate_quote(rfq_id: str, priced: pd.DataFrame, header: dict, *, destination: str,
                   end_user: str, approved_by_shayan: bool = False, today: date | None = None,
                   template_path: Path | None = None, quotes_dir: Path | None = None,
                   currency: str = "EUR", fx_rate: float = 1.0,
                   validity_days: int = QUOTE_VALIDITY_DAYS, logo: Path | None = None,
                   require_approval: bool = False) -> dict:
    """Write quotes/<rfq_id>_vN.md + .pdf and log to CSVs. Raises ComplianceHoldError / ValueError.

    Customer files only ever receive the whitelisted fields in CUSTOMER_LINE_FIELDS.
    """
    rfq_id = safe_rfq_id(rfq_id)
    held, message = compliance_hold(destination, end_user)
    if held:
        raise ComplianceHoldError(message)
    if priced.empty:
        raise ValueError("Quote has no lines")
    for _, row in priced.iterrows():
        validate_margin(row["margin"], approved_by_shayan)
    if currency not in CURRENCIES:
        raise ValueError("Currency must be EUR, GBP or USD")
    fx_rate = 1.0 if currency == "EUR" else float(fx_rate)
    if not math.isfinite(fx_rate) or fx_rate <= 0:
        raise ValueError("Exchange rate must be greater than 0")
    validity_days = int(validity_days)
    if not 1 <= validity_days <= 365:
        raise ValueError("Validity must be between 1 and 365 days")

    today = today or date.today()
    quotes_dir = quotes_dir or QUOTES_DIR
    template_path = template_path or (TEMPLATES_DIR / "quote.md")
    if not template_path.exists():
        raise FileNotFoundError(f"Quote template missing: {template_path}")

    version = next_version(rfq_id, quotes_dir)
    quote_no = f"Q-{rfq_id}-v{version}"
    summary = quote_summary(priced)
    lines = customer_lines(priced, fx_rate)
    subtotal_ccy = round(sum(l["total"] for l in lines), 2)
    context = {
        **{k: ("" if _is_blank(v) else str(v)) for k, v in header.items()},
        "quote_no": quote_no, "date": today.isoformat(),
        "valid_until": (today + timedelta(days=validity_days)).isoformat(),
        "subtotal": _fmt_money(subtotal_ccy), "currency": currency,
        "currency_symbol": CURRENCIES[currency],
    }

    stem = f"{rfq_id}_v{version}" + ("_DRAFT" if require_approval else "")
    md_path = quotes_dir / f"{stem}.md"
    pdf_path = quotes_dir / f"{stem}.pdf"
    template_text = template_path.read_text(encoding="utf-8")
    markdown = render_quote_markdown(template_text, context, lines)
    if require_approval:
        markdown = "> **DRAFT – PENDING APPROVAL. Not valid for order.**\n\n" + markdown
    pdf = render_quote_pdf(context, lines, logo=logo, draft=require_approval)
    write_bytes_atomic(md_path, markdown.encode("utf-8"))
    write_bytes_atomic(pdf_path, pdf)
    _write_snapshot(quote_no, {"rfq_id": rfq_id, "version": version, "context": context, "lines": lines,
                               "logo": logo.name if logo else "", "template": str(template_path),
                               "quotes_dir": str(quotes_dir)})

    # Internal records (data/, never customer-facing).
    log = read_csv("quotes.csv", QUOTE_LOG_COLUMNS)
    log = pd.concat([log, pd.DataFrame([{
        "rfq_id": rfq_id, "sent_date": pd.NA, "value_eur": summary["subtotal"], "margin": summary["margin"],
        "quote_no": quote_no, "version": version, "generated_date": today.isoformat(),
        "md_file": str(md_path.relative_to(ROOT)) if md_path.is_relative_to(ROOT) else str(md_path),
        "pdf_file": str(pdf_path.relative_to(ROOT)) if pdf_path.is_relative_to(ROOT) else str(pdf_path),
        "destination": destination.strip(), "end_user": end_user.strip(),
        "approved_by_shayan": bool(approved_by_shayan), "currency": currency, "fx_rate": fx_rate,
        "value_quote_ccy": subtotal_ccy, "outcome": pd.NA, "outcome_date": pd.NA, "lost_reason": pd.NA,
        "customer_company": context.get("customer_company", ""), "contact_name": context.get("contact_name", ""),
        "customer_email": context.get("customer_email", ""), "valid_until": context["valid_until"],
        "followup_count": 0, "last_followup_date": pd.NA,
        "approval_status": "pending" if require_approval else "not_required", "approver": pd.NA,
        "approval_date": pd.NA, "approval_note": pd.NA,
    }])], ignore_index=True)
    write_csv("quotes.csv", log)

    internal = priced.copy()
    internal["quote_no"], internal["rfq_id"], internal["version"] = quote_no, rfq_id, version
    for column in QUOTE_LINE_COLUMNS:
        if column not in internal.columns:
            internal[column] = pd.NA
    existing = read_csv("quote_lines.csv", QUOTE_LINE_COLUMNS)
    write_csv("quote_lines.csv", pd.concat([existing, internal[QUOTE_LINE_COLUMNS]], ignore_index=True))

    rfqs = read_csv("rfqs.csv", RFQ_COLUMNS)
    current = rfqs.loc[rfqs["rfq_id"].astype(str) == rfq_id, "status"] if not rfqs.empty else pd.Series(dtype=object)
    fields = {"value_eur": summary["subtotal"]}
    if (current.empty or str(current.iloc[0]).lower() not in FINAL_STATUSES) and not require_approval:
        fields["status"] = "quoted"
    write_csv("rfqs.csv", upsert_rfq(rfqs, rfq_id, **fields))

    return {"quote_no": quote_no, "version": version, "md_path": md_path, "pdf_path": pdf_path,
            "summary": summary, "currency": currency, "subtotal_quote_ccy": subtotal_ccy,
            "approval_status": "pending" if require_approval else "not_required"}


def mark_quote_sent(quote_no: str, today: date | None = None) -> pd.DataFrame:
    log = read_csv("quotes.csv", QUOTE_LOG_COLUMNS)
    match = log["quote_no"].astype(str) == str(quote_no)
    if not match.any():
        raise KeyError(f"Quote {quote_no!r} not found")
    status = str(log.loc[match, "approval_status"].iloc[0]).strip().lower()
    if status in {"pending", "rejected"}:
        raise ValueError(f"{quote_no} is {status} approval and cannot be sent")
    log["sent_date"] = log["sent_date"].astype(object)
    log.loc[match, "sent_date"] = (today or date.today()).isoformat()
    write_csv("quotes.csv", log)
    return log


# ---------------------------------------------------------------------------
# Quoting completion: manual RFQs, seller profile, currency, revisions, outcomes
# ---------------------------------------------------------------------------

def save_parsed_rfq(rfq_id: str, lines: list[dict], missing: list[str] | None = None,
                    overwrite: bool = False, parsed_dir: Path | None = None,
                    allow_incomplete: bool = False, draft: bool = False) -> Path:
    """Write a manually entered RFQ to rfq/parsed/<rfq_id>.json (same format Claude produces)."""
    rfq_id = safe_rfq_id(rfq_id)
    parsed_dir = parsed_dir or PARSED_DIR
    path = parsed_dir / f"{rfq_id}.json"
    if path.exists() and not overwrite:
        raise FileExistsError(f"{rfq_id} already exists. Tick overwrite to replace it.")
    clean: list[dict] = []
    seen: set[str] = set()
    for position, raw in enumerate(lines, start=1):
        line = {k: v for k, v in raw.items() if not _is_blank(v)}
        if not line:
            continue
        line_no = str(line.get("line", position)).strip()
        if line_no.endswith(".0"):
            line_no = line_no[:-2]
        if line_no in seen:
            raise ValueError(f"Line number {line_no} is used twice")
        seen.add(line_no)
        if _is_blank(line.get("description")):
            raise ValueError(f"Line {line_no}: description is required")
        if allow_incomplete and str(line.get("quantity", "")).strip().upper() == "TBC":
            qty = None
        else:
            try:
                qty = _num(line.get("quantity"), default=0)
            except (TypeError, ValueError):
                raise ValueError(f"Line {line_no}: quantity must be a number") from None
            if qty <= 0:
                raise ValueError(f"Line {line_no}: quantity must be greater than 0")
        line["line"] = int(line_no) if line_no.isdigit() else line_no
        if qty is not None:
            line["quantity"] = int(qty) if float(qty).is_integer() else qty
        clean.append(line)
    if not clean:
        raise ValueError("Add at least one line item")
    payload = {"lines": clean, "missing": [m for m in (missing or []) if str(m).strip()], "draft": bool(draft)}
    if path.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        (BACKUP_DIR / f"{path.stem}_{_timestamp()}.json").write_bytes(path.read_bytes())
    return write_bytes_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))


def load_seller() -> dict:
    frame = read_csv("seller.csv", SELLER_COLUMNS)
    seller = {c: "" for c in SELLER_COLUMNS}
    if not frame.empty:
        seller.update({k: ("" if _is_blank(v) else str(v)) for k, v in frame.iloc[0].to_dict().items()})
    seller["default_currency"] = seller["default_currency"] if seller["default_currency"] in CURRENCIES else "EUR"
    seller["default_payment_terms"] = seller["default_payment_terms"] or "To be agreed"
    try:
        seller["default_validity_days"] = str(int(float(seller["default_validity_days"] or QUOTE_VALIDITY_DAYS)))
    except ValueError:
        seller["default_validity_days"] = str(QUOTE_VALIDITY_DAYS)
    seller["terms_text"] = seller["terms_text"] or DEFAULT_TERMS
    return seller


def save_seller(values: dict) -> None:
    row = {c: values.get(c, "") for c in SELLER_COLUMNS}
    if row["default_currency"] and row["default_currency"] not in CURRENCIES:
        raise ValueError("Currency must be EUR, GBP or USD")
    if row["seller_email"] and not valid_email(row["seller_email"]):
        raise ValueError("Enter a valid email address")
    if row.get("approver_email") and not valid_email(row["approver_email"]):
        raise ValueError("Enter a valid approver email address")
    if row.get("require_approval") == "yes" and not str(row.get("approver_name") or "").strip():
        raise ValueError("Add the approver's name to require approval")
    write_csv("seller.csv", pd.DataFrame([row]))


def save_logo(data: bytes, suffix: str) -> str:
    suffix = suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg"}:
        raise ValueError("Logo must be PNG or JPG")
    path = DATA_DIR / f"logo{suffix}"
    write_bytes_atomic(path, data)
    return path.name


def logo_path(seller: dict) -> Path | None:
    name = seller.get("logo_file") or ""
    path = DATA_DIR / safe_filename(name) if name else None
    return path if path and path.exists() else None


def previous_quote_lines(rfq_id: str) -> tuple[int | None, pd.DataFrame]:
    lines = read_csv("quote_lines.csv", QUOTE_LINE_COLUMNS)
    if lines.empty:
        return None, lines
    mine = lines[lines["rfq_id"].astype(str) == str(rfq_id)]
    if mine.empty:
        return None, mine
    versions = pd.to_numeric(mine["version"], errors="coerce")
    latest = int(versions.max())
    return latest, mine[versions == latest]


def carry_forward(table: pd.DataFrame, previous: pd.DataFrame) -> pd.DataFrame:
    """Copy the last version's edits (margin, wording, model, qty, lead time) onto a fresh table."""
    if previous is None or previous.empty:
        return table
    result = table.copy()
    prev = {str(r["line"]): r for _, r in previous.iterrows()}
    for idx, row in result.iterrows():
        old = prev.get(str(row["line"]))
        if old is None:
            continue
        for column in ("description", "brand_model", "lead_time", "tag"):
            if not _is_blank(old.get(column)):
                result.at[idx, column] = str(old.get(column))
        if not _is_blank(old.get("qty")):
            result.at[idx, "qty"] = _num(old.get("qty"))
        if not _is_blank(old.get("margin")):
            result.at[idx, "margin"] = float(old.get("margin"))
    return result


def set_quote_outcome(quote_no: str, outcome: str, reason: str = "", today: date | None = None) -> None:
    """Mark a quote won or lost and carry the result to rfqs.csv."""
    outcome = outcome.strip().lower()
    if outcome not in OUTCOMES:
        raise ValueError("Outcome must be won or lost")
    log = read_csv("quotes.csv", QUOTE_LOG_COLUMNS)
    match = log["quote_no"].astype(str) == str(quote_no)
    if not match.any():
        raise KeyError(f"Quote {quote_no!r} not found")
    for column in ("outcome", "outcome_date", "lost_reason", "sent_date"):
        log[column] = log[column].astype(object)
    when = (today or date.today()).isoformat()
    log.loc[match, "outcome"] = outcome
    log.loc[match, "outcome_date"] = when
    log.loc[match, "lost_reason"] = reason.strip() if outcome == "lost" and reason.strip() else pd.NA
    if log.loc[match, "sent_date"].isna().all():
        log.loc[match, "sent_date"] = when
    write_csv("quotes.csv", log)
    rfq_id = str(log.loc[match, "rfq_id"].iloc[0])
    fields = {"status": outcome}
    if outcome == "won":
        fields["value_eur"] = float(log.loc[match, "value_eur"].iloc[0])
    write_csv("rfqs.csv", upsert_rfq(read_csv("rfqs.csv", RFQ_COLUMNS), rfq_id, **fields))


# ---------------------------------------------------------------------------
# Supplier price requests
# ---------------------------------------------------------------------------

from urllib.parse import quote as _urlquote

SUPPLIER_REQUEST_COLUMNS = ["request_id", "rfq_id", "supplier_id", "lines", "requested_date", "reply_by",
                            "destination", "end_use", "include_end_user", "chased_date"]
SPEC_LABELS = {
    "tag": "Tag", "service": "Service / medium", "range": "Range", "accuracy": "Accuracy", "output": "Output",
    "process_connection": "Process connection", "wetted_material": "Wetted material",
    "hazardous_area_cert": "Hazardous area", "ip": "Ingress protection", "power": "Power", "display": "Display",
    "ambient_temp": "Ambient temp", "process_temp": "Process temp", "documents": "Documents",
    "delivery_date": "Delivery date", "source_reference": "Source reference (document / page / clause)",
    "notes": "Notes",
}
_SKIP_SPEC = {"line", "description", "item", "quantity", "qty", "missing"}


def format_line_spec(line: dict) -> str:
    """Plain-text spec block for one RFQ line, as a supplier needs it."""
    title = line.get("description") or line.get("item") or "Item"
    qty = line_quantity(line)
    head = f"Line {line.get('line')}: {title} — Qty {_fmt_qty(qty) if qty else 'TBC'}"
    specs = []
    for key, value in line.items():
        if key in _SKIP_SPEC or _is_blank(value):
            continue
        text = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
        specs.append(f"   - {SPEC_LABELS.get(key, key.replace('_', ' ').capitalize())}: {text}")
    return "\n".join([head] + specs)


def supplier_request_email(rfq_id: str, lines: list[dict], supplier: dict, seller: dict, *, reply_by: date,
                           destination: str, end_use: str, documents: str, end_user: str = "") -> dict:
    """Draft (subject, body) asking one supplier to price the selected lines. Never sent automatically."""
    held, message = compliance_hold(destination, end_user or "not disclosed")
    if held:
        raise ComplianceHoldError(message)
    if not lines:
        raise ValueError("Select at least one line")
    contact = str(supplier.get("contact") or "").strip()
    greeting = f"Dear {contact}," if contact else "Dear Sales Team,"
    descriptions = sorted({str(l.get("description") or l.get("item") or "items").split("—")[0].strip()
                           for l in lines})
    short = ", ".join(descriptions[:3]) + (" and more" if len(descriptions) > 3 else "")
    subject = f"Request for quotation {rfq_id} – {short}"
    destination_line = f"End destination: {destination.strip()}."
    if end_user.strip():
        destination_line += f" End user: {end_user.strip()}."
    body = "\n".join([
        greeting, "",
        "Please quote the following items:", "",
        "\n\n".join(format_line_spec(l) for l in lines), "",
        "Please include for each line: unit price, lead time, country of origin, HS code, Incoterms "
        "and quote validity.",
        f"Documents required: {documents.strip() or 'datasheet, certificate of conformity'}.",
        f"{destination_line} End use: {end_use.strip() or 'industrial process measurement'}.",
        "If any specification above is marked TBC or cannot be met, please propose the closest alternative "
        "and state the deviation.", "",
        f"Please reply by {reply_by.isoformat()}.", "",
        "Best regards,",
        seller.get("seller_name") or "",
        " | ".join(x for x in (seller.get("seller_company"), seller.get("seller_email"),
                               seller.get("seller_phone")) if x),
    ]).rstrip() + "\n"
    return {"subject": subject, "body": body}


def mailto_link(to: str, subject: str, body: str) -> str:
    return f"mailto:{_urlquote(str(to or ''))}?subject={_urlquote(subject)}&body={_urlquote(body)}"


def log_supplier_requests(rfq_id: str, supplier_ids: list[str], line_ids: list[str], *, reply_by: date,
                          destination: str, end_use: str, include_end_user: bool,
                          today: date | None = None) -> pd.DataFrame:
    log = read_csv("supplier_requests.csv", SUPPLIER_REQUEST_COLUMNS)
    today = today or date.today()
    rows = []
    for supplier_id in supplier_ids:
        rows.append({"request_id": f"{rfq_id}-{supplier_id}-{_timestamp()[:15]}", "rfq_id": rfq_id,
                     "supplier_id": supplier_id, "lines": ";".join(map(str, line_ids)),
                     "requested_date": today.isoformat(), "reply_by": reply_by.isoformat(),
                     "destination": destination.strip(), "end_use": end_use.strip(),
                     "include_end_user": bool(include_end_user), "chased_date": pd.NA})
    log = pd.concat([log, pd.DataFrame(rows)], ignore_index=True)
    write_csv("supplier_requests.csv", log)
    return log


def awaiting_supplier_replies(requests: pd.DataFrame, costs: pd.DataFrame,
                              today: date | None = None) -> pd.DataFrame:
    """Requests with no cost entered yet from that supplier for that RFQ; adds days_overdue (<=0 = not due)."""
    today = today or date.today()
    if requests.empty:
        return requests.assign(days_overdue=pd.Series(dtype=int))
    priced = set()
    if not costs.empty:
        priced = set(zip(costs["rfq_id"].astype(str), costs["supplier_id"].astype(str)))
    open_rows = requests[[(str(r), str(s)) not in priced
                          for r, s in zip(requests["rfq_id"], requests["supplier_id"])]].copy()
    reply_by = pd.to_datetime(open_rows["reply_by"], errors="coerce")
    open_rows["days_overdue"] = [(today - d.date()).days if pd.notna(d) else 0 for d in reply_by]
    return open_rows.sort_values("days_overdue", ascending=False)


def mark_supplier_chased(request_id: str, today: date | None = None) -> None:
    log = read_csv("supplier_requests.csv", SUPPLIER_REQUEST_COLUMNS)
    match = log["request_id"].astype(str) == str(request_id)
    if not match.any():
        raise KeyError(f"Request {request_id!r} not found")
    log["chased_date"] = log["chased_date"].astype(object)
    log.loc[match, "chased_date"] = (today or date.today()).isoformat()
    write_csv("supplier_requests.csv", log)


# ---------------------------------------------------------------------------
# Excel / CSV import
# ---------------------------------------------------------------------------

IMPORT_SYNONYMS = {
    "line": ["line", "item no", "item", "no", "pos", "position", "sr", "s/n", "sl no", "line no"],
    "tag": ["tag", "tag no", "tag number", "instrument tag", "tag no."],
    "description": ["description", "desc", "item description", "material description", "instrument",
                    "instrument type", "product", "type"],
    "quantity": ["qty", "quantity", "qnty", "q'ty", "nos", "no. of units", "units", "quantity required"],
    "service": ["service", "medium", "fluid", "process fluid", "media"],
    "range": ["range", "measuring range", "calibrated range", "span", "set range"],
    "accuracy": ["accuracy"],
    "output": ["output", "signal", "output signal", "protocol", "communication"],
    "process_connection": ["process connection", "connection", "conn", "process conn", "flange", "end connection"],
    "wetted_material": ["wetted material", "material", "wetted parts", "moc", "diaphragm material"],
    "hazardous_area_cert": ["hazardous area", "ex", "atex", "certification", "ex rating", "area classification",
                            "haz area"],
    "ip": ["ip", "ip rating", "ingress protection", "enclosure"],
    "notes": ["notes", "remarks", "comment", "comments", "remark"],
}


def read_import_table(data: bytes, filename: str, sheet: str | int | None = 0,
                      header_row: int = 1) -> pd.DataFrame:
    """Read an uploaded .xlsx/.csv; header_row is 1-based as shown in Excel."""
    from io import BytesIO
    if header_row < 1:
        raise ValueError("Header row must be 1 or more")
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(BytesIO(data), header=header_row - 1, dtype=str, keep_default_na=False,
                            encoding_errors="replace")
    elif suffix in {".xlsx", ".xlsm"}:
        frame = pd.read_excel(BytesIO(data), sheet_name=sheet if sheet is not None else 0,
                              header=header_row - 1, dtype=str)
    else:
        raise ValueError("Upload an .xlsx or .csv file (save .xls files as .xlsx first)")
    frame.columns = [str(c).strip() for c in frame.columns]
    frame = frame.loc[:, [not c.lower().startswith("unnamed") for c in frame.columns]]
    return frame.dropna(how="all")


def excel_sheets(data: bytes) -> list[str]:
    from io import BytesIO
    return list(pd.ExcelFile(BytesIO(data)).sheet_names)


def guess_mapping(columns: list[str]) -> dict[str, str | None]:
    """Best-effort mapping of target fields to source column names (exact synonym first, then contains)."""
    normalized = {c: re.sub(r"[^a-z0-9/' ]+", " ", c.lower()).strip() for c in columns}
    normalized = {c: re.sub(r"\s+", " ", n) for c, n in normalized.items()}
    mapping: dict[str, str | None] = {}
    used: set[str] = set()
    for field, synonyms in IMPORT_SYNONYMS.items():
        choice = next((c for c in columns if c not in used and normalized[c] in synonyms), None)
        if choice is None and field not in {"line"}:
            choice = next((c for c in columns if c not in used and any(
                len(s) > 3 and s in normalized[c] for s in synonyms)), None)
        mapping[field] = choice
        if choice:
            used.add(choice)
    return mapping


def parse_quantity(value: Any) -> float | None:
    if _is_blank(value):
        return None
    match = re.search(r"\d+(?:[.,]\d+)?", str(value))
    if not match:
        return None
    number = float(match.group(0).replace(",", "."))
    return number if number > 0 else None


def apply_mapping(frame: pd.DataFrame, mapping: dict[str, str | None],
                  header_row: int = 1) -> tuple[list[dict], list[str]]:
    """Convert source rows to RFQ lines. Returns (lines, skipped-row messages with Excel row numbers)."""
    if not mapping.get("description") or not mapping.get("quantity"):
        raise ValueError("Map at least Description and Quantity")
    lines: list[dict] = []
    skipped: list[str] = []
    for position, (index, row) in enumerate(frame.iterrows(), start=1):
        excel_row = header_row + (int(index) + 1 if isinstance(index, (int,)) else position)
        values = {field: row.get(column) for field, column in mapping.items() if column}
        description = "" if _is_blank(values.get("description")) else str(values["description"]).strip()
        qty = parse_quantity(values.get("quantity"))
        if not description and qty is None:
            continue  # blank / separator row
        if not description:
            skipped.append(f"Row {excel_row}: no description")
            continue
        if qty is None:
            skipped.append(f"Row {excel_row} ({description[:30]}): quantity missing or not a number")
            continue
        line = {k: str(v).strip() for k, v in values.items()
                if k not in {"quantity"} and not _is_blank(v) and str(v).strip()}
        line["description"] = description
        line["quantity"] = int(qty) if qty.is_integer() else qty
        line.setdefault("line", str(len(lines) + 1))
        lines.append(line)
    seen: dict[str, int] = {}
    for line in lines:  # make duplicate line numbers unique instead of failing the whole import
        key = str(line["line"])
        if key in seen:
            seen[key] += 1
            line["line"] = f"{key}.{seen[key]}"
        else:
            seen[key] = 0
    return lines, skipped


# ---------------------------------------------------------------------------
# Quote follow-ups
# ---------------------------------------------------------------------------

FOLLOWUP_DAYS = [3, 7, 14]


def quote_followups_due(log: pd.DataFrame, today: date | None = None) -> pd.DataFrame:
    """Sent quotes with no outcome whose next follow-up (day 3/7/14 after sending) is due."""
    today = today or date.today()
    if log.empty:
        return log.assign(stage=pd.Series(dtype=int), due_date=pd.Series(dtype=object))
    rows = []
    for _, row in log.iterrows():
        if _is_blank(row.get("sent_date")) or not _is_blank(row.get("outcome")):
            continue
        count = int(_num(row.get("followup_count"), 0))
        if count >= len(FOLLOWUP_DAYS):
            continue
        due = date.fromisoformat(str(row["sent_date"])[:10]) + timedelta(days=FOLLOWUP_DAYS[count])
        if due <= today:
            entry = row.to_dict()
            entry.update({"stage": count + 1, "due_date": due.isoformat()})
            rows.append(entry)
    return pd.DataFrame(rows)


def followup_email(quote: dict, seller: dict) -> dict:
    stage = int(quote.get("stage", 1))
    contact = str(quote.get("contact_name") or "").strip()
    greeting = f"Dear {contact}," if contact else "Hello,"
    quote_no = quote.get("quote_no", "")
    valid = "" if _is_blank(quote.get("valid_until")) else str(quote.get("valid_until"))
    if stage == 1:
        subject = f"Quotation {quote_no} – any questions?"
        text = (f"I'm following up on our quotation {quote_no}. Please let me know if anything needs "
                "clarifying, or if any specification should be confirmed or changed.")
    elif stage == 2:
        subject = f"Quotation {quote_no} – alternatives and timing"
        text = (f"Following up on quotation {quote_no}. If lead time or budget is a concern, we can propose "
                "alternative models. Could you share when you expect to make a decision?")
    else:
        subject = f"Quotation {quote_no} – keep open?"
        text = (f"I wanted to check whether quotation {quote_no} is still under consideration"
                f"{f' (valid until {valid})' if valid else ''}. Should I keep it open, or close it on our side?")
    signature = "\n".join(x for x in (seller.get("seller_name"), seller.get("seller_company")) if x)
    body = f"{greeting}\n\n{text}\n\nBest regards,\n{signature}\n"
    return {"subject": subject, "body": body, "stage": stage}


def mark_followup_done(quote_no: str, today: date | None = None) -> None:
    log = read_csv("quotes.csv", QUOTE_LOG_COLUMNS)
    match = log["quote_no"].astype(str) == str(quote_no)
    if not match.any():
        raise KeyError(f"Quote {quote_no!r} not found")
    current = pd.to_numeric(log.loc[match, "followup_count"], errors="coerce").fillna(0).astype(int)
    log["followup_count"] = log["followup_count"].astype(object)
    log["last_followup_date"] = log["last_followup_date"].astype(object)
    log.loc[match, "followup_count"] = (current + 1).values
    log.loc[match, "last_followup_date"] = (today or date.today()).isoformat()
    write_csv("quotes.csv", log)


# ---------------------------------------------------------------------------
# Approval workflow and cover email
# ---------------------------------------------------------------------------

def _snapshot_path(quote_no: str) -> Path:
    return DATA_DIR / SNAPSHOT_DIR_NAME / f"{safe_filename(quote_no)}.json"


def _write_snapshot(quote_no: str, payload: dict) -> None:
    write_bytes_atomic(_snapshot_path(quote_no), json.dumps(payload, indent=2, default=str).encode("utf-8"))


def load_snapshot(quote_no: str) -> dict:
    path = _snapshot_path(quote_no)
    if not path.exists():
        raise FileNotFoundError(f"No saved data for {quote_no}")
    return json.loads(path.read_text(encoding="utf-8"))


def _update_quote_row(quote_no: str, **fields: Any) -> pd.DataFrame:
    log = read_csv("quotes.csv", QUOTE_LOG_COLUMNS)
    match = log["quote_no"].astype(str) == str(quote_no)
    if not match.any():
        raise KeyError(f"Quote {quote_no!r} not found")
    for key, value in fields.items():
        log[key] = log[key].astype(object)
        log.loc[match, key] = value
    write_csv("quotes.csv", log)
    return log


def quote_row(quote_no: str) -> dict:
    log = read_csv("quotes.csv", QUOTE_LOG_COLUMNS)
    match = log[log["quote_no"].astype(str) == str(quote_no)]
    if match.empty:
        raise KeyError(f"Quote {quote_no!r} not found")
    return {k: (None if _is_blank(v) else v) for k, v in match.iloc[0].to_dict().items()}


def approve_quote(quote_no: str, approver: str, note: str = "", today: date | None = None) -> dict:
    """Approve a pending draft: render the final (unwatermarked) Markdown + PDF and unlock sending."""
    row = quote_row(quote_no)
    if str(row.get("approval_status") or "").lower() != "pending":
        raise ValueError(f"{quote_no} is not pending approval")
    if not str(approver or "").strip():
        raise ValueError("Approver name is required")
    snap = load_snapshot(quote_no)
    saved_dir = Path(snap.get("quotes_dir") or QUOTES_DIR)
    quotes_dir = saved_dir if saved_dir.exists() else QUOTES_DIR  # survives moving the project folder
    stem = f"{snap['rfq_id']}_v{snap['version']}"
    md_path, pdf_path = quotes_dir / f"{stem}.md", quotes_dir / f"{stem}.pdf"
    template = Path(snap.get("template") or TEMPLATES_DIR / "quote.md")
    if not template.exists():
        template = TEMPLATES_DIR / "quote.md"
    logo = DATA_DIR / snap["logo"] if snap.get("logo") else None
    logo = logo if logo and logo.exists() else None
    write_bytes_atomic(md_path, render_quote_markdown(template.read_text(encoding="utf-8"), snap["context"],
                                                      snap["lines"]).encode("utf-8"))
    write_bytes_atomic(pdf_path, render_quote_pdf(snap["context"], snap["lines"], logo=logo))
    when = (today or date.today()).isoformat()
    rel = (lambda p: str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p))
    _update_quote_row(quote_no, approval_status="approved", approver=approver.strip(), approval_date=when,
                      approval_note=note.strip() or pd.NA, md_file=rel(md_path), pdf_file=rel(pdf_path))
    rfqs = read_csv("rfqs.csv", RFQ_COLUMNS)
    current = rfqs.loc[rfqs["rfq_id"].astype(str) == snap["rfq_id"], "status"] if not rfqs.empty else pd.Series()
    if current.empty or str(current.iloc[0]).lower() not in FINAL_STATUSES:
        write_csv("rfqs.csv", upsert_rfq(rfqs, snap["rfq_id"], status="quoted"))
    return {"md_path": md_path, "pdf_path": pdf_path}


def reject_quote(quote_no: str, approver: str, note: str, today: date | None = None) -> None:
    row = quote_row(quote_no)
    if str(row.get("approval_status") or "").lower() != "pending":
        raise ValueError(f"{quote_no} is not pending approval")
    if not str(note or "").strip():
        raise ValueError("Give a reason so the next version can fix it")
    _update_quote_row(quote_no, approval_status="rejected", approver=(approver or "").strip() or pd.NA,
                      approval_date=(today or date.today()).isoformat(), approval_note=note.strip())


def approval_request_email(quote_no: str, seller: dict) -> dict:
    """Internal email to the approver: includes costs and margins (never goes to the customer)."""
    row = quote_row(quote_no)
    snap = load_snapshot(quote_no)
    internal = read_csv("quote_lines.csv", QUOTE_LINE_COLUMNS)
    internal = internal[internal["quote_no"].astype(str) == str(quote_no)]
    ctx = snap["context"]
    lines = []
    for _, r in internal.iterrows():
        lines.append(f"  Line {r['line']}: {r.get('brand_model') or '-'} x {_fmt_qty(r['qty'])} | landed "
                     f"€{_num(r['landed_unit_eur']):,.2f} | margin {float(r['margin']):.0%} | "
                     f"price €{_num(r['unit_price_eur']):,.2f} | supplier {r.get('supplier_id') or '-'}")
    revenue = _num(row.get("value_eur"))
    margin = _num(row.get("margin"))
    approver = seller.get("approver_name") or ""
    body = "\n".join([
        f"Hi {approver.split(' ')[0]}," if approver else "Hi,", "",
        f"Please review quotation {quote_no} before it goes to the customer. Draft PDF attached.", "",
        f"Customer: {ctx.get('customer_company') or '-'} ({ctx.get('customer_rfq_ref') or '-'})",
        f"Destination / end user: {row.get('destination') or '-'} / {row.get('end_user') or '-'}",
        f"Customer total: {ctx.get('currency_symbol', '€')}{ctx.get('subtotal')} excl. VAT, "
        f"{ctx.get('incoterms') or '-'}",
        f"Revenue €{revenue:,.2f} | gross profit €{revenue * margin:,.2f} | margin {margin:.1%}"
        + (" | below-12% margin approved by Shayan" if str(row.get("approved_by_shayan")).lower() == "true" else ""),
        "", "Lines:", *lines, "",
        f"Deviations: {ctx.get('deviations') or 'None.'}", "",
        "Reply 'approved' or tell me what to change.", "",
        "Thanks,", seller.get("seller_name") or "",
    ]) + "\n"
    return {"subject": f"Approval needed: {quote_no} – {ctx.get('customer_company') or ''}".rstrip(" –"),
            "body": body, "to": seller.get("approver_email") or ""}


def cover_email(quote_no: str, seller: dict) -> dict:
    """Customer-facing email to send with the final PDF. Contains no cost or margin data."""
    row = quote_row(quote_no)
    if str(row.get("approval_status") or "").lower() in {"pending", "rejected"}:
        raise ValueError(f"{quote_no} is not approved yet")
    snap = load_snapshot(quote_no)
    ctx, lines = snap["context"], snap["lines"]
    contact = (ctx.get("contact_name") or "").strip()
    ref = ctx.get("customer_rfq_ref") or ""
    lead_times = sorted({str(l.get("lead_time")) for l in lines if not _is_blank(l.get("lead_time"))})
    if not lead_times:
        lead_line = "Lead times: to be confirmed."
    elif len(lead_times) == 1:
        lead_line = f"Lead time: {lead_times[0]}."
    else:
        lead_line = "Lead times: " + ", ".join(lead_times) + " depending on the line (see quotation)."
    has_tbc = (ctx.get("deviations") or "None.").strip() not in {"", "None.", "None"}
    body_parts = [
        f"Dear {contact}," if contact else "Dear Sir/Madam,", "",
        f"Thank you for your enquiry{f' {ref}' if ref else ''}. Please find attached our quotation {quote_no}.", "",
        f"Total: {ctx.get('currency_symbol', '€')}{ctx.get('subtotal')} excl. VAT, {ctx.get('incoterms') or ''}".rstrip(", "),
        lead_line,
        f"Valid until: {ctx.get('valid_until')}.", "",
    ]
    if has_tbc:
        body_parts += ["Some specifications are still to be confirmed. They are listed under 'Notes / deviations' "
                       "in the quotation; please confirm them before ordering.", ""]
    body_parts += ["We would be glad to clarify any point or propose alternatives if needed.", "",
                   "Best regards,",
                   *[x for x in (seller.get("seller_name"), seller.get("seller_company"), seller.get("seller_phone"))
                     if x]]
    return {"subject": f"Quotation {quote_no}" + (f" – your ref {ref}" if ref else ""),
            "body": "\n".join(body_parts) + "\n", "to": row.get("customer_email") or ""}


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

BACKUP_ARCHIVE_DIR_NAME = "backups"
BACKUP_INCLUDE = ["data", "quotes", "rfq", "templates", "AGENTS.md", "CODEX_UPDATE.md"]


def create_backup(today: datetime | None = None, dest_dir: Path | None = None) -> Path:
    """Zip data/ (minus its per-write CSV copies), quotes/, rfq/, templates/ into backups/. Returns the zip path."""
    import zipfile
    dest_dir = dest_dir or (ROOT / BACKUP_ARCHIVE_DIR_NAME)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = (today or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = dest_dir / f"instro-backup-{stamp}.zip"
    fd, temp_name = tempfile.mkstemp(prefix=".backup.", suffix=".tmp", dir=dest_dir)
    os.close(fd)
    try:
        with zipfile.ZipFile(temp_name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in BACKUP_INCLUDE:
                path = ROOT / item
                if path.is_file():
                    archive.write(path, item)
                elif path.is_dir():
                    for file in sorted(path.rglob("*")):
                        rel = file.relative_to(ROOT)
                        if not file.is_file() or file.name.startswith(".") or rel.parts[:2] == ("data", "backups"):
                            continue
                        archive.write(file, str(rel))
        os.replace(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return target


def latest_backup(dest_dir: Path | None = None) -> Path | None:
    dest_dir = dest_dir or (ROOT / BACKUP_ARCHIVE_DIR_NAME)
    if not dest_dir.exists():
        return None
    zips = sorted(dest_dir.glob("instro-backup-*.zip"))
    return zips[-1] if zips else None


def backup_age_days(today: date | None = None) -> int | None:
    last = latest_backup()
    if last is None:
        return None
    match = re.search(r"(\d{8})-\d{6}", last.name)
    if not match:
        return None
    return ((today or date.today()) - datetime.strptime(match.group(1), "%Y%m%d").date()).days


# ---------------------------------------------------------------------------
# Weekly review
# ---------------------------------------------------------------------------

def _in_range(series: pd.Series, start: date, end: date) -> pd.Series:
    dates = pd.to_datetime(series.astype("string"), errors="coerce").dt.normalize()
    return dates.notna() & (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))


def review_metrics(start: date, end: date) -> dict:
    rfqs = read_csv("rfqs.csv", RFQ_COLUMNS)
    requests = read_csv("supplier_requests.csv", SUPPLIER_REQUEST_COLUMNS)
    quotes = read_csv("quotes.csv", QUOTE_LOG_COLUMNS)
    leads = read_csv("leads.csv", LEAD_COLUMNS)
    final = quotes[~quotes["approval_status"].astype(str).str.lower().isin({"pending", "rejected"})] \
        if not quotes.empty else quotes
    sent = final[_in_range(final["sent_date"], start, end)] if not final.empty else final
    decided = quotes[_in_range(quotes["outcome_date"], start, end)] if not quotes.empty else quotes
    won = decided[decided["outcome"].astype(str) == "won"] if not decided.empty else decided
    lost = decided[decided["outcome"].astype(str) == "lost"] if not decided.empty else decided
    open_quotes = final[final["sent_date"].notna() & final["outcome"].isna()] if not final.empty else final
    won_value = float(pd.to_numeric(won["value_eur"], errors="coerce").sum()) if not won.empty else 0.0
    won_profit = float((pd.to_numeric(won["value_eur"], errors="coerce")
                        * pd.to_numeric(won["margin"], errors="coerce")).sum()) if not won.empty else 0.0
    sent_margin = pd.to_numeric(sent["margin"], errors="coerce") if not sent.empty else pd.Series(dtype=float)
    lost_reasons = (lost["lost_reason"].fillna("(no reason)").astype(str).value_counts().to_dict()
                    if not lost.empty else {})
    decisions = len(won) + len(lost)
    return {
        "lead_contacts": int(_in_range(leads["last_contact"], start, end).sum()) if not leads.empty else 0,
        "rfqs_received": int(_in_range(rfqs["received_date"], start, end).sum()) if not rfqs.empty else 0,
        "supplier_requests": int(_in_range(requests["requested_date"], start, end).sum()) if not requests.empty else 0,
        "quotes_generated": int(_in_range(quotes["generated_date"], start, end).sum()) if not quotes.empty else 0,
        "quotes_sent": len(sent),
        "value_sent_eur": round(float(pd.to_numeric(sent["value_eur"], errors="coerce").sum()), 2) if len(sent) else 0.0,
        "avg_margin_sent": round(float(sent_margin.mean()), 4) if sent_margin.notna().any() else None,
        "won": len(won), "lost": len(lost),
        "win_rate": round(len(won) / decisions, 4) if decisions else None,
        "won_value_eur": round(won_value, 2), "won_profit_eur": round(won_profit, 2),
        "open_pipeline_eur": round(float(pd.to_numeric(open_quotes["value_eur"], errors="coerce").sum()), 2)
        if not open_quotes.empty else 0.0,
        "lost_reasons": lost_reasons,
    }


def weekly_activity(weeks: int = 8, today: date | None = None) -> pd.DataFrame:
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    rows = []
    for i in range(weeks - 1, -1, -1):
        start = monday - timedelta(weeks=i)
        m = review_metrics(start, start + timedelta(days=6))
        rows.append({"week": start.isoformat(), "RFQs": m["rfqs_received"], "Quotes sent": m["quotes_sent"],
                     "Won": m["won"]})
    return pd.DataFrame(rows).set_index("week")


# ---------------------------------------------------------------------------
# Product catalog
# ---------------------------------------------------------------------------

PRODUCT_COLUMNS = ["product_id", "category", "brand", "model", "measurement", "range", "accuracy", "output",
                   "process_connection", "wetted_material", "haz_cert", "ip", "datasheet_url", "notes",
                   "typical_cost_eur", "preferred_supplier_id"]
PRODUCT_CATEGORIES = ["pressure", "temperature", "flow", "level", "gas detection", "fire detection", "analyzer",
                      "valve", "actuator", "switch", "gauge", "accessory", "other"]
CATEGORY_KEYWORDS = {
    "pressure": ["pressure", "dp", "differential", "manometer", "pt-", "pdt", "pit"],
    "temperature": ["temperature", "rtd", "pt100", "thermocouple", "thermowell", "tt-", "tit"],
    "flow": ["flow", "magnetic", "mag ", "coriolis", "vortex", "ultrasonic", "flowmeter", "ft-", "fit"],
    "level": ["level", "radar", "guided wave", "gwr", "float", "displacer", "lt-", "lit"],
    "gas detection": ["gas detector", "gas detection", "h2s", "lel", "combustible", "toxic gas", "gd-"],
    "fire detection": ["flame", "smoke", "heat detector", "fire"],
    "analyzer": ["analyzer", "analyser", "ph", "conductivity", "oxygen", "dissolved", "turbidity"],
    "valve": ["valve", "ball valve", "globe", "butterfly", "control valve", "psv", "safety valve"],
    "actuator": ["actuator", "mov", "positioner"],
    "switch": ["switch"],
    "gauge": ["gauge", "indicator"],
}
_FEATURE_TOKENS = {
    "output": ["hart", "profibus", "foundation", "fieldbus", "modbus", "4-20", "wirelesshart", "relay"],
    "haz": ["ex ia", "ex d", "ex db", "atex", "iecex", "intrinsically", "explosion", "flameproof", "sil"],
    "material": ["316", "316l", "hastelloy", "monel", "duplex", "titanium", "tantalum", "ptfe", "pvdf"],
    "connection": ["npt", "flange", "ansi", "tri-clamp", "tri clamp", "dn", "g1/2", "g 1/2", "bsp"],
}


def load_products() -> pd.DataFrame:
    return read_csv("products.csv", PRODUCT_COLUMNS)


def _line_text(line: dict) -> str:
    return " ".join(str(v) for k, v in line.items() if k not in {"line", "quantity", "qty"} and not _is_blank(v)
                    ).lower()


def detect_category(text: str) -> str | None:
    text = f" {text.lower()} "
    best, score = None, 0
    for category, words in CATEGORY_KEYWORDS.items():
        hits = sum(1 for w in words if w in text)
        if hits > score:
            best, score = category, hits
    return best


def suggest_products(line: dict, products: pd.DataFrame, top: int = 3) -> list[dict]:
    """Rank catalog products for one RFQ line. Category must match; features add points."""
    if products.empty:
        return []
    text = _line_text(line)
    category = detect_category(text)
    if category is None:
        return []
    results = []
    for _, product in products.iterrows():
        p_cat = str(product.get("category") or "").strip().lower()
        p_text = " ".join(str(product.get(c) or "") for c in PRODUCT_COLUMNS
                          if c not in {"datasheet_url", "typical_cost_eur", "product_id"}).lower()
        if p_cat != category and detect_category(p_text) != category:
            continue
        score, reasons = 3.0, [category]
        for group, tokens in _FEATURE_TOKENS.items():
            shared = [t for t in tokens if t in text and t in p_text]
            if shared:
                score += 1.5 if group in {"output", "haz"} else 1.0
                reasons.append(shared[0])
            elif group == "haz" and any(t in text for t in tokens):
                score -= 2.0  # line needs a hazardous-area cert the product does not mention
                reasons.append("no Ex cert listed")
        results.append({"product_id": product.get("product_id"), "brand": product.get("brand"),
                        "model": product.get("model"), "score": round(score, 1),
                        "why": ", ".join(reasons), "datasheet_url": product.get("datasheet_url"),
                        "typical_cost_eur": product.get("typical_cost_eur"),
                        "preferred_supplier_id": product.get("preferred_supplier_id")})
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top]
