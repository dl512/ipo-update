"""
HKEX Disclosure of Interests — detect whether an issuer also has A Shares.

Opens the listed-corporation search form, submits a stock code, and checks whether
any result row's \"Name of listed corporation\" mentions A Shares / A-share.

Form: https://di.hkex.com.hk/di/NSSrchCorp.aspx?src=MAIN&lang=EN&g_lang=en
"""

from __future__ import annotations

import re
import time
from datetime import date
from typing import Iterable

import requests
from bs4 import BeautifulSoup

DI_FORM_URL = "https://di.hkex.com.hk/di/NSSrchCorp.aspx?src=MAIN&lang=EN&g_lang=en"
_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": DI_FORM_URL,
}

# Matches \"A Shares\", \"A Share\", \"A-Shares\", \"A-share\", etc. in the corp name column.
_A_SHARE_NAME_RE = re.compile(r"\bA[\s-]*Shares?\b", re.IGNORECASE)


def _normalize_stock_code(stock_code: str) -> str:
    s = str(stock_code).strip()
    if s.replace(".", "").isdigit():
        s = str(int(float(s)))
    if not s.isdigit():
        raise ValueError(f"stock code must be numeric, got {stock_code!r}")
    return s.zfill(5)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HTTP_HEADERS)
    return s


def fetch_listed_corporation_names(
    stock_code: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 60,
) -> list[str]:
    """
    Return \"Name of listed corporation\" values from the DI current-securities table
    for ``stock_code`` (e.g. ``['... - H Shares', '... - A Shares']``).
    """
    code5 = _normalize_stock_code(stock_code)
    code_query = str(int(code5))  # DI expects unpadded in the form / query
    own = session is None
    s = session or _session()
    try:
        r = s.get(DI_FORM_URL, timeout=timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        payload: dict[str, str] = {}
        for inp in soup.find_all("input"):
            name = inp.get("name")
            if name:
                payload[name] = inp.get("value") or ""

        today = date.today()
        payload["txtStockCode"] = code_query
        payload["txtCorpName"] = ""
        payload["ddlStartDateDD"] = "01"
        payload["ddlStartDateMM"] = "01"
        payload["ddlStartDateYYYY"] = "2003"
        payload["ddlEndDateDD"] = f"{today.day:02d}"
        payload["ddlEndDateMM"] = f"{today.month:02d}"
        payload["ddlEndDateYYYY"] = str(today.year)
        payload["cmdSearch"] = "Search"
        payload.pop("cmdBack", None)

        post = s.post(DI_FORM_URL, data=payload, timeout=timeout, allow_redirects=True)
        post.raise_for_status()
        soup2 = BeautifulSoup(post.text, "lxml")

        names: list[str] = []
        seen: set[str] = set()
        for tr in soup2.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            code_cell = " ".join(tds[0].get_text(" ", strip=True).split())
            name_cell = " ".join(tds[1].get_text(" ", strip=True).split())
            if not name_cell:
                continue
            # Row stock code is zero-padded (06935 / 02384).
            if code_cell.isdigit() and code_cell.zfill(5) == code5:
                if name_cell not in seen:
                    seen.add(name_cell)
                    names.append(name_cell)
        return names
    finally:
        if own:
            s.close()


def is_a_share_listed(
    stock_code: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 60,
) -> bool:
    """True if any DI corp-name row mentions A Shares / A-share."""
    names = fetch_listed_corporation_names(stock_code, session=session, timeout=timeout)
    return any(_A_SHARE_NAME_RE.search(n) for n in names)


def a_share_listed_cell(
    stock_code: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 60,
) -> str:
    """Canonical listings-cell value: ``true`` / ``false``."""
    return "true" if is_a_share_listed(stock_code, session=session, timeout=timeout) else "false"


def backfill_a_share_listed(
    codes: Iterable[str],
    *,
    sleep_s: float = 0.4,
    timeout: int = 60,
) -> dict[str, str]:
    """Map stock_code -> ``true``/``false``. Reuses one HTTP session."""
    code_list = [_normalize_stock_code(c) for c in codes]
    out: dict[str, str] = {}
    with _session() as s:
        for i, code in enumerate(code_list):
            out[code] = a_share_listed_cell(code, session=s, timeout=timeout)
            if sleep_s > 0 and i + 1 < len(code_list):
                time.sleep(sleep_s)
    return out
