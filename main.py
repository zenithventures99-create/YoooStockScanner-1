#!/usr/bin/env python3
"""
PROJECT ARTHA V6.2 — Expert Indian Stock Intelligence Scanner

Educational/research scanner. Not investment advice. No profitability guarantee.

SUPPORTED UNIVERSE (explicit limitation):
  Exactly five hard-coded companies: RELIANCE, TCS, INFY, HDFCBANK, ICICIBANK.
  This is NOT a market-wide NSE/BSE scanner.

Reporting basis:
  PEAD uses Screener.in consolidated quarterly results only. Standalone and
  consolidated statements are never mixed.

Verification policy:
  - PEAD values are verified only when the quarterly table, reporting basis,
    explicit unit, latest quarter, and comparable prior-year quarter are all
    identified deterministically.
  - Order/buyback monetary values are verified only when present in accessible
    announcement text. PDF-only values are NOT claimed as verified because this
    implementation does not parse PDFs.
  - CMP is third-party delayed data from Yahoo Finance and is labelled as such.

Persistence:
  GitHub Contents API with SHA optimistic locking. If the current state cannot
  be read, the scanner does not write and reports PERSISTENCE_FAILURE. The
  read-modify-write cycle is best-effort; concurrent runs can still race, but
  SHA conflicts are re-read and merged. This is not a transactional database.

Self-tests:
  Offline only. SELF_TEST mode blocks real HTTP. The author could not execute
  the tests in the generation environment; test results are therefore NOT
  claimed as passed. Run `python main.py --self-test` locally or in Actions.
"""
from __future__ import annotations
import argparse
import base64
import json
import logging
import math
import os
import re
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from unittest import mock
import requests
from bs4 import BeautifulSoup
# ----------------------------------------------------------------------------
# Global configuration
# ----------------------------------------------------------------------------
SELF_TEST = False
IST = timezone(timedelta(hours=5, minutes=30), "IST")
TIMEOUT = 10
STATE_PATH = "project-artha-state.json"
STATE_VERSION = 2
MAX_STATE_KEYS = 2000
STATE_MAX_AGE_DAYS = 90
MAX_ORDER_AGE_DAYS = 30
MAX_BUYBACK_AGE_DAYS = 180
MAX_CMP_AGE_DAYS = 7
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 ProjectArtha/6.2"
)
TELEGRAM_METRICS: Dict[str, int] = {"attempted": 0, "delivered": 0}
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
# Hard-coded, explicit identity map. Never infer tickers from unknown names.
TICKER_MAP: Dict[str, Dict[str, str]] = {
    "RELIANCE": {
        "company_name": "Reliance Industries",
        "nse": "RELIANCE",
        "bse": "500325",
        "screener": "reliance-industries",
        "yahoo": "RELIANCE.NS",
    },
    "TCS": {
        "company_name": "Tata Consultancy Services",
        "nse": "TCS",
        "bse": "532540",
        "screener": "tata-consultancy-services",
        "yahoo": "TCS.NS",
    },
    "INFY": {
        "company_name": "Infosys",
        "nse": "INFY",
        "bse": "500208",
        "screener": "infosys",
        "yahoo": "INFY.NS",
    },
    "HDFCBANK": {
        "company_name": "HDFC Bank",
        "nse": "HDFCBANK",
        "bse": "500180",
        "screener": "hdfc-bank",
        "yahoo": "HDFCBANK.NS",
    },
    "ICICIBANK": {
        "company_name": "ICICI Bank",
        "nse": "ICICIBANK",
        "bse": "532174",
        "screener": "icici-bank",
        "yahoo": "ICICIBANK.NS",
    },
}
PAGE_CACHE: Dict[str, str] = {}
REVENUE_CACHE: Dict[str, Optional[float]] = {}
# ----------------------------------------------------------------------------
# Small utilities
# ----------------------------------------------------------------------------
def ist_now() -> datetime:
    return datetime.now(IST)
def ist_now_str() -> str:
    return ist_now().strftime("%Y-%m-%d %H:%M:%S %z")
def ist_today():
    return ist_now().date()
def sanitize_message(text: Any) -> str:
    """Remove credentials from any text that may be logged or sent."""
    value = str(text)
    token = os.getenv("BOT_TOKEN", "").strip()
    chat_id = os.getenv("CHAT_ID", "").strip()
    if token:
        value = value.replace(token, "[REDACTED]")
    if chat_id:
        value = value.replace(chat_id, "[REDACTED]")
    return " ".join(value.split())[:500]
def first_line(text: Any, limit: int = 250) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit]
def resolve_ticker(query: Any) -> Optional[str]:
    """Exact, hard-coded ticker resolution only."""
    q = str(query or "").strip().upper()
    if not q:
        return None
    if q in TICKER_MAP:
        return q
    for symbol, info in TICKER_MAP.items():
        if q == info["nse"].upper() or q == str(info["bse"]).upper():
            return symbol
        if q == info["company_name"].upper():
            return symbol
    return None
def utf16_len(text: str) -> int:
    """Telegram limits are measured in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2
def split_message(text: str, limit: int = 4000) -> List[str]:
    """Split at newline boundaries without exceeding Telegram's 4096-unit limit."""
    text = text or ""
    if utf16_len(text) <= limit:
        return [text]
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for line in text.splitlines():
        line_len = utf16_len(line) + 1
        if current_len + line_len > limit:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            while utf16_len(line) > limit:
                # Hard cut a pathological single line by characters.
                cut = line
                while utf16_len(cut) > limit:
                    cut = cut[:-1]
                chunks.append(cut)
                line = line[len(cut):]
            if line:
                current = [line]
                current_len = utf16_len(line) + 1
        else:
            current.append(line)
            current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks
# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------
def _http_request(
    method: str,
    url: str,
    *,
    session: Optional[requests.Session] = None,
    expected_types: Optional[Iterable[str]] = None,
    raise_for_status: bool = True,
    **kwargs: Any,
) -> requests.Response:
    """
    Central HTTP helper. Every real request gets timeout=10.

    In self-test mode this raises immediately unless a test patches this
    function or the lower-level _http_get/_http_post/_http_put.
    """
    if SELF_TEST:
        raise RuntimeError("SELF_TEST network guard triggered")
    kwargs.setdefault("timeout", TIMEOUT)
    requester = session or requests
    response = requester.request(method, url, **kwargs)
    if raise_for_status:
        response.raise_for_status()
    if expected_types:
        content_type = response.headers.get("Content-Type", "") or ""
        if not any(item.lower() in content_type.lower() for item in expected_types):
            raise ValueError(f"Unexpected content type {content_type} from {url}")
    return response
def _http_get(url: str, **kwargs: Any) -> requests.Response:
    return _http_request("GET", url, **kwargs)
def _http_post(url: str, **kwargs: Any) -> requests.Response:
    return _http_request("POST", url, **kwargs)
def _http_put(url: str, **kwargs: Any) -> requests.Response:
    return _http_request("PUT", url, **kwargs)
# ----------------------------------------------------------------------------
# Numeric parsing and normalization
# ----------------------------------------------------------------------------
def parse_float(token: Any) -> Optional[float]:
    if token is None:
        return None
    s = str(token).strip()
    if not s or s in {"-", "--", "NA", "N/A", "nan", "NaN"}:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    s = s.replace(",", "")
    s = re.sub(r"[^0-9.\-]", "", s)
    if not s or s in {"-", ".", "--"}:
        return None
    try:
        value = float(s)
    except ValueError:
        return None
    if negative and value > 0:
        value = -value
    return value
def normalize_unit_key(unit: Optional[str]) -> Optional[str]:
    if not unit:
        return None
    u = str(unit).lower().strip().rstrip(".")
    if u in {"crore", "crores", "cr"}:
        return "crore"
    if u in {"lakh", "lakhs", "lac", "lacs"}:
        return "lakh"
    if u in {"million", "mn"}:
        return "million"
    if u in {"billion", "bn"}:
        return "billion"
    return None
