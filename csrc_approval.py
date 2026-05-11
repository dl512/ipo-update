"""
Fetch CSRC approval list, extract "全流通备案通知书" rows, and upsert to CSV.

The zfxxgk list page fills via AJAX: GET /searchList/{channelId}?_isJson=true...
Static HTML has an empty placeholder, so scraping the .shtml alone yields 0 rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

from google_sheets_store import hkex_sheets_enabled, read_sheet_df, write_sheet_df

CSRC_APPROVAL_URL = (
    "https://www.csrc.gov.cn/csrc/c101935/zfxxgk_zdgk.shtml?channelid=8f3f0d4be56b4f8aa8183b3234b88ede"
)
CSRC_ORIGIN = "https://www.csrc.gov.cn"
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": f"{CSRC_ORIGIN}/",
}
# CSRC titles vary: …股份"全流通"的备案通知书 vs …股份"全流通"备案通知书 (no 的)
_QUOTES = r'["\u201c\u201d\u2018\u2019\u300c\u300d]'
_TITLE_PATTERN = re.compile(
    rf"关于(?P<company>.+?)境外发行上市及境内未上市股份\s*{_QUOTES}?\s*全流通\s*{_QUOTES}?\s*的?备案通知书"
)
_DATE_IN_TEXT = re.compile(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})")
_META_CHANNEL_RE = re.compile(
    r'<meta\s+name=["\"]channelid["\"]\s+content=["\"]([^"\']+)["\"]', re.I
)


@dataclass
class CsrcApprovalUpsertResult:
    csv_path: Path
    added_rows: list[dict[str, str]]


def _extract_company_from_title(title: str) -> str | None:
    m = _TITLE_PATTERN.search(str(title).strip())
    if not m:
        return None
    return m.group("company").strip()


def _normalize_date_cell(s: str) -> str:
    s = str(s).strip()
    m = _DATE_IN_TEXT.search(s)
    if not m:
        return s
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    return f"{y}-{mo:02d}-{d:02d}"


def _abs_csrc_url(href: str) -> str:
    h = str(href).strip()
    if not h:
        return CSRC_APPROVAL_URL
    if h.startswith("http://") or h.startswith("https://"):
        return h
    if h.startswith("/"):
        return CSRC_ORIGIN + h
    return f"{CSRC_ORIGIN}/{h}"


def _channel_id_from_list_url(url: str) -> str | None:
    q = parse_qs(urlparse(url).query)
    for key in ("channelid", "channelId", "CHANNELID"):
        if key in q and q[key][0].strip():
            return q[key][0].strip()
    return None


def _channel_id_from_page_html(html: str) -> str | None:
    m = _META_CHANNEL_RE.search(html)
    if m:
        return m.group(1).strip()
    return None


def _check_html_forbidden(html: str) -> None:
    if re.search(r"<title>\s*403\s*</title>", html, re.I) or re.search(
        r"<h1>\s*403\s*</h1>", html, re.I
    ):
        raise RuntimeError(
            "CSRC returned 403 Forbidden for this request (common from some datacenters/VPNs). "
            "Run from a network that can open the page in a browser."
        )


def _fetch_search_api_rows(
    channel_id: str,
    *,
    timeout: int,
    page_size: int = 100,
    max_pages: int = 40,
) -> list[dict]:
    """Mirror site JS: render.js table_ajax → GET /searchList/{id}?_isJson=true..."""
    out: list[dict] = []
    page = 1
    total: int | None = None
    list_url = f"{CSRC_ORIGIN}/searchList/{channel_id}"
    while page <= max_pages:
        r = requests.get(
            list_url,
            params={
                "_isAgg": "true",
                "_isJson": "true",
                "_pageSize": str(page_size),
                "_template": "index",
                "_rangeTimeGte": "",
                "_channelName": "",
                "page": str(page),
            },
            headers=_HTTP_HEADERS,
            timeout=timeout,
        )
        r.raise_for_status()
        payload = r.json()
        data = payload.get("data") or {}
        if total is None:
            try:
                total = int(data.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
        batch = data.get("results") or []
        if not batch:
            break
        out.extend(batch)
        if total and page * page_size >= total:
            break
        page += 1
    return out


def _matching_rows_from_api_items(items: list[dict], list_page_url: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in items:
        title = str(item.get("title") or "").strip()
        company = _extract_company_from_title(title)
        if not company:
            continue
        pub = str(item.get("publishedTimeStr") or item.get("publishedTime") or "").strip()
        announce_date = _normalize_date_cell(pub[:10] if len(pub) >= 10 else pub)
        detail = _abs_csrc_url(str(item.get("url") or "").strip())
        rows.append(
            {
                "company_name": company,
                "title": title,
                "announcement_date": announce_date,
                "source_url": detail if detail else list_page_url,
            }
        )
    return rows


def _pick_title_and_date_from_row(cells: list[str]) -> tuple[str, str] | None:
    title = ""
    for raw in cells:
        t = raw.strip()
        if not t or t in ("序号", "标题", "文号", "发文日期"):
            continue
        if _extract_company_from_title(t):
            title = t
            break
    if not title:
        return None
    announce_date = ""
    if len(cells) >= 4:
        announce_date = _normalize_date_cell(cells[3])
    if not _DATE_IN_TEXT.search(announce_date):
        for raw in reversed(cells):
            if raw != title and _DATE_IN_TEXT.search(raw):
                announce_date = _normalize_date_cell(raw)
                break
    return title, announce_date


def _extract_rows_from_tables(soup: BeautifulSoup) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for tr in soup.select("tr"):
        cells_el = tr.find_all(["td", "th"])
        if len(cells_el) < 2:
            continue
        values = [c.get_text(" ", strip=True) for c in cells_el]
        if values and values[0].strip() in ("序号", "序号 "):
            continue
        picked = _pick_title_and_date_from_row(values)
        if not picked:
            continue
        title, announce_date = picked
        company = _extract_company_from_title(title)
        if not company:
            continue
        rows.append(
            {
                "company_name": company,
                "title": title,
                "announcement_date": announce_date,
                "source_url": CSRC_APPROVAL_URL,
            }
        )
    return rows


def _extract_rows_from_free_text(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for m in _TITLE_PATTERN.finditer(text):
        title = m.group(0).strip()
        company = m.group("company").strip()
        if not company:
            continue
        tail = text[m.end() : m.end() + 120]
        dm = _DATE_IN_TEXT.search(tail)
        announce_date = _normalize_date_cell(dm.group(0)) if dm else ""
        rows.append(
            {
                "company_name": company,
                "title": title,
                "announcement_date": announce_date,
                "source_url": CSRC_APPROVAL_URL,
            }
        )
    return rows


def _extract_rows_from_html(html: str, list_url: str) -> list[dict[str, str]]:
    _check_html_forbidden(html)
    soup = BeautifulSoup(html, "lxml")
    from_table = _extract_rows_from_tables(soup)
    if from_table:
        for r in from_table:
            r["source_url"] = list_url
        return from_table
    text = soup.get_text("\n", strip=True)
    return _extract_rows_from_free_text(text)


def fetch_csrc_approvals(url: str = CSRC_APPROVAL_URL, timeout: int = 60) -> list[dict[str, str]]:
    """
    Prefer /searchList JSON (same as browser). Fall back to HTML scrape if needed.
    """
    channel_id = _channel_id_from_list_url(url)
    html_for_meta: str | None = None

    if not channel_id:
        r0 = requests.get(url, headers=_HTTP_HEADERS, timeout=timeout)
        r0.raise_for_status()
        if r0.encoding is None or str(r0.encoding).lower() in ("iso-8859-1", "ascii"):
            r0.encoding = r0.apparent_encoding or "utf-8"
        html_for_meta = r0.text
        _check_html_forbidden(html_for_meta)
        channel_id = _channel_id_from_page_html(html_for_meta) or ""

    if channel_id:
        try:
            raw_items = _fetch_search_api_rows(channel_id, timeout=timeout)
            matched = _matching_rows_from_api_items(raw_items, url)
            if matched:
                return matched
        except (requests.RequestException, ValueError, KeyError, TypeError):
            pass

    if html_for_meta is None:
        r0 = requests.get(url, headers=_HTTP_HEADERS, timeout=timeout)
        r0.raise_for_status()
        if r0.encoding is None or str(r0.encoding).lower() in ("iso-8859-1", "ascii"):
            r0.encoding = r0.apparent_encoding or "utf-8"
        html_for_meta = r0.text

    _check_html_forbidden(html_for_meta)
    return _extract_rows_from_html(html_for_meta, url)


def upsert_csrc_approvals(csv_path: Path, rows: list[dict[str, str]]) -> CsrcApprovalUpsertResult:
    csv_path = csv_path.resolve()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    incoming = pd.DataFrame(rows).fillna("")
    cols = ["company_name", "title", "announcement_date", "source_url"]

    if incoming.empty:
        if hkex_sheets_enabled():
            return CsrcApprovalUpsertResult(csv_path=csv_path, added_rows=[])
        if not csv_path.exists():
            empty = pd.DataFrame(columns=cols)
            empty.to_csv(csv_path, index=False, encoding="utf-8-sig")
        return CsrcApprovalUpsertResult(csv_path=csv_path, added_rows=[])

    for c in cols:
        if c not in incoming.columns:
            incoming[c] = ""
    incoming = incoming[cols].astype(str)

    if hkex_sheets_enabled():
        existing = read_sheet_df("csrc")
        if existing.empty:
            existing = pd.DataFrame(columns=cols)
        for c in cols:
            if c not in existing.columns:
                existing[c] = ""
        existing = existing[cols].astype(str)
    elif csv_path.exists():
        existing = pd.read_csv(csv_path, dtype=str).fillna("")
        for c in cols:
            if c not in existing.columns:
                existing[c] = ""
        existing = existing[cols].astype(str)
    else:
        existing = pd.DataFrame(columns=cols)

    existing_keys = set(
        tuple(existing.loc[i, ["company_name", "announcement_date", "title"]].tolist()) for i in existing.index
    )
    added: list[dict[str, str]] = []
    for _, r in incoming.iterrows():
        key = (r["company_name"], r["announcement_date"], r["title"])
        if key in existing_keys:
            continue
        existing_keys.add(key)
        added.append(
            {
                "company_name": r["company_name"],
                "title": r["title"],
                "announcement_date": r["announcement_date"],
                "source_url": r["source_url"],
            }
        )

    if added:
        merged = pd.concat([existing, pd.DataFrame(added)], ignore_index=True)
        if hkex_sheets_enabled():
            write_sheet_df("csrc", merged)
        else:
            merged.to_csv(csv_path, index=False, encoding="utf-8-sig")
    elif not hkex_sheets_enabled() and not csv_path.exists():
        existing.to_csv(csv_path, index=False, encoding="utf-8-sig")

    return CsrcApprovalUpsertResult(csv_path=csv_path, added_rows=added)


def build_csrc_new_rows_markdown(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "_No new CSRC approvals added in this run._"
    lines = []
    for r in rows:
        d = r.get("announcement_date", "").strip() or "—"
        name = r.get("company_name", "").strip() or "—"
        lines.append(f"- **{name}** — {d}")
    return "\n".join(lines)
