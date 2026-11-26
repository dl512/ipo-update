"""
IPO news headlines for the daily digest (title + link).

Sources:
- AAStocks: https://www.aastocks.com/tc/stocks/market/ipo/iponews.aspx
- HKET:     https://inews.hket.com/sran009-2/新股IPO

Default window: last 3 calendar days through today (Asia/Hong_Kong), inclusive
(today and the prior 2 days — no trading-day logic).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import unescape
from urllib.parse import unquote, urljoin
from zoneinfo import ZoneInfo

import requests

_HKT = ZoneInfo("Asia/Hong_Kong")
IPO_NEWS_LOOKBACK_DAYS = 3  # inclusive calendar days ending today
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
}

AASTOCKS_IPO_NEWS_URL = "https://www.aastocks.com/tc/stocks/market/ipo/iponews.aspx"
HKET_IPO_NEWS_URL = "https://inews.hket.com/sran009-2/%E6%96%B0%E8%82%A1IPO"

_AA_ITEM_RE = re.compile(
    r'class="newshead4[^"]*"[\s\S]{0,500}?<a\s+([^>]+)>([\s\S]*?)</a>'
    r"[\s\S]{0,900}?ConvertToLocalTime\(\{dt:'([^']+)'\}\)",
    re.I,
)
_HKET_ITEM_RE = re.compile(
    r'class="listing-title"[\s\S]{0,500}?<a\s+([^>]+)>([\s\S]*?)</a>'
    r'[\s\S]{0,1200}?class="listing-date"[\s\S]{0,400}?(20\d{2}/\d{1,2}/\d{1,2})',
    re.I,
)
_ATTR_HREF_RE = re.compile(r'\bhref="([^"]+)"', re.I)
_ATTR_TITLE_RE = re.compile(r'\btitle="([^"]*)"', re.I)


@dataclass(frozen=True)
class IpoNewsItem:
    title: str
    url: str
    published: date
    source: str


def news_date_window(
    today: date | None = None,
    *,
    lookback_days: int = IPO_NEWS_LOOKBACK_DAYS,
) -> tuple[date, date]:
    """
    Inclusive calendar window ending on ``today`` (HKT).

    ``lookback_days=3`` → today and the previous 2 days (e.g. Nov 24–26 if today is Nov 26).
    """
    end = today or datetime.now(_HKT).date()
    days = max(1, int(lookback_days))
    start = end - timedelta(days=days - 1)
    return start, end


def _parse_aastocks_dt(raw: str) -> date | None:
    raw = (raw or "").strip()
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_hket_dt(raw: str) -> date | None:
    raw = " ".join((raw or "").split())
    m = re.search(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", raw)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _clean_title(title: str) -> str:
    t = unescape(re.sub(r"<[^>]+>", " ", title or ""))
    return " ".join(t.split()).strip()


def _title_and_href(anchor_attrs: str, anchor_inner: str) -> tuple[str, str]:
    href_m = _ATTR_HREF_RE.search(anchor_attrs or "")
    title_m = _ATTR_TITLE_RE.search(anchor_attrs or "")
    href = (href_m.group(1) if href_m else "").strip()
    title = _clean_title(title_m.group(1) if title_m else "") or _clean_title(anchor_inner)
    return title, href


def fetch_aastocks_ipo_news(*, timeout: int = 60) -> list[IpoNewsItem]:
    r = requests.get(AASTOCKS_IPO_NEWS_URL, headers=_HTTP_HEADERS, timeout=timeout)
    r.raise_for_status()
    text = r.text
    out: list[IpoNewsItem] = []
    seen: set[str] = set()
    for m in _AA_ITEM_RE.finditer(text):
        title, href = _title_and_href(m.group(1), m.group(2))
        pub = _parse_aastocks_dt(m.group(3))
        if pub is None or not href or not title:
            continue
        url = urljoin(AASTOCKS_IPO_NEWS_URL, href)
        if url in seen:
            continue
        seen.add(url)
        out.append(IpoNewsItem(title=title, url=url, published=pub, source="AAStocks"))
    return out


def fetch_hket_ipo_news(*, timeout: int = 60) -> list[IpoNewsItem]:
    r = requests.get(
        HKET_IPO_NEWS_URL,
        headers={**_HTTP_HEADERS, "Referer": "https://inews.hket.com/"},
        timeout=timeout,
    )
    r.raise_for_status()
    text = r.text
    out: list[IpoNewsItem] = []
    seen: set[str] = set()
    for m in _HKET_ITEM_RE.finditer(text):
        title, href = _title_and_href(m.group(1), m.group(2))
        pub = _parse_hket_dt(m.group(3))
        if pub is None or not href or not title:
            if not title and href:
                slug = unquote(href.split("/")[-1].split("?")[0])
                title = _clean_title(slug.replace("-", " "))
        if pub is None or not href or not title:
            continue
        url = urljoin("https://inews.hket.com", href.split("?")[0])
        if url in seen:
            continue
        seen.add(url)
        out.append(IpoNewsItem(title=title, url=url, published=pub, source="HKET"))
    return out


def collect_ipo_news(
    *,
    start: date | None = None,
    end: date | None = None,
    timeout: int = 60,
) -> list[IpoNewsItem]:
    """Fetch both sources and keep items with published date in [start, end]."""
    if end is None or start is None:
        start, end = news_date_window()
    assert start is not None and end is not None
    items: list[IpoNewsItem] = []
    errors: list[str] = []
    for label, fn in (("AAStocks", fetch_aastocks_ipo_news), ("HKET", fetch_hket_ipo_news)):
        try:
            items.extend(fn(timeout=timeout))
        except Exception as exc:  # noqa: BLE001 — digest should still send other sections
            errors.append(f"{label}: {exc}")
    filtered = [it for it in items if start <= it.published <= end]
    # Newest first; stable by source then title
    filtered.sort(key=lambda x: (x.published, x.source, x.title), reverse=True)
    # expose errors via attribute for caller logging
    collect_ipo_news.last_errors = errors  # type: ignore[attr-defined]
    return filtered


def build_ipo_news_markdown(items: list[IpoNewsItem], *, start: date, end: date) -> tuple[str, int]:
    """Markdown bullet list of titled links. Returns (markdown, count)."""
    if not items:
        body = f"_No IPO news from {start.isoformat()} to {end.isoformat()}_"
        return body, 0
    lines = [f"_Window: {start.isoformat()} → {end.isoformat()} (last {IPO_NEWS_LOOKBACK_DAYS} calendar days)_", ""]
    for it in items:
        # markdown link; escape nothing special beyond newlines already cleaned
        lines.append(f"- {it.published.isoformat()} · {it.source}: [{it.title}]({it.url})")
    return "\n".join(lines).strip() + "\n", len(items)