def normalize_amount_to_cr(value: Optional[float], unit: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    unit_key = normalize_unit_key(unit)
    if unit_key == "crore":
        return float(value)
    if unit_key == "lakh":
        return float(value) / 100.0
    if unit_key == "million":
        return float(value) * 0.1
    if unit_key == "billion":
        return float(value) * 100.0
    return None
FOREIGN_CURRENCY_RE = re.compile(
    r"\b(?:USD|EUR|GBP|JPY|SGD|AED|CHF|CAD|AUD)\b|\$|€|£|¥", re.I
)
INR_CURRENCY_RE = re.compile(r"₹|\bRS\b|\bRS\.?\b|\bINR\b|INDIAN\s+RUPEE", re.I)
def detect_currency(context: str) -> Optional[str]:
    context = context or ""
    if FOREIGN_CURRENCY_RE.search(context):
        return "FOREIGN"
    if INR_CURRENCY_RE.search(context):
        return "INR"
    return None
# ----------------------------------------------------------------------------
# Telegram delivery
# ----------------------------------------------------------------------------
class TelegramResult:
    __slots__ = ("delivered", "attempts", "permanent_error", "error")
    def __init__(self):
        self.delivered = False
        self.attempts = 0
        self.permanent_error = False
        self.error = ""
def _send_telegram_chunk(
    url: str,
    chat_id: str,
    chunk: str,
    *,
    max_attempts: int = 3,
) -> TelegramResult:
    result = TelegramResult()
    for attempt in range(1, max_attempts + 1):
        result.attempts += 1
        try:
            response = _http_post(
                url,
                data={"chat_id": chat_id, "text": chunk},
                raise_for_status=False,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            result.error = type(exc).__name__
            if attempt < max_attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
                continue
            return result
        except Exception as exc:  # Isolate unexpected delivery failures.
            result.error = sanitize_message(exc)
            result.permanent_error = True
            return result
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if (
            response.status_code // 100 == 2
            and isinstance(payload, dict)
            and payload.get("ok") is True
        ):
            result.delivered = True
            result.error = ""
            return result
        # Permanent API errors: do not retry.
        if response.status_code in (400, 401, 403, 404):
            result.error = f"HTTP {response.status_code}"
            result.permanent_error = True
            return result
        if response.status_code == 429:
            retry_after = 1
            if isinstance(payload, dict):
                params = payload.get("parameters") or {}
                if isinstance(params, dict):
                    retry_after = int(params.get("retry_after") or 1)
            if attempt < max_attempts:
                time.sleep(min(max(retry_after, 1), 10))
                continue
            result.error = "HTTP 429"
            return result
        if response.status_code >= 500:
            result.error = f"HTTP {response.status_code}"
            if attempt < max_attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
                continue
            return result
        # Any other non-2xx response is treated as permanent.
        result.error = f"HTTP {response.status_code}"
        result.permanent_error = True
        return result
    return result
def send_telegram_alert(message: str, metrics: Optional[Dict[str, int]] = None) -> bool:
    """
    Send plain-text Telegram message(s).

    Confirmed only when HTTP is successful AND JSON contains ok:true.
    Every chunk is counted as one attempted message. A failed chunk makes the
    overall return False but does not prevent remaining chunks from being tried.
    """
    if metrics is None:
        metrics = TELEGRAM_METRICS
    token = os.getenv("BOT_TOKEN", "").strip()
    chat_id = os.getenv("CHAT_ID", "").strip()
    if not message or not token or not chat_id:
        logging.warning("Telegram delivery skipped: missing message or credentials.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    all_ok = True
    for chunk in split_message(message):
        metrics["attempted"] += 1
        result = _send_telegram_chunk(url, chat_id, chunk)
        if result.delivered:
            metrics["delivered"] += 1
        else:
            all_ok = False
            logging.warning(
                "Telegram chunk failed: attempts=%s permanent=%s error=%s",
                result.attempts,
                result.permanent_error,
                sanitize_message(result.error),
            )
    return all_ok
# ----------------------------------------------------------------------------
# GitHub Contents API persistence
# ----------------------------------------------------------------------------
def _github_api_headers() -> Optional[Dict[str, str]]:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
def _github_branch() -> Optional[str]:
    branch = os.getenv("GITHUB_REF_NAME", "").strip()
    if branch:
        return branch
    ref = os.getenv("GITHUB_REF", "").strip()
    if ref.startswith("refs/heads/"):
        return ref.split("/")[-1]
    return None
class StateRead:
    __slots__ = ("ok", "exists", "keys", "sha", "error_kind", "error")
    def __init__(self):
        self.ok = False
        self.exists = False
        self.keys: Dict[str, str] = {}
        self.sha: Optional[str] = None
        self.error_kind = "NONE"
        self.error = ""
def read_state() -> StateRead:
    """
    Read persisted keys and their timestamps.

    Distinguishes a genuinely missing file (404) from auth, rate-limit, server,
    malformed-response, and network failures. A failed read MUST NOT be treated
    as an empty state by the caller.
    """
    result = StateRead()
    headers = _github_api_headers()
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not headers or not repo:
        result.error_kind = "MISSING_CREDENTIALS"
        result.error = "Missing GITHUB_TOKEN or GITHUB_REPOSITORY."
        return result
    url = f"https://api.github.com/repos/{repo}/contents/{STATE_PATH}"
    branch = _github_branch()
    params = {"ref": branch} if branch else None
    try:
        response = _http_get(
            url,
            headers=headers,
            params=params,
            expected_types=("application/json",),
            raise_for_status=False,
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        result.error_kind = "NETWORK"
        result.error = sanitize_message(f"State read network failure: {exc}")
        return result
    except Exception as exc:  # Defensive isolation.
        result.error_kind = "NETWORK"
        result.error = sanitize_message(f"State read unexpected failure: {exc}")
        return result
    if response.status_code == 404:
        result.ok = True
        result.exists = False
        result.keys = {}
        result.sha = None
        return result
    if response.status_code == 429:
        result.error_kind = "RATE_LIMIT"
        result.error = "State read rate limited (HTTP 429)."
        return result
    if response.status_code in (401, 403):
        remaining = response.headers.get("x-ratelimit-remaining", "")
        if str(remaining).strip() == "0":
            result.error_kind = "RATE_LIMIT"
            result.error = "State read rate limited (HTTP 403, remaining=0)."
        else:
            result.error_kind = "AUTH"
            result.error = f"State read authorization failure (HTTP {response.status_code})."
        return result
    if response.status_code // 100 != 2:
        result.error_kind = "SERVER"
        result.error = f"State read HTTP {response.status_code}."
        return result
    try:
        payload = response.json()
        content = payload.get("content", "") or ""
        decoded = base64.b64decode(content).decode("utf-8") if content else "{}"
        state = json.loads(decoded)
        raw_keys = state.get("keys", {})
        if isinstance(raw_keys, list):
            # Legacy list format: assign current timestamp so retention can work.
            now_iso = ist_now().isoformat()
            keys = {str(k): now_iso for k in raw_keys if str(k).strip()}
        elif isinstance(raw_keys, dict):
            keys = {}
            for k, v in raw_keys.items():
                if not str(k).strip():
                    continue
                keys[str(k)] = str(v) if v else ist_now().isoformat()
        else:
            raise ValueError("Invalid state keys container.")
        result.ok = True
        result.exists = True
        result.keys = keys
        result.sha = payload.get("sha")
        return result
    except (ValueError, TypeError, KeyError) as exc:
        result.error_kind = "MALFORMED"
        result.error = sanitize_message(f"State read malformed payload: {exc}")
        return result
    except Exception as exc:  # Defensive isolation.
        result.error_kind = "MALFORMED"
        result.error = sanitize_message(f"State read malformed payload: {exc}")
        return result
def _prune_state(keys: Dict[str, str]) -> Dict[str, str]:
    """
    Retain recent keys by actual timestamp, never lexicographic order.
    """
    now = ist_now()
    cutoff = now - timedelta(days=STATE_MAX_AGE_DAYS)
    parsed: List[Tuple[datetime, str]] = []
    for key, raw_ts in keys.items():
        try:
            ts = datetime.fromisoformat(str(raw_ts))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
        except Exception:
            # Unknown timestamp: keep it but sort it as oldest.
            ts = cutoff - timedelta(days=1)
        if ts >= cutoff:
            parsed.append((ts, key))
    parsed.sort(key=lambda item: item[0], reverse=True)
    retained = parsed[:MAX_STATE_KEYS]
    return {key: ts.isoformat() for ts, key in retained}
def save_state(new_keys: Dict[str, str], max_attempts: int = 3) -> Tuple[bool, str]:
    """
    Save dedup keys with SHA optimistic locking.

    If reading the current state fails, this function refuses to write. It never
    silently replaces an unreadable file with a partial state.
    """
    if not new_keys:
        return True, ""
    headers = _github_api_headers()
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not headers or not repo:
        return False, "Missing GITHUB_TOKEN or GITHUB_REPOSITORY."
    url = f"https://api.github.com/repos/{repo}/contents/{STATE_PATH}"
    branch = _github_branch()
    for attempt in range(max_attempts):
        current = read_state()
        if not current.ok:
            return False, f"State read failed before write: {current.error_kind}"
        merged = dict(current.keys)
        merged.update(new_keys)
        merged = _prune_state(merged)
        payload: Dict[str, Any] = {
            "message": f"Project Artha state update {ist_now_str()}",
            "content": base64.b64encode(
                json.dumps(
                    {"version": STATE_VERSION, "keys": merged},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).decode("utf-8"),
        }
        if current.sha:
            payload["sha"] = current.sha
        if branch:
            payload["branch"] = branch
        try:
            response = _http_put(
                url,
                headers=headers,
                json=payload,
                expected_types=("application/json",),
                raise_for_status=False,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt < max_attempts - 1:
                time.sleep(min(2 ** attempt, 8))
                continue
            return False, sanitize_message(f"State write network failure: {exc}")
        except Exception as exc:  # Defensive isolation.
            return False, sanitize_message(f"State write unexpected failure: {exc}")
        if response.status_code in (200, 201):
            return True, ""
        if response.status_code == 409:
            # Someone else wrote first. Re-read and merge, bounded retries.
            if attempt < max_attempts - 1:
                time.sleep(min(1 + attempt, 3))
                continue
            return False, "State write SHA conflict after retries."
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < max_attempts - 1:
                time.sleep(min(2 ** attempt, 8))
                continue
            return False, f"State write HTTP {response.status_code} after retries."
        # 422 and other 4xx are permanent; do NOT switch branches silently.
        return False, f"State write HTTP {response.status_code}."
    return False, "State write exhausted retries."
# ----------------------------------------------------------------------------
# Quarter / date parsing
# ----------------------------------------------------------------------------
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
FISCAL_QUARTER_BY_MONTH = {
    "jun": "Q1",
    "sep": "Q2",
    "dec": "Q3",
    "mar": "Q4",
}
QUARTER_MONTH_END_DAY = {3: 31, 6: 30, 9: 30, 12: 31}
def parse_quarter_label(label: Any) -> Optional[Dict[str, Any]]:
    if not label:
        return None
    s = str(label).strip()
    match = re.search(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s\-']*([0-9]{4})\b",
        s,
        re.I,
    )
    if not match:
        return None
    month_key = match.group(1).lower()
    if month_key not in FISCAL_QUARTER_BY_MONTH:
        return None
    year = int(match.group(2))
    month_num = MONTHS[month_key]
    day = QUARTER_MONTH_END_DAY[month_num]
    return {
        "label": s,
        "year": year,
        "month": month_key,
        "fiscal_quarter": FISCAL_QUARTER_BY_MONTH[month_key],
        "end_date": datetime(year, month_num, day).date(),
    }
def find_latest_and_previous_parsed(
    parsed_items: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    if not parsed_items:
        return None, None
    latest = max(parsed_items, key=lambda item: item["end_date"])
    previous = next(
        (
            item
            for item in parsed_items
            if item["month"] == latest["month"] and item["year"] == latest["year"] - 1
        ),
        None,
    )
    return latest, previous
def parse_announcement_date(value: Any):
    if not value:
        return None
    s = str(value).strip()
    match = re.search(r"(\d{4}-\d{2}-\d{2})", s)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass
    match = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})", s)
    if match:
        try:
            return datetime.strptime(match.group(1), "%d-%b-%Y").date()
        except ValueError:
            pass
    match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", s)
    if match:
        try:
            return datetime.strptime(match.group(1), "%d/%m/%Y").date()
        except ValueError:
            pass
    match = re.search(r"([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})", s)
    if match:
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
            try:
                return datetime.strptime(match.group(1), fmt).date()
            except ValueError:
                continue
    return None
# ----------------------------------------------------------------------------
# HTML / Screener parsing helpers
# ----------------------------------------------------------------------------
UNIT_PATTERNS = (
    ("crore", re.compile(r"\bcrores?\b|\bcr\b", re.I)),
    ("lakh", re.compile(r"\blakhs?\b|\blacs?\b", re.I)),
    ("million", re.compile(r"\bmillions?\b|\bmn\b", re.I)),
    ("billion", re.compile(r"\bbillions?\b|\bbn\b", re.I)),
)
FOREIGN_UNIT_PATTERN = re.compile(r"\busd\b|\bus\$\b|\$|€|£", re.I)
def detect_unit_from_text(text: str) -> Tuple[Optional[str], str]:
    """
    Detect an explicit unit. Never assume a missing unit means INR crore.
    Foreign currency in the section makes the section unverified.
    """
    if not text:
        return None, "UNIT_NOT_FOUND"
    if FOREIGN_UNIT_PATTERN.search(text):
        return None, "FOREIGN_CURRENCY"
    found = {name for name, pattern in UNIT_PATTERNS if pattern.search(text)}
    if len(found) == 1:
        return found.pop(), "OK"
    if len(found) > 1:
        return None, "AMBIGUOUS_UNIT"
    return None, "UNIT_NOT_FOUND"
def find_header_columns(
    rows: List[Any], require_annual: bool = False
) -> Tuple[Optional[int], Dict[int, Dict[str, Any]]]:
    for row_index, row in enumerate(rows):
        cells = row.find_all(["th", "td"])
        columns: Dict[int, Dict[str, Any]] = {}
        for cell_index, cell in enumerate(cells):
            parsed = parse_quarter_label(cell.get_text(" ", strip=True))
            if parsed:
                columns[cell_index] = parsed
        if not columns:
            continue
        if require_annual:
            if all(item["month"] == "mar" for item in columns.values()):
                return row_index, columns
        else:
            if len(columns) >= 2:
                return row_index, columns
    return None, {}
def parse_table_value(raw: Any, unit: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in {"-", "--", "NA", "N/A"}:
        return None
    explicit_unit = None
    if re.search(r"\bcrores?\b|\bcr\b", s, re.I):
        explicit_unit = "crore"
    elif re.search(r"\blakhs?\b|\blacs?\b", s, re.I):
        explicit_unit = "lakh"
    value = parse_float(s)
    if value is None:
        return None
    return normalize_amount_to_cr(value, explicit_unit or unit)
def fetch_screener_page(url: str, info: Dict[str, str]) -> str:
    response = _http_get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        expected_types=("text/html",),
    )
    return response.text
def fetch_screener_page_cached(url: str, info: Dict[str, str]) -> str:
    if url not in PAGE_CACHE:
        PAGE_CACHE[url] = fetch_screener_page(url, info)
    return PAGE_CACHE[url]
# ----------------------------------------------------------------------------
# Module A: PEAD / quarterly profit growth
# ----------------------------------------------------------------------------
def evaluate_profit_growth(
    current_profit: Optional[float], previous_profit: Optional[float]
) -> Dict[str, Any]:
    if current_profit is None or previous_profit is None:
        return {"eligible": False, "reason": "MISSING_VALUE"}
    if math.isclose(previous_profit, 0.0, abs_tol=1e-9):
        return {
            "eligible": False,
            "reason": "PREVIOUS_ZERO",
            "event": "ZERO_TO_PROFIT" if current_profit > 0 else "ZERO_TO_NONPOSITIVE",
        }
    if previous_profit < 0:
        return {
            "eligible": False,
            "reason": "PREVIOUS_NEGATIVE",
            "event": "LOSS_TO_PROFIT" if current_profit > 0 else "LOSS_TO_LOSS",
        }
    growth_pct = ((current_profit - previous_profit) / previous_profit) * 100.0
    eligible = growth_pct > 100.0 and not math.isclose(growth_pct, 100.0, abs_tol=1e-9)
    return {
        "eligible": eligible,
        "growth_pct": growth_pct,
        "reason": "OK" if eligible else "THRESHOLD_NOT_MET",
    }
def parse_pead_from_html(html_text: str, ticker_info: Dict[str, str]) -> Dict[str, Any]:
    base = {
        "verified": False,
        "eligible": False,
        "reason": "UNVERIFIED",
        "basis": "consolidated",
        "quarter": None,
        "previous_quarter": None,
        "current_profit_cr": None,
        "previous_profit_cr": None,
        "growth_pct": None,
        "unit": "INR crore",
    }
    try:
        soup = BeautifulSoup(html_text, "html.parser")
    except Exception:
        return {**base, "reason": "HTML_PARSE_FAILED"}
    section = soup.find("section", id="quarters")
    if section is None:
        # Conservative fallback: only accept a section whose heading says
        # "quarterly results". Never scan arbitrary tables.
        for candidate in soup.find_all("section"):
            heading = candidate.find(["h1", "h2", "h3"])
            if heading and re.search(r"quarterly\s+results", heading.get_text(" ", strip=True), re.I):
                section = candidate
                break
    if section is None:
        return {**base, "reason": "QUARTERLY_SECTION_NOT_FOUND"}
    section_text = section.get_text(" ", strip=True)
    unit, unit_reason = detect_unit_from_text(section_text)
    if not unit:
        return {**base, "reason": unit_reason}
    tables = section.find_all("table")
    if not tables:
        return {**base, "reason": "QUARTERLY_TABLE_NOT_FOUND"}
    for table in tables:
        rows = table.find_all("tr")
        header_index, quarter_columns = find_header_columns(rows, require_annual=False)
        if header_index is None or len(quarter_columns) < 2:
            continue
        latest, previous = find_latest_and_previous_parsed(list(quarter_columns.values()))
        if not latest or not previous:
            continue
        latest_index = next(
            (idx for idx, item in quarter_columns.items() if item["end_date"] == latest["end_date"]),
            None,
        )
        previous_index = next(
            (
                idx
                for idx, item in quarter_columns.items()
                if item["month"] == previous["month"] and item["year"] == previous["year"]
            ),
            None,
        )
        if latest_index is None or previous_index is None:
            continue
        period_delta_days = abs((latest["end_date"] - previous["end_date"]).days - 365)
        if period_delta_days > 100:
            continue
        for row in rows[header_index + 1:]:
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            row_label = cells[0].get_text(" ", strip=True).lower()
            if not re.search(r"net profit|profit after tax|\bpat\b|net income", row_label):
                continue
            if len(cells) <= max(latest_index, previous_index):
                continue
            current_raw = cells[latest_index].get_text(" ", strip=True)
            previous_raw = cells[previous_index].get_text(" ", strip=True)
            current_profit = parse_table_value(current_raw, unit)
            previous_profit = parse_table_value(previous_raw, unit)
            if current_profit is None or previous_profit is None:
                return {**base, "reason": "MISSING_OR_INVALID_PROFIT"}
            growth = evaluate_profit_growth(current_profit, previous_profit)
            return {
                "verified": True,
                "eligible": growth.get("eligible", False),
                "reason": growth.get("reason", "OK"),
                "event": growth.get("event"),
                "basis": "consolidated",
                "quarter": latest["label"],
                "previous_quarter": previous["label"],
                "current_profit_cr": current_profit,
                "previous_profit_cr": previous_profit,
                "growth_pct": growth.get("growth_pct"),
                "unit": "INR crore",
            }
    return {**base, "reason": "QUARTER_TABLE_NOT_FOUND"}
def build_pead_alert(
    symbol: str, info: Dict[str, str], parsed: Dict[str, Any], source_url: str
) -> str:
    return "\n".join(
        [
            "PEAD PROFIT GROWTH (EDUCATIONAL SCREENING)",
            f"{info['company_name']} ({symbol})",
            "Reporting basis: consolidated",
            f"Quarter: {parsed['quarter']} vs {parsed['previous_quarter']}",
            f"Current profit: {parsed['current_profit_cr']:.2f} {parsed['unit']}",
            f"Previous-year profit: {parsed['previous_profit_cr']:.2f} {parsed['unit']}",
            f"Growth: {parsed['growth_pct']:.2f}%",
            f"Source: {source_url}",
            f"Scan time: {ist_now_str()}",
            "A profit-growth signal is not proof of post-earnings drift or a profitable trade.",
        ]
    )
def scan_pead(
    tickers: Dict[str, Dict[str, str]],
    known_keys: Set[str],
    new_keys: Set[str],
    fetch_html: Optional[Callable[[str, Dict[str, str]], str]] = None,
) -> Tuple[List[Dict[str, str]], str, int, List[str]]:
    alerts: List[Dict[str, str]] = []
    skipped = 0
    errors: List[str] = []
    successful_sources = 0
    failed_sources = 0
    fetcher = fetch_html or fetch_screener_page_cached
    for symbol, info in tickers.items():
        url = f"https://www.screener.in/company/{info['screener']}/consolidated/"
        try:
            html_text = fetcher(url, info)
            successful_sources += 1
        except Exception as exc:  # Isolate one ticker.
            failed_sources += 1
            skipped += 1
            errors.append(sanitize_message(f"PEAD fetch failed for {symbol}: {exc}"))
            continue
        try:
            parsed = parse_pead_from_html(html_text, info)
        except Exception as exc:  # Isolate one parse.
            failed_sources += 1
            skipped += 1
            errors.append(sanitize_message(f"PEAD parse failed for {symbol}: {exc}"))
            continue
        if not parsed.get("verified") or not parsed.get("eligible"):
            skipped += 1
            continue
        key = f"PEAD:{symbol}:{parsed['quarter']}"
        if key in known_keys:
            continue
        known_keys.add(key)
        new_keys.add(key)
        alerts.append(
            {
                "module": "PEAD",
                "key": key,
                "message": build_pead_alert(symbol, info, parsed, url),
            }
        )
    if successful_sources == 0:
        status = "FAILED" if failed_sources > 0 else "NO_CANDIDATES"
    elif failed_sources > 0:
        status = "PARTIAL_FAILURE"
    else:
        status = "SUCCESS" if alerts else "NO_CANDIDATES"
    return alerts, status, skipped, errors
# ----------------------------------------------------------------------------
# Module B: Big orders / contract announcements
# ----------------------------------------------------------------------------
ORDER_KEYWORD_RE = re.compile(
    r"\b(?:orders?|contracts?|work[\s-]?orders?|purchase[\s-]?orders?|awards?|"
    r"awarded|letter\s+of\s+award|loa)\b",
    re.I,
)
EXCLUDED_ORDER_STATUS_RE = re.compile(
    r"\b(?:cancel(?:led|lation)?|lapsed|withdrawn|revoked|expired|completed|"
    r"concluded|terminated|letter\s+of\s+intent|loi|memorandum\s+of\s+understanding|"
    r"mou|proposal|proposed|extension|amendment|revised|revision|historical)\b",
    re.I,
)
ORDER_AMOUNT_RE = re.compile(
    r"(?<!\w)(?P<currency>₹|Rs\.?|INR)?\s*"
    r"(?P<number>\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<unit>crores?|cr\.?|lakhs?|lacs?|lac|million|mn\.?|billion|bn\.?)(?!\w)",
    re.I,
)
FORBIDDEN_BEFORE_RE = re.compile(
    r"(?:revenue|turnover|sales|profit|ebitda|margin|dividend|eps|earnings|"
    r"income|share\s+price|market\s+price|cmp|face\s+value|book\s+value|"
    r"net\s+worth|debt|borrowing|market\s+cap)\s*(?:of|at|was|is|:)?\s*$",
    re.I,
)
FORBIDDEN_AFTER_RE = re.compile(
    r"^\s*(?:per\s+share|share|%|percent|mw|megawatt|units?|tonnes?|tons?|"
    r"km|kms|years?|months?|days?)",
    re.I,
)
def extract_order_value(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Extract a single unambiguous INR order value from accessible announcement
    text. PDF-only values are not parsed and must not be claimed as verified.
    """
    if not text:
        return None, "MISSING_TEXT"
    if EXCLUDED_ORDER_STATUS_RE.search(text):
        return None, "EXCLUDED_ANNOUNCEMENT_STATUS"
    keyword_positions = [m.start() for m in ORDER_KEYWORD_RE.finditer(text)]
    if not keyword_positions:
        return None, "NO_ORDER_KEYWORD"
    unique_candidates: Dict[float, Dict[str, Any]] = {}
    for match in ORDER_AMOUNT_RE.finditer(text):
        distance = min(abs(match.start() - position) for position in keyword_positions)
        if distance > 150:
            continue
        before = text[max(0, match.start() - 60): match.start()]
        after = text[match.end(): min(len(text), match.end() + 60)]
        if FORBIDDEN_BEFORE_RE.search(before):
            continue
        if FORBIDDEN_AFTER_RE.search(after):
            continue
        window = text[max(0, match.start() - 80): min(len(text), match.end() + 80)]
        currency = detect_currency(window)
        if currency != "INR":
            continue
        # Explicit INR evidence near the amount is mandatory.
        marker_text = text[max(0, match.start() - 25): match.end()]
        if not re.search(r"₹|\bRs\.?\b|\bINR\b", marker_text, re.I):
            continue
        unit_key = normalize_unit_key(match.group("unit"))
        if unit_key not in {"crore", "lakh", "million", "billion"}:
            continue
        number = parse_float(match.group("number"))
        if number is None or number <= 0:
            continue
        value_cr = normalize_amount_to_cr(number, unit_key)
        if value_cr is None or value_cr <= 0:
            continue
        rounded = round(value_cr, 4)
        unique_candidates.setdefault(
            rounded,
            {"value_cr": rounded, "currency": "INR", "raw": match.group(0)},
        )
    if len(unique_candidates) == 1:
        return list(unique_candidates.values())[0], "VERIFIED"
    if not unique_candidates:
        return None, "NO_UNAMBIGUOUS_VALUE"
    return None, "AMBIGUOUS_MULTIPLE_VALUES"
def build_nse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    try:
        _http_get(
            "https://www.nseindia.com/corporate-announcements",
            session=session,
            expected_types=("text/html",),
            raise_for_status=False,
        )
    except Exception:
        pass
    return session
def build_bse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.bseindia.com/",
            "Origin": "https://www.bseindia.com",
        }
    )
    try:
        _http_get(
            "https://www.bseindia.com/",
            session=session,
            expected_types=("text/html",),
            raise_for_status=False,
        )
    except Exception:
        pass
    return session
def fetch_nse_announcements(session: requests.Session) -> List[Any]:
    url = "https://www.nseindia.com/api/corporate-announcements?index=equities"
    response = _http_get(url, session=session, expected_types=("application/json",))
    data = response.json()
    if isinstance(data, dict):
        data = data.get("data") or []
    if not isinstance(data, list):
        raise ValueError("Unexpected NSE announcements payload.")
    return data
def fetch_bse_announcements_for_scripcode(
    session: requests.Session, scripcode: str
) -> List[Any]:
    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
    params = {
        "strCat": "-1",
        "strPrevDate": "",
        "strScrip": scripcode,
        "strSearch": "A",
        "strToDate": "",
        "strType": "C",
    }
    response = _http_get(
        url,
        session=session,
        params=params,
        expected_types=("application/json",),
    )
    data = response.json()
    if isinstance(data, dict):
        data = data.get("Table") or data.get("data") or []
    if not isinstance(data, list):
        raise ValueError("Unexpected BSE announcements payload.")
    return data
def get_first(record: Dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return default
def normalize_nse_announcements(raw_items: List[Any]) -> List[Dict[str, Any]]:
    normalized = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        text = str(get_first(raw, ["desc", "description", "subject", "title"], "") or "").strip()
        if not text:
            continue
        symbol = get_first(raw, ["symbol", "Symbol", "sym"], None)
        attachment = get_first(raw, ["attchmntFile", "attachment", "attchmntText"], None)
        if attachment and str(attachment).startswith("http"):
            url = str(attachment)
        elif attachment:
            url = "https://www.nseindia.com/" + str(attachment).lstrip("/")
        else:
            url = "https://www.nseindia.com/corporate-announcements"
        normalized.append(
            {
                "exchange": "NSE",
                "raw_symbol": str(symbol).upper() if symbol else None,
                "matched_symbol": None,
                "text": text,
                "date": get_first(raw, ["an_dt", "an_date", "date", "created_date"], None),
                "url": url,
                "attachment_present": bool(attachment),
            }
        )
    return normalized
def normalize_bse_announcements(
    raw_items: List[Any], symbol: str, info: Dict[str, str]
) -> List[Dict[str, Any]]:
    normalized = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        text = str(
            get_first(raw, ["NEWSSUB", "NewsSubject", "Subject", "Description", "Headline"], "") or ""
        ).strip()
        if not text:
            continue
        attachment = get_first(raw, ["ATTACHMENTNAME", "Attachment", "URL", "Url"], None)
        if attachment and str(attachment).startswith("http"):
            url = str(attachment)
        elif attachment:
            url = "https://www.bseindia.com/" + str(attachment).lstrip("/")
        else:
            url = "https://www.bseindia.com/corporates/anndet.aspx"
        normalized.append(
            {
                "exchange": "BSE",
                "raw_symbol": info["bse"],
                "matched_symbol": symbol,
                "text": text,
                "date": get_first(raw, ["NEWS_DT", "CreatedDateTime", "UpdateTime", "Date"], None),
                "url": url,
                "attachment_present": bool(attachment),
            }
        )
    return normalized
def fetch_exchange_announcements(
    tickers: Dict[str, Dict[str, str]],
) -> Tuple[List[Dict[str, Any]], str, List[str]]:
    items: List[Dict[str, Any]] = []
    errors: List[str] = []
    successful_sources = 0
    failed_sources = 0
    try:
        nse_session = build_nse_session()
        raw_nse = fetch_nse_announcements(nse_session)
        items.extend(normalize_nse_announcements(raw_nse))
        successful_sources += 1
    except Exception as exc:  # Isolate source failure.
        failed_sources += 1
        errors.append(sanitize_message(f"NSE announcement source failed: {exc}"))
    try:
        bse_session = build_bse_session()
        bse_success = False
        for symbol, info in tickers.items():
            try:
                raw_bse = fetch_bse_announcements_for_scripcode(bse_session, info["bse"])
                items.extend(normalize_bse_announcements(raw_bse, symbol, info))
                bse_success = True
            except Exception as exc:  # Isolate one BSE ticker.
                failed_sources += 1
                errors.append(sanitize_message(f"BSE announcements failed for {symbol}: {exc}"))
        if bse_success:
            successful_sources += 1
    except Exception as exc:  # Isolate BSE session failure.
        failed_sources += 1
        errors.append(sanitize_message(f"BSE session failed: {exc}"))
    if successful_sources == 0:
        status = "FAILED"
    elif failed_sources > 0:
        status = "PARTIAL_FAILURE"
    else:
        status = "SUCCESS"
    return items, status, errors
def parse_annual_revenue_from_html(html_text: str) -> Optional[float]:
    try:
        soup = BeautifulSoup(html_text, "html.parser")
    except Exception:
        return None
    for section_id in ("profit-loss",):
        section = soup.find("section", id=section_id)
        if section is None:
            continue
        section_text = section.get_text(" ", strip=True)
        unit, reason = detect_unit_from_text(section_text)
        if not unit:
            continue
        for table in section.find_all("table"):
            rows = table.find_all("tr")
            header_index, columns = find_header_columns(rows, require_annual=True)
            if header_index is None:
                continue
            latest = max(columns.values(), key=lambda item: item["end_date"])
            latest_index = next(
                idx for idx, item in columns.items() if item["end_date"] == latest["end_date"]
            )
            for row in rows[header_index + 1:]:
                cells = row.find_all(["th", "td"])
                if not cells:
                    continue
                label = cells[0].get_text(" ", strip=True).lower()
                if re.search(
                    r"^(sales|net sales|total income|revenue from operations|operating revenue)$",
                    label,
                ):
                    if len(cells) <= latest_index:
                        continue
                    value = parse_table_value(cells[latest_index].get_text(" ", strip=True), unit)
                    if value is not None and value > 0:
                        return value
    return None
def fetch_annual_revenue_cr(info: Dict[str, str]) -> Optional[float]:
    slug = info["screener"]
    if slug in REVENUE_CACHE:
        return REVENUE_CACHE[slug]
    url = f"https://www.screener.in/company/{info['screener']}/consolidated/"
    try:
        html_text = fetch_screener_page_cached(url, info)
        revenue = parse_annual_revenue_from_html(html_text)
    except Exception:
        revenue = None
    REVENUE_CACHE[slug] = revenue
    return revenue
def build_order_alert(
    symbol: str,
    info: Dict[str, str],
    item: Dict[str, Any],
    announcement_date,
    value: Dict[str, Any],
    materiality: str,
    revenue_ratio_text: str,
) -> str:
    lines = [
        "BIG ORDER / CONTRACT ANNOUNCEMENT (EDUCATIONAL SCREENING)",
        f"{info['company_name']} ({symbol})",
        f"Announcement date: {announcement_date.isoformat()}",
        f"Order value: {value['value_cr']:.2f} INR crore",
        "Value source: accessible announcement text (not PDF-parsed)",
        "Counterparty: Not verified",
    ]
    if materiality:
        lines.append(f"Materiality label: {materiality} (screening label only)")
    if revenue_ratio_text:
        lines.append(revenue_ratio_text)
    lines.extend(
        [
            f"Description: {first_line(item.get('text', ''))}",
            f"Source: {item.get('url')}",
            "Verification status: value extracted from text; treat as screening, not proof.",
            f"Scan time: {ist_now_str()}",
        ]
    )
    return "\n".join(lines)
def process_big_orders(
    tickers: Dict[str, Dict[str, str]],
    announcement_items: Optional[List[Dict[str, Any]]],
    known_keys: Set[str],
    new_keys: Set[str],
    revenue_fetcher: Optional[Callable[[Dict[str, str]], Optional[float]]] = None,
) -> Tuple[List[Dict[str, str]], str, int, List[str]]:
    alerts: List[Dict[str, str]] = []
    skipped = 0
    errors: List[str] = []
    if announcement_items is None:
        return alerts, "FAILED", 0, ["Announcement source unavailable."]
    valid_nse = {info["nse"].upper(): symbol for symbol, info in tickers.items()}
    valid_bse = {str(info["bse"]): symbol for symbol, info in tickers.items()}
    for item in announcement_items:
        try:
            matched = item.get("matched_symbol")
            if not matched:
                raw_symbol = str(item.get("raw_symbol") or "").upper()
                matched = valid_nse.get(raw_symbol) or valid_bse.get(raw_symbol)
            if not matched or matched not in tickers:
                continue
            text = item.get("text", "") or ""
            if not ORDER_KEYWORD_RE.search(text):
                continue
            announcement_date = parse_announcement_date(item.get("date"))
            if not announcement_date:
                skipped += 1
                continue
            age_days = (ist_today() - announcement_date).days
            if age_days < 0:
                age_days = 0
            if age_days > MAX_ORDER_AGE_DAYS:
                skipped += 1
                continue
            if EXCLUDED_ORDER_STATUS_RE.search(text):
                skipped += 1
                continue
            value, reason = extract_order_value(text)
            if value is None:
                # If an attachment exists, record the honest limitation instead
                # of inventing a value.
                if item.get("attachment_present"):
                    skipped += 1
                    continue
                skipped += 1
                continue
            key = f"ORDERS:{matched}:{announcement_date.isoformat()}:{value['value_cr']:.2f}"
            if key in known_keys:
                continue
            materiality = ""
            revenue_ratio_text = ""
            if revenue_fetcher is not None:
                try:
                    revenue_cr = revenue_fetcher(tickers[matched])
                except Exception:
                    revenue_cr = None
                if revenue_cr and revenue_cr > 0:
                    ratio_pct = (value["value_cr"] / revenue_cr) * 100.0
                    revenue_ratio_text = f"Order / verified annual revenue: {ratio_pct:.1f}%"
                    if ratio_pct > 50.0:
                        materiality = "HIGH MATERIALITY"
                    elif ratio_pct > 20.0:
                        materiality = "MATERIAL"
            known_keys.add(key)
            new_keys.add(key)
            alerts.append(
                {
                    "module": "ORDERS",
                    "key": key,
                    "message": build_order_alert(
                        matched,
                        tickers[matched],
                        item,
                        announcement_date,
                        value,
                        materiality,
                        revenue_ratio_text,
                    ),
                }
            )
        except Exception as exc:  # Isolate one record.
            skipped += 1
            errors.append(sanitize_message(f"Order record failed: {exc}"))
    if errors:
        status = "PARTIAL_FAILURE"
    elif alerts:
        status = "SUCCESS"
    else:
        status = "NO_CANDIDATES"
    return alerts, status, skipped, errors
# ----------------------------------------------------------------------------
# Module C: Share buybacks / theoretical premium
# ----------------------------------------------------------------------------
BUYBACK_KEYWORD_RE = re.compile(
    r"\b(?:buyback|buy[\s-]?back|repurchase|share\s+repurchase|tender\s+offer)\b",
    re.I,
)
BUYBACK_PRICE_RE = re.compile(
    r"(?<!\w)(?P<currency>₹|Rs\.?|INR)\s*"
    r"(?P<number>\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?:per\s+(?:equity\s+)?share|/\s*share|per\s+share)(?!\w)",
    re.I,
)
def classify_buyback_status(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"withdraw|cancel", t):
        return "WITHDRAWN"
    if re.search(r"expire", t):
        return "EXPIRED"
    if re.search(r"complet|concluded", t):
        return "COMPLETED"
    if re.search(r"clos(?:ed|ing)", t):
        return "CLOSED"
    if re.search(r"\bopen\b|tender\s+offer\s+begins|offer\s+opens|opens\s+on", t):
        return "OPEN"
    if re.search(r"approved", t):
        return "APPROVED"
    if re.search(r"proposal|proposed|intimation", t):
        return "PROPOSED"
    return "UNKNOWN"
def extract_buyback_price(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Buyback-specific price extraction. Does not reuse order-exclusion logic,
    because legitimate buybacks often contain "proposal" or "approved".
    """
    if not text or not BUYBACK_KEYWORD_RE.search(text):
        return None, "NO_BUYBACK_KEYWORD"
    unique_candidates: Dict[float, Dict[str, Any]] = {}
    for match in BUYBACK_PRICE_RE.finditer(text):
        window = text[max(0, match.start() - 120): min(len(text), match.end() + 120)]
        if not BUYBACK_KEYWORD_RE.search(window):
            continue
        # Total buyback size (crore/lakh/million) is not a per-share price.
        if re.search(r"\b(?:crore|lakh|million|billion)\b", window, re.I):
            continue
        currency = detect_currency(window)
        if currency != "INR":
            continue
        price = parse_float(match.group("number"))
        if price is None or price <= 0:
            continue
        rounded = round(price, 4)
        unique_candidates.setdefault(rounded, {"price": rounded, "raw": match.group(0)})
    if len(unique_candidates) == 1:
        return list(unique_candidates.values())[0], "VERIFIED"
    if not unique_candidates:
        return None, "NO_PRICE"
    return None, "AMBIGUOUS_PRICE"
def calculate_buyback_premium(
    offer_price: Optional[float], cmp_price: Optional[float]
) -> Dict[str, Any]:
    if offer_price is None or cmp_price is None:
        return {"eligible": False, "reason": "MISSING_PRICE", "premium_pct": None}
    if offer_price <= 0 or cmp_price <= 0:
        return {"eligible": False, "reason": "NON_POSITIVE_PRICE", "premium_pct": None}
    premium_pct = ((offer_price - cmp_price) / cmp_price) * 100.0
    eligible = premium_pct > 8.0 and not math.isclose(premium_pct, 8.0, abs_tol=1e-9)
    return {
        "eligible": eligible,
        "reason": "OK" if eligible else "THRESHOLD_NOT_MET",
        "premium_pct": premium_pct,
    }
def fetch_yahoo_cmp(yahoo_symbol: str) -> Tuple[float, datetime]:
    """
    Third-party, potentially delayed CMP. Stale prices are rejected.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}?interval=1d&range=1d"
    response = _http_get(
        url,
        headers={"User-Agent": USER_AGENT},
        expected_types=("application/json",),
    )
    payload = response.json()
    result = payload["chart"]["result"][0]
    meta = result["meta"]
    currency = str(meta.get("currency", "")).upper()
    if currency != "INR":
        raise ValueError(f"Non-INR Yahoo currency: {currency or 'missing'}")
    price = float(meta["regularMarketPrice"])
    if price <= 0:
        raise ValueError("Non-positive CMP.")
    timestamp = meta.get("regularMarketTime")
    if not timestamp:
        raise ValueError("Missing CMP timestamp.")
    cmp_time = datetime.fromtimestamp(timestamp, IST)
    age_days = (ist_now() - cmp_time).days
    if age_days > MAX_CMP_AGE_DAYS:
        raise ValueError(f"Stale CMP: {age_days} days old.")
    return price, cmp_time
def build_buyback_alert(
    symbol: str,
    info: Dict[str, str],
    item: Dict[str, Any],
    announcement_date,
    status: str,
    price_info: Dict[str, Any],
    cmp_price: float,
    cmp_time: datetime,
    premium: Dict[str, Any],
) -> str:
    return "\n".join(
        [
            "[THEORETICAL PREMIUM — NOT GUARANTEED]",
            f"{info['company_name']} ({symbol})",
            f"Offer status: {status}",
            f"Announcement date: {announcement_date.isoformat()}",
            f"Buyback price: {price_info['price']:.2f} INR per share",
            f"CMP: {cmp_price:.2f} INR as of {cmp_time.strftime('%Y-%m-%d %H:%M:%S %z')}",
            "CMP source: Yahoo Finance (third-party, potentially delayed)",
            f"Theoretical premium: {premium['premium_pct']:.2f}%",
            f"Source: {item.get('url')}",
            "This displayed premium is not a guaranteed return. Eligibility, acceptance ratio, "
            "taxes, costs, market-price changes, and offer conditions can materially affect outcomes.",
            f"Scan time: {ist_now_str()}",
        ]
    )
def process_buybacks(
    tickers: Dict[str, Dict[str, str]],
    announcement_items: Optional[List[Dict[str, Any]]],
    known_keys: Set[str],
    new_keys: Set[str],
    cmp_fetcher: Optional[Callable[[str], Tuple[float, datetime]]] = None,
) -> Tuple[List[Dict[str, str]], str, int, List[str]]:
    alerts: List[Dict[str, str]] = []
    skipped = 0
    errors: List[str] = []
    if announcement_items is None:
        return alerts, "FAILED", 0, ["Announcement source unavailable."]
    valid_nse = {info["nse"].upper(): symbol for symbol, info in tickers.items()}
    valid_bse = {str(info["bse"]): symbol for symbol, info in tickers.items()}
    fetch_cmp = cmp_fetcher or fetch_yahoo_cmp
    cmp_cache: Dict[str, Optional[Tuple[float, datetime]]] = {}
    for item in announcement_items:
        try:
            matched = item.get("matched_symbol")
            if not matched:
                raw_symbol = str(item.get("raw_symbol") or "").upper()
                matched = valid_nse.get(raw_symbol) or valid_bse.get(raw_symbol)
            if not matched or matched not in tickers:
                continue
            text = item.get("text", "") or ""
            if not BUYBACK_KEYWORD_RE.search(text):
                continue
            status = classify_buyback_status(text)
            if status not in {"OPEN", "APPROVED"}:
                skipped += 1
                continue
            announcement_date = parse_announcement_date(item.get("date"))
            if not announcement_date:
                skipped += 1
                continue
            age_days = (ist_today() - announcement_date).days
            if age_days < 0:
                age_days = 0
            if age_days > MAX_BUYBACK_AGE_DAYS:
                skipped += 1
                continue
            price_info, reason = extract_buyback_price(text)
            if price_info is None:
                skipped += 1
                continue
            if matched in cmp_cache:
                cached = cmp_cache[matched]
                if cached is None:
                    skipped += 1
                    continue
                cmp_price, cmp_time = cached
            else:
                try:
                    cmp_price, cmp_time = fetch_cmp(tickers[matched]["yahoo"])
                    cmp_cache[matched] = (cmp_price, cmp_time)
                except Exception as exc:  # Isolate CMP failure.
                    cmp_cache[matched] = None
                    skipped += 1
                    errors.append(sanitize_message(f"CMP fetch failed for {matched}: {exc}"))
                    continue
            # Enforce freshness even for injected fetchers.
            if (ist_now() - cmp_time).days > MAX_CMP_AGE_DAYS:
                skipped += 1
                continue
            premium = calculate_buyback_premium(price_info["price"], cmp_price)
            if not premium.get("eligible"):
                skipped += 1
                continue
            key = (
                f"BUYBACK:{matched}:{announcement_date.isoformat()}:"
                f"{price_info['price']:.2f}:{cmp_price:.2f}"
            )
            if key in known_keys:
                continue
            known_keys.add(key)
            new_keys.add(key)
            alerts.append(
                {
                    "module": "BUYBACK",
                    "key": key,
                    "message": build_buyback_alert(
                        matched,
                        tickers[matched],
                        item,
                        announcement_date,
                        status,
                        price_info,
                        cmp_price,
                        cmp_time,
                        premium,
                    ),
                }
            )
        except Exception as exc:  # Isolate one record.
            skipped += 1
            errors.append(sanitize_message(f"Buyback record failed: {exc}"))
    if errors:
        status = "PARTIAL_FAILURE"
    elif alerts:
        status = "SUCCESS"
    else:
        status = "NO_CANDIDATES"
    return alerts, status, skipped, errors
# ----------------------------------------------------------------------------
# Heartbeat and overall status
# ----------------------------------------------------------------------------
def compute_overall_status(
    module_statuses: Dict[str, str],
    persistence_status: str,
    alert_delivery_ok: bool,
    heartbeat_delivered: bool,
    alerts_count: int,
) -> str:
    if not heartbeat_delivered or not alert_delivery_ok:
        return "DELIVERY_FAILURE"
    if persistence_status == "PERSISTENCE_FAILURE":
        return "PERSISTENCE_FAILURE"
    if any(status == "FAILED" for status in module_statuses.values()):
        return "FAILED"
    if any(status == "PARTIAL_FAILURE" for status in module_statuses.values()):
        return "PARTIAL_FAILURE"
    if alerts_count > 0:
        return "SUCCESS"
    return "NO_CANDIDATES"
def build_heartbeat_message(
    statuses: Dict[str, str],
    overall: str,
    alerts_found: int,
    alert_metrics: Dict[str, int],
    skipped_total: int,
    persistence_status: str,
    error_summaries: List[str],
) -> str:
    errors_summary = (
        "; ".join([sanitize_message(item) for item in error_summaries if item][:5]) or "None"
    )
    return "\n".join(
        [
            "PROJECT ARTHA V6.2 HEARTBEAT",
            f"Scan time (Asia/Kolkata): {ist_now_str()}",
            f"Overall (pre-heartbeat-delivery): {overall}",
            "Supported universe: 5 hard-coded stocks, not market-wide.",
            f"PEAD module: {statuses.get('PEAD')}",
            f"Big Orders module: {statuses.get('ORDERS')}",
            f"Buybacks module: {statuses.get('BUYBACK')}",
            f"Verified alerts found: {alerts_found}",
            f"Alert messages attempted/delivered: {alert_metrics['attempted']}/{alert_metrics['delivered']}",
            f"Skipped/unverified records: {skipped_total}",
            f"Persistence: {persistence_status}",
            f"Errors: {errors_summary}",
            "Educational/research scanner only. Signals are not guaranteed investment opportunities.",
        ]
    )
# ----------------------------------------------------------------------------
# Main run
# ----------------------------------------------------------------------------
def run_scan() -> int:
    logging.info("Project Artha V6.2 scan starting. Universe: 5 hard-coded stocks.")
    error_summaries: List[str] = []
    state_read = read_state()
    state_read_ok = state_read.ok
    if state_read_ok:
        known_keys: Set[str] = set(state_read.keys)
        persistence_status = "SUCCESS"
    else:
        known_keys = set()
        persistence_status = "PERSISTENCE_FAILURE"
        error_summaries.append(
            sanitize_message(
                f"State read failed ({state_read.error_kind}): {state_read.error}"
            )
        )
    new_keys: Set[str] = set()
    pead_alerts, pead_status, pead_skipped, pead_errors = scan_pead(
        TICKER_MAP, known_keys, new_keys
    )
    error_summaries.extend(pead_errors)
    announcement_items, announcement_status, announcement_errors = fetch_exchange_announcements(
        TICKER_MAP
    )
    error_summaries.extend(announcement_errors)
    if announcement_status == "FAILED":
        orders_alerts: List[Dict[str, str]] = []
        orders_status = "FAILED"
        orders_skipped = 0
        order_errors = announcement_errors
        buyback_alerts: List[Dict[str, str]] = []
        buyback_status = "FAILED"
        buyback_skipped = 0
        buyback_errors = announcement_errors
    else:
        orders_alerts, orders_status, orders_skipped, order_errors = process_big_orders(
            TICKER_MAP,
            announcement_items,
            known_keys,
            new_keys,
            revenue_fetcher=fetch_annual_revenue_cr,
        )
        buyback_alerts, buyback_status, buyback_skipped, buyback_errors = process_buybacks(
            TICKER_MAP,
            announcement_items,
            known_keys,
            new_keys,
            cmp_fetcher=fetch_yahoo_cmp,
        )
        if announcement_status == "PARTIAL_FAILURE":
            if orders_status in ("SUCCESS", "NO_CANDIDATES"):
                orders_status = "PARTIAL_FAILURE"
            if buyback_status in ("SUCCESS", "NO_CANDIDATES"):
                buyback_status = "PARTIAL_FAILURE"
    error_summaries.extend(order_errors)
    error_summaries.extend(buyback_errors)
    all_alerts = pead_alerts + orders_alerts + buyback_alerts
    alert_metrics = {"attempted": 0, "delivered": 0}
    alert_delivery_ok = True
    delivered_keys: Set[str] = set()
    for alert in all_alerts:
        ok = send_telegram_alert(alert["message"], metrics=alert_metrics)
        if ok:
            delivered_keys.add(alert["key"])
        else:
            alert_delivery_ok = False
    persistence_ok = state_read_ok
    if state_read_ok:
        if delivered_keys:
            delivered_with_ts = {key: ist_now().isoformat() for key in delivered_keys}
            saved, save_error = save_state(delivered_with_ts)
            if saved:
                persistence_status = "SUCCESS"
            else:
                persistence_status = "PERSISTENCE_FAILURE"
                persistence_ok = False
                error_summaries.append(
                    sanitize_message(
                        f"State write failed: {save_error}. Delivered alerts may repeat next run."
                    )
                )
        else:
            persistence_status = "SUCCESS"
    else:
        if delivered_keys:
            error_summaries.append(
                "State could not be read, so delivered alerts were not persisted. "
                "Duplicate alerts are likely on the next run."
            )
        persistence_status = "PERSISTENCE_FAILURE"
        persistence_ok = False
    statuses = {
        "PEAD": pead_status,
        "ORDERS": orders_status,
        "BUYBACK": buyback_status,
    }
    skipped_total = pead_skipped + orders_skipped + buyback_skipped
    preliminary_overall = compute_overall_status(
        statuses,
        persistence_status,
        alert_delivery_ok,
        True,
        len(all_alerts),
    )
    heartbeat_message = build_heartbeat_message(
        statuses,
        preliminary_overall,
        len(all_alerts),
        alert_metrics,
        skipped_total,
        persistence_status,
        error_summaries,
    )
    heartbeat_metrics = {"attempted": 0, "delivered": 0}
    heartbeat_delivered = send_telegram_alert(heartbeat_message, metrics=heartbeat_metrics)
    # A heartbeat cannot reliably report the outcome of its own delivery before
    # that delivery has occurred. The final status is therefore computed after
    # the heartbeat attempt and may differ from the value printed in the message.
    overall_status = compute_overall_status(
        statuses,
        persistence_status,
        alert_delivery_ok,
        heartbeat_delivered,
        len(all_alerts),
    )
    logging.info("Overall status: %s", overall_status)
    logging.info(
        "Module statuses: PEAD=%s ORDERS=%s BUYBACK=%s persistence=%s",
        pead_status,
        orders_status,
        buyback_status,
        persistence_status,
    )
    if overall_status in ("SUCCESS", "NO_CANDIDATES"):
        return 0
    return 1
# ----------------------------------------------------------------------------
# Self-tests
# ----------------------------------------------------------------------------
class TestPeadMath(unittest.TestCase):
    def test_growth_below_100(self):
        self.assertFalse(evaluate_profit_growth(199.99, 100.0)["eligible"])
    def test_growth_exactly_100(self):
        self.assertFalse(evaluate_profit_growth(200.0, 100.0)["eligible"])
    def test_growth_above_100(self):
        self.assertTrue(evaluate_profit_growth(200.01, 100.0)["eligible"])
    def test_previous_zero(self):
        result = evaluate_profit_growth(10.0, 0.0)
        self.assertFalse(result["eligible"])
        self.assertEqual(result.get("event"), "ZERO_TO_PROFIT")
    def test_previous_negative(self):
        result = evaluate_profit_growth(10.0, -5.0)
        self.assertFalse(result["eligible"])
        self.assertEqual(result.get("event"), "LOSS_TO_PROFIT")
class TestQuarterMatching(unittest.TestCase):
    def test_prior_year_same_quarter(self):
        parsed = [
            parse_quarter_label("Dec 2024"),
            parse_quarter_label("Sep 2024"),
            parse_quarter_label("Dec 2023"),
        ]
        latest, previous = find_latest_and_previous_parsed([p for p in parsed if p])
        self.assertIsNotNone(previous)
        self.assertEqual(latest["label"], "Dec 2024")
        self.assertEqual(previous["label"], "Dec 2023")
    def test_mismatched_quarter(self):
        parsed = [parse_quarter_label("Dec 2024"), parse_quarter_label("Sep 2023")]
        latest, previous = find_latest_and_previous_parsed([p for p in parsed if p])
        self.assertIsNone(previous)
    def test_fiscal_quarter_mapping(self):
        self.assertEqual(parse_quarter_label("Jun 2024")["fiscal_quarter"], "Q1")
        self.assertEqual(parse_quarter_label("Mar 2025")["fiscal_quarter"], "Q4")
class TestPeadHtmlParsing(unittest.TestCase):
    SAMPLE_OK = """
    <html><body>
    <section id="quarters">
      <h2>Quarterly Results</h2>
      <p class="sub">Consolidated Figures in Rs. Crores</p>
      <table class="data-table">
        <thead><tr><th></th><th>Sep 2024</th><th>Dec 2024</th><th>Dec 2023</th></tr></thead>
        <tbody>
          <tr><td>Sales</td><td>100</td><td>200</td><td>150</td></tr>
          <tr><td>Net Profit</td><td>50</td><td>220</td><td>100</td></tr>
        </tbody>
      </table>
    </section>
    </body></html>
    """
    SAMPLE_NO_UNIT = """
    <html><body>
    <section id="quarters">
      <h2>Quarterly Results</h2>
      <table class="data-table">
        <thead><tr><th></th><th>Sep 2024</th><th>Dec 2024</th><th>Dec 2023</th></tr></thead>
        <tbody><tr><td>Net Profit</td><td>50</td><td>220</td><td>100</td></tr></tbody>
      </table>
    </section>
    </body></html>
    """
    SAMPLE_AMBIGUOUS_UNIT = """
    <html><body>
    <section id="quarters">
      <h2>Quarterly Results</h2>
      <p>Figures in Rs. Crores and Lakhs</p>
      <table class="data-table">
        <thead><tr><th></th><th>Sep 2024</th><th>Dec 2024</th><th>Dec 2023</th></tr></thead>
        <tbody><tr><td>Net Profit</td><td>50</td><td>220</td><td>100</td></tr></tbody>
      </table>
    </section>
    </body></html>
    """
    SAMPLE_MISMATCH = """
    <html><body>
    <section id="quarters">
      <h2>Quarterly Results</h2>
      <p>Consolidated Figures in Rs. Crores</p>
      <table class="data-table">
        <thead><tr><th></th><th>Sep 2024</th><th>Dec 2024</th><th>Sep 2023</th></tr></thead>
        <tbody><tr><td>Net Profit</td><td>50</td><td>220</td><td>100</td></tr></tbody>
      </table>
    </section>
    </body></html>
    """
    def test_verified_and_eligible(self):
        result = parse_pead_from_html(self.SAMPLE_OK, TICKER_MAP["TCS"])
        self.assertTrue(result["verified"])
        self.assertTrue(result["eligible"])
        self.assertAlmostEqual(result["growth_pct"], 120.0)
        self.assertEqual(result["basis"], "consolidated")
    def test_missing_unit_unverified(self):
        result = parse_pead_from_html(self.SAMPLE_NO_UNIT, TICKER_MAP["TCS"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "UNIT_NOT_FOUND")
    def test_ambiguous_unit_unverified(self):
        result = parse_pead_from_html(self.SAMPLE_AMBIGUOUS_UNIT, TICKER_MAP["TCS"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "AMBIGUOUS_UNIT")
    def test_mismatched_quarter_unverified(self):
        result = parse_pead_from_html(self.SAMPLE_MISMATCH, TICKER_MAP["TCS"])
        self.assertFalse(result["verified"])
    def test_parse_table_value_missing(self):
        self.assertIsNone(parse_table_value("", "crore"))
        self.assertIsNone(parse_table_value("--", "crore"))
    def test_unit_normalization(self):
        self.assertAlmostEqual(normalize_amount_to_cr(100.0, "lakh"), 1.0)
        self.assertAlmostEqual(normalize_amount_to_cr(1.0, "crore"), 1.0)
        self.assertAlmostEqual(normalize_amount_to_cr(10.0, "million"), 1.0)
        self.assertAlmostEqual(normalize_amount_to_cr(1.0, "billion"), 100.0)
    def test_negative_and_comma_parsing(self):
        self.assertAlmostEqual(parse_float("(1,234.5)"), -1234.5)
        self.assertAlmostEqual(parse_float("-1,234.5"), -1234.5)
        self.assertIsNone(parse_float("NA"))
class TestOrderValueExtraction(unittest.TestCase):
    def test_crore_value(self):
        value, reason = extract_order_value(
            "Company received a new order worth Rs. 1,234.5 crore from client."
        )
        self.assertEqual(reason, "VERIFIED")
        self.assertAlmostEqual(value["value_cr"], 1234.5)
    def test_lakh_value(self):
        value, reason = extract_order_value("Work order of Rs 50 lakh received.")
        self.assertEqual(reason, "VERIFIED")
        self.assertAlmostEqual(value["value_cr"], 0.5)
    def test_million_inr_value(self):
        value, reason = extract_order_value("Contract valued at INR 10 million.")
        self.assertEqual(reason, "VERIFIED")
        self.assertAlmostEqual(value["value_cr"], 1.0)
    def test_billion_inr_value(self):
        value, reason = extract_order_value("Purchase order worth INR 1 billion awarded.")
        self.assertEqual(reason, "VERIFIED")
        self.assertAlmostEqual(value["value_cr"], 100.0)
    def test_decimal_value(self):
        value, reason = extract_order_value("Purchase order ₹99.9 crore received.")
        self.assertEqual(reason, "VERIFIED")
        self.assertAlmostEqual(value["value_cr"], 99.9)
    def test_ambiguous_multiple_values(self):
        value, reason = extract_order_value("Order worth Rs 10 crore and Rs 20 crore.")
        self.assertIsNone(value)
        self.assertEqual(reason, "AMBIGUOUS_MULTIPLE_VALUES")
    def test_reject_unrelated_revenue(self):
        value, reason = extract_order_value(
            "Order won. Revenue of Rs 10 crore was reported earlier."
        )
        self.assertIsNone(value)
        self.assertEqual(reason, "NO_UNAMBIGUOUS_VALUE")
    def test_reject_missing_currency_marker(self):
        value, reason = extract_order_value("Order worth 500 crore received.")
        self.assertIsNone(value)
    def test_reject_foreign_currency(self):
        value, reason = extract_order_value("Order worth USD 10 million received.")
        self.assertIsNone(value)
    def test_reject_cancelled_order(self):
        value, reason = extract_order_value("Order worth Rs 500 crore cancelled.")
        self.assertIsNone(value)
        self.assertEqual(reason, "EXCLUDED_ANNOUNCEMENT_STATUS")
    def test_reject_letter_of_intent(self):
        value, reason = extract_order_value(
            "Letter of intent for order worth Rs 500 crore received."
        )
        self.assertIsNone(value)
        self.assertEqual(reason, "EXCLUDED_ANNOUNCEMENT_STATUS")
    def test_reject_share_price(self):
        value, reason = extract_order_value(
            "Order won. Share price is Rs 500 per share."
        )
        self.assertIsNone(value)
    def test_unverified_ticker_mapping(self):
        self.assertIsNone(resolve_ticker("Unknown Company"))
        self.assertEqual(resolve_ticker("TCS"), "TCS")
        self.assertEqual(resolve_ticker("532540"), "TCS")
class TestBuybackPremium(unittest.TestCase):
    def test_exact_8_percent_not_eligible(self):
        result = calculate_buyback_premium(108.0, 100.0)
        self.assertFalse(result["eligible"])
    def test_above_8_percent_eligible(self):
        result = calculate_buyback_premium(108.1, 100.0)
        self.assertTrue(result["eligible"])
    def test_invalid_prices(self):
        self.assertFalse(calculate_buyback_premium(None, 100.0)["eligible"])
        self.assertFalse(calculate_buyback_premium(100.0, None)["eligible"])
        self.assertFalse(calculate_buyback_premium(0.0, 100.0)["eligible"])
        self.assertFalse(calculate_buyback_premium(100.0, -1.0)["eligible"])
    def test_status_classification(self):
        self.assertEqual(classify_buyback_status("Buyback withdrawn by company"), "WITHDRAWN")
        self.assertEqual(classify_buyback_status("Buyback offer expired"), "EXPIRED")
        self.assertEqual(classify_buyback_status("Buyback completed"), "COMPLETED")
        self.assertEqual(classify_buyback_status("Buyback offer open"), "OPEN")
        self.assertEqual(classify_buyback_status("Buyback approved by board"), "APPROVED")
        self.assertEqual(classify_buyback_status("Buyback proposal"), "PROPOSED")
    def test_price_extraction(self):
        value, reason = extract_buyback_price(
            "The buyback offer is at Rs 500 per share."
        )
        self.assertEqual(reason, "VERIFIED")
        self.assertAlmostEqual(value["price"], 500.0)
    def test_price_extraction_rejects_total_size(self):
        value, reason = extract_buyback_price(
            "Buyback of Rs 500 crore at Rs 500 per share."
        )
        # The per-share price is still unambiguous; total size is ignored.
        self.assertEqual(reason, "VERIFIED")
        self.assertAlmostEqual(value["price"], 500.0)
    def test_price_extraction_requires_buyback_keyword(self):
        value, reason = extract_buyback_price("Acquisition at Rs 500 per share.")
        self.assertIsNone(value)
        self.assertEqual(reason, "NO_BUYBACK_KEYWORD")
    def test_price_extraction_ambiguous(self):
        value, reason = extract_buyback_price(
            "Buyback at Rs 500 per share and Rs 600 per share."
        )
        self.assertIsNone(value)
        self.assertEqual(reason, "AMBIGUOUS_PRICE")
class TestTelegram(unittest.TestCase):
    def setUp(self):
        sleep_patcher = mock.patch("time.sleep")
        self.addCleanup(sleep_patcher.stop)
        self.mock_sleep = sleep_patcher.start()
    def _env(self):
        return mock.patch.dict(
            os.environ,
            {"BOT_TOKEN": "test-token", "CHAT_ID": "123"},
            clear=False,
        )
    def test_ok_true(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"ok": True}
        with self._env(), mock.patch(f"{__name__}._http_post", return_value=response):
            metrics = {"attempted": 0, "delivered": 0}
            self.assertTrue(send_telegram_alert("hello", metrics=metrics))
            self.assertEqual(metrics["delivered"], 1)
    def test_ok_false(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"ok": False, "description": "Bad Request"}
        with self._env(), mock.patch(f"{__name__}._http_post", return_value=response):
            metrics = {"attempted": 0, "delivered": 0}
            self.assertFalse(send_telegram_alert("hello", metrics=metrics))
            self.assertEqual(metrics["delivered"], 0)
    def test_invalid_json(self):
        response = mock.Mock(status_code=200)
        response.json.side_effect = ValueError("bad json")
        with self._env(), mock.patch(f"{__name__}._http_post", return_value=response):
            metrics = {"attempted": 0, "delivered": 0}
            self.assertFalse(send_telegram_alert("hello", metrics=metrics))
    def test_http_500_retries_then_fails(self):
        response = mock.Mock(status_code=500)
        response.json.return_value = {"ok": False}
        with self._env(), mock.patch(f"{__name__}._http_post", return_value=response) as mocked:
            metrics = {"attempted": 0, "delivered": 0}
            self.assertFalse(send_telegram_alert("hello", metrics=metrics))
            self.assertEqual(mocked.call_count, 3)
    def test_timeout_retries_then_fails(self):
        with self._env(), mock.patch(
            f"{__name__}._http_post", side_effect=requests.Timeout
        ) as mocked:
            metrics = {"attempted": 0, "delivered": 0}
            self.assertFalse(send_telegram_alert("hello", metrics=metrics))
            self.assertEqual(mocked.call_count, 3)
    def test_400_permanent_no_retry(self):
        response = mock.Mock(status_code=400)
        response.json.return_value = {"ok": False}
        with self._env(), mock.patch(f"{__name__}._http_post", return_value=response) as mocked:
            metrics = {"attempted": 0, "delivered": 0}
            self.assertFalse(send_telegram_alert("hello", metrics=metrics))
            self.assertEqual(mocked.call_count, 1)
    def test_429_retry_after(self):
        rate_limited = mock.Mock(status_code=429)
        rate_limited.json.return_value = {
            "ok": False,
            "parameters": {"retry_after": 1},
        }
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"ok": True}
        with self._env(), mock.patch(
            f"{__name__}._http_post", side_effect=[rate_limited, ok]
        ):
            metrics = {"attempted": 0, "delivered": 0}
            self.assertTrue(send_telegram_alert("hello", metrics=metrics))
            self.assertEqual(metrics["delivered"], 1)
    def test_split_long_message(self):
        long_message = "\n".join([f"line {i} " + "x" * 100 for i in range(100)])
        chunks = split_message(long_message)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(utf16_len(chunk), 4096)
class TestPersistence(unittest.TestCase):
    def setUp(self):
        sleep_patcher = mock.patch("time.sleep")
        self.addCleanup(sleep_patcher.stop)
        self.mock_sleep = sleep_patcher.start()
    def _env(self):
        return mock.patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "test-token",
                "GITHUB_REPOSITORY": "test/repo",
                "GITHUB_REF_NAME": "main",
            },
            clear=False,
        )
    def _get_response(self, keys: Dict[str, str], sha: str) -> mock.Mock:
        state = {"version": 2, "keys": keys}
        content = base64.b64encode(json.dumps(state).encode("utf-8")).decode("utf-8")
        response = mock.Mock(status_code=200)
        response.headers = {}
        response.json.return_value = {"content": content, "sha": sha}
        return response
    def test_missing_state_is_ok(self):
        response = mock.Mock(status_code=404)
        response.headers = {}
        with self._env(), mock.patch(f"{__name__}._http_get", return_value=response):
            result = read_state()
            self.assertTrue(result.ok)
            self.assertFalse(result.exists)
            self.assertEqual(result.keys, {})
    def test_successful_read(self):
        response = self._get_response({"a": "2025-01-01T00:00:00+05:30"}, "sha123")
        with self._env(), mock.patch(f"{__name__}._http_get", return_value=response):
            result = read_state()
            self.assertTrue(result.ok)
            self.assertIn("a", result.keys)
            self.assertEqual(result.sha, "sha123")
    def test_malformed_state(self):
        response = mock.Mock(status_code=200)
        response.headers = {}
        response.json.return_value = {"content": "not-base64!!!", "sha": "x"}
        with self._env(), mock.patch(f"{__name__}._http_get", return_value=response):
            result = read_state()
            self.assertFalse(result.ok)
            self.assertEqual(result.error_kind, "MALFORMED")
    def test_auth_failure(self):
        response = mock.Mock(status_code=401)
        response.headers = {}
        with self._env(), mock.patch(f"{__name__}._http_get", return_value=response):
            result = read_state()
            self.assertFalse(result.ok)
            self.assertEqual(result.error_kind, "AUTH")
    def test_rate_limit_403(self):
        response = mock.Mock(status_code=403)
        response.headers = {"x-ratelimit-remaining": "0"}
        with self._env(), mock.patch(f"{__name__}._http_get", return_value=response):
            result = read_state()
            self.assertFalse(result.ok)
            self.assertEqual(result.error_kind, "RATE_LIMIT")
    def test_save_refuses_when_read_fails(self):
        with self._env(), mock.patch(
            f"{__name__}._http_get", side_effect=requests.ConnectionError("boom")
        ), mock.patch(f"{__name__}._http_put") as put_mock:
            saved, _ = save_state({"b": "2025-01-01T00:00:00+05:30"})
            self.assertFalse(saved)
            put_mock.assert_not_called()
    def test_save_creates_when_missing(self):
        get_response = mock.Mock(status_code=404)
        get_response.headers = {}
        put_response = mock.Mock(status_code=201)
        put_response.json.return_value = {}
        with self._env(), mock.patch(
            f"{__name__}._http_get", return_value=get_response
        ), mock.patch(f"{__name__}._http_put", return_value=put_response):
            saved, _ = save_state({"b": "2025-01-01T00:00:00+05:30"})
            self.assertTrue(saved)
    def test_save_conflict_then_success(self):
        get_response = self._get_response({"a": "2025-01-01T00:00:00+05:30"}, "sha123")
        conflict = mock.Mock(status_code=409)
        conflict.json.return_value = {}
        ok = mock.Mock(status_code=201)
        ok.json.return_value = {}
        with self._env(), mock.patch(
            f"{__name__}._http_get", return_value=get_response
        ), mock.patch(f"{__name__}._http_put", side_effect=[conflict, ok]):
            saved, _ = save_state({"b": "2025-01-02T00:00:00+05:30"}, max_attempts=3)
            self.assertTrue(saved)
    def test_save_failure_after_retries(self):
        get_response = self._get_response({"a": "2025-01-01T00:00:00+05:30"}, "sha123")
        bad = mock.Mock(status_code=500)
        bad.json.return_value = {}
        with self._env(), mock.patch(
            f"{__name__}._http_get", return_value=get_response
        ), mock.patch(f"{__name__}._http_put", return_value=bad):
            saved, _ = save_state({"b": "2025-01-02T00:00:00+05:30"}, max_attempts=2)
            self.assertFalse(saved)
    def test_prune_uses_timestamp_not_lexicographic(self):
        old = (ist_now() - timedelta(days=STATE_MAX_AGE_DAYS + 1)).isoformat()
        new = ist_now().isoformat()
        pruned = _prune_state({"zzz_old": old, "aaa_new": new})
        self.assertIn("aaa_new", pruned)
        self.assertNotIn("zzz_old", pruned)
    def test_missing_environment_failure(self):
        with mock.patch.dict(
            os.environ, {"GITHUB_TOKEN": "", "GITHUB_REPOSITORY": ""}, clear=False
        ):
            result = read_state()
            self.assertFalse(result.ok)
            self.assertEqual(result.error_kind, "MISSING_CREDENTIALS")
class TestFaultTolerance(unittest.TestCase):
    SAMPLE_OK = TestPeadHtmlParsing.SAMPLE_OK
    def test_one_ticker_failure_does_not_stop_others(self):
        tickers = {
            "TCS": TICKER_MAP["TCS"],
            "FAIL": {
                "company_name": "Fail Co",
                "nse": "FAIL",
                "bse": "000000",
                "screener": "fail",
                "yahoo": "FAIL.NS",
            },
        }
        def fake_fetch(url: str, info: Dict[str, str]) -> str:
            if info["screener"] == "fail":
                raise RuntimeError("boom")
            return self.SAMPLE_OK
        known_keys: Set[str] = set()
        new_keys: Set[str] = set()
        alerts, status, skipped, errors = scan_pead(
            tickers, known_keys, new_keys, fetch_html=fake_fetch
        )
        self.assertEqual(status, "PARTIAL_FAILURE")
        self.assertEqual(len(alerts), 1)
        self.assertTrue(errors)
class TestCmpFreshness(unittest.TestCase):
    def _response(self, price: float, timestamp: int, currency: str = "INR") -> mock.Mock:
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "regularMarketPrice": price,
                            "regularMarketTime": timestamp,
                            "currency": currency,
                        }
                    }
                ]
            }
        }
        return response
    def test_fresh_cmp(self):
        ts = int(ist_now().timestamp()) - 3600
        with mock.patch(f"{__name__}_http_get", return_value=self._response(100.0, ts)):
            price, cmp_time = fetch_yahoo_cmp("TCS.NS")
            self.assertEqual(price, 100.0)
            self.assertIsInstance(cmp_time, datetime)
    def test_stale_cmp_rejected(self):
        ts = int((ist_now() - timedelta(days=MAX_CMP_AGE_DAYS + 2)).timestamp())
        with mock.patch(f"{__name__}_http_get", return_value=self._response(100.0, ts)):
            with self.assertRaises(ValueError):
                fetch_yahoo_cmp("TCS.NS")
    def test_foreign_currency_rejected(self):
        ts = int(ist_now().timestamp())
        with mock.patch(
            f"{__name__}_http_get", return_value=self._response(100.0, ts, "USD")
        ):
            with self.assertRaises(ValueError):
                fetch_yahoo_cmp("TCS.NS")
class TestStatusAccounting(unittest.TestCase):
    def test_overall_status(self):
        statuses = {"PEAD": "SUCCESS", "ORDERS": "NO_CANDIDATES", "BUYBACK": "SUCCESS"}
        self.assertEqual(compute_overall_status(statuses, "SUCCESS", True, True, 1), "SUCCESS")
        self.assertEqual(compute_overall_status(statuses, "SUCCESS", True, True, 0), "NO_CANDIDATES")
        self.assertEqual(
            compute_overall_status(statuses, "PERSISTENCE_FAILURE", True, True, 0),
            "PERSISTENCE_FAILURE",
        )
        self.assertEqual(
            compute_overall_status(statuses, "SUCCESS", False, True, 0),
            "DELIVERY_FAILURE",
        )
        self.assertEqual(
            compute_overall_status(statuses, "SUCCESS", True, False, 0),
            "DELIVERY_FAILURE",
        )
        self.assertEqual(
            compute_overall_status({"PEAD": "FAILED"}, "SUCCESS", True, True, 0),
            "FAILED",
        )
        self.assertEqual(
            compute_overall_status({"PEAD": "PARTIAL_FAILURE"}, "SUCCESS", True, True, 0),
            "PARTIAL_FAILURE",
        )
    def test_heartbeat_contains_limitations_and_errors(self):
        message = build_heartbeat_message(
            {"PEAD": "SUCCESS", "ORDERS": "NO_CANDIDATES", "BUYBACK": "NO_CANDIDATES"},
            "SUCCESS",
            1,
            {"attempted": 1, "delivered": 1},
            0,
            "SUCCESS",
            ["test error"],
        )
        self.assertIn("5 hard-coded stocks", message)
        self.assertIn("test error", message)
        self.assertIn("Educational/research", message)
class TestTimeoutUsage(unittest.TestCase):
    def test_http_request_sets_timeout(self):
        response = mock.Mock()
        response.status_code = 200
        response.headers = {}
        import __main__ as main_module
        with mock.patch.object(main_module, "SELF_TEST", False), mock.patch(
            "requests.request", return_value=response
        ) as request_mock:
            _http_request("GET", "https://example.invalid")
            _, kwargs = request_mock.call_args
            self.assertEqual(kwargs.get("timeout"), TIMEOUT)
class TestSelfTestNetworkGuard(unittest.TestCase):
    def test_http_guard_blocks_live_network(self):
        with self.assertRaises(RuntimeError):
            _http_get("https://example.invalid")
def run_self_tests() -> bool:
    global SELF_TEST
    SELF_TEST = True
    logging.getLogger().setLevel(logging.CRITICAL)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    with mock.patch(
        "socket.socket.connect",
        side_effect=AssertionError("Live network blocked in self-test"),
    ), mock.patch(
        "socket.create_connection",
        side_effect=AssertionError("Live network blocked in self-test"),
    ):
        runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=2)
        result = runner.run(suite)
    print(
        f"Tests run: {result.testsRun}, failures: {len(result.failures)}, "
        f"errors: {len(result.errors)}"
    )
    return result.wasSuccessful()
# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Project Artha V6.2 scanner")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run offline self-tests and exit",
    )
    args = parser.parse_args(argv)
    if args.self_test:
        return 0 if run_self_tests() else 1
    return run_scan()
if __name__ == "__main__":
    sys.exit(main())
