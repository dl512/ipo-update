"""
HK stock code -> cornerstone investors (HKEX website only).

1. Open HKEX Listed Co. title search, enter code, pick first autocomplete match, SEARCH.
2. Load all results, find IPO allotment-results notice, download PDF.
3. Run extract_allotment_info (word-count precheck + LLM).

Default stdout is a single line: the cornerstone cell ("none" or semicolon-separated names).
Use --verbose for progress on stderr.

Usage:
    python hkex/hkex_cornerstone_from_stock.py --stock 03750
    python hkex/hkex_cornerstone_from_stock.py --stock 03750 --verbose
    python hkex/hkex_cornerstone_from_stock.py --stock 03750 --skip-llm
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import Page, sync_playwright

from extract_allotment_info import extract_with_ai, save_result_to_csv

DEFAULT_TITLE_SEARCH_URL = "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en"
HOME_URL = "https://www.hkexnews.hk/index.htm#"

ALLOTMENT_TITLE_PHRASES: tuple[str, ...] = (
    "announcement of allotment results",
    "announcement of final offer price and allotment results",
    "final offer price and allotment results",
    "allotment results announcement",
    "results of allotment",
    "announcement of allotment",
)


def _log(verbose: bool, msg: str) -> None:
    if verbose:
        print(msg, file=sys.stderr)


def _fill_stock_and_pick_first(page: Page, stock_code: str) -> None:
    code = stock_code.strip().zfill(5)
    if not code.isdigit():
        raise ValueError("stock code must be numeric")

    inp = page.locator("#searchStockCode")
    inp.wait_for(state="visible", timeout=30000)
    inp.scroll_into_view_if_needed()
    inp.click()
    inp.fill(code)
    page.wait_for_timeout(1200)

    first_row = page.locator("tr.autocomplete-suggestion.narrow:not(.suggestion-viewall)").first
    first_row.wait_for(state="visible", timeout=15000)
    first_row.click()
    page.wait_for_timeout(800)


def _click_search(page: Page, use_home: bool) -> None:
    candidates = page.locator("a.filter__btn-applyFilters-js").filter(
        has_text=re.compile(r"SEARCH", re.IGNORECASE)
    )
    for i in range(candidates.count()):
        btn = candidates.nth(i)
        if btn.is_visible():
            btn.click()
            return

    if use_home:
        scoped = page.locator("div:has(#searchStockCode)").locator(
            "a.filter__btn-applyFilters-js"
        ).filter(has_text=re.compile("SEARCH", re.IGNORECASE))
        if scoped.count():
            scoped.first.click()
            return

    raise RuntimeError(
        "Could not find a visible SEARCH control. Use default title-search URL or --headed."
    )


_LOAD_MORE_NAME = re.compile(r"load\s*more", re.IGNORECASE)


def _find_visible_load_more(page: Page):
    for role in ("link", "button"):
        loc = page.get_by_role(role, name=_LOAD_MORE_NAME)
        for i in range(loc.count()):
            el = loc.nth(i)
            if el.is_visible():
                return el
    loc = page.locator("a, button").filter(has_text=_LOAD_MORE_NAME)
    for i in range(loc.count()):
        el = loc.nth(i)
        if el.is_visible():
            return el
    return None


def _click_load_more_until_done(page: Page, max_clicks: int) -> int:
    clicks = 0
    for _ in range(max(0, max_clicks)):
        page.mouse.wheel(0, 2400)
        page.wait_for_timeout(350)
        btn = _find_visible_load_more(page)
        if btn is None:
            break
        try:
            btn.scroll_into_view_if_needed()
            page.wait_for_timeout(250)
            btn.click(timeout=8000)
            clicks += 1
            page.wait_for_timeout(900)
        except Exception:
            break
    return clicks


def _normalize_match_blob(*parts: str) -> str:
    return " ".join(" ".join(parts).lower().split())


def _matches_allotment_title(blob: str) -> bool:
    return any(phrase in blob for phrase in ALLOTMENT_TITLE_PHRASES)


def _find_first_allotment_announcement_url(page: Page) -> tuple[str | None, str | None]:
    rows = page.locator("table tbody tr")
    n = rows.count()
    for i in range(n):
        row = rows.nth(i)
        row_text = row.inner_text()
        link = row.locator(".doc-link a[href]").first
        if link.count() == 0:
            link = row.locator("a[href]").first
        if link.count() == 0:
            continue
        href = (link.get_attribute("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        title = " ".join(link.inner_text().split())
        blob = _normalize_match_blob(row_text, title)
        if not _matches_allotment_title(blob):
            continue
        absolute = href if href.startswith("http") else urljoin(page.url, href)
        return absolute, title or None
    return None, None


def _http_headers() -> dict[str, str]:
    return {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _first_pdf_href_from_notice_html(html: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if ".pdf" in href.lower():
            return urljoin(base_url, href)
    return None


def resolve_allotment_pdf_url(url: str) -> str:
    lower = url.lower().split("?", 1)[0]
    if lower.endswith(".pdf"):
        return url
    if lower.endswith((".htm", ".html", ".xhtml")):
        r = requests.get(url, headers=_http_headers(), timeout=90)
        r.raise_for_status()
        pdf = _first_pdf_href_from_notice_html(r.text, url)
        if not pdf:
            raise RuntimeError(f"No PDF link found on notice page: {url}")
        return pdf
    raise RuntimeError(f"Unsupported allotment document URL: {url}")


def download_allotment_pdf(pdf_url: str, stock_code: str, dest_dir: Path) -> Path:
    code = stock_code.strip().zfill(5)
    dest_dir = dest_dir / code
    dest_dir.mkdir(parents=True, exist_ok=True)
    r = requests.get(pdf_url, headers=_http_headers(), timeout=120, stream=True)
    r.raise_for_status()
    fname = urlparse(pdf_url).path.rstrip("/").split("/")[-1] or f"{code}_allotment.pdf"
    if not fname.lower().endswith(".pdf"):
        fname = f"{code}_allotment.pdf"
    path = dest_dir / fname
    with path.open("wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
    return path


def run_hkex_title_search(
    stock_code: str,
    *,
    use_home: bool,
    headed: bool,
    timeout_ms: int,
    max_load_more: int,
) -> tuple[str, int, int, str | None, str | None]:
    start_url = HOME_URL if use_home else DEFAULT_TITLE_SEARCH_URL
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        page.goto(start_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        _fill_stock_and_pick_first(page, stock_code)
        _click_search(page, use_home)

        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(3000)
        final_url = page.url
        load_more_clicks = _click_load_more_until_done(page, max_load_more)
        row_count = page.locator("table tbody tr").count()
        allotment_url, allotment_title = _find_first_allotment_announcement_url(page)
        browser.close()
        return final_url, row_count, load_more_clicks, allotment_url, allotment_title


def cornerstone_cell_from_extract_result(result: dict) -> str:
    if result.get("no_cornerstone_table"):
        return "none"
    investors = result.get("cornerstone_investors") or []
    if not isinstance(investors, list):
        return ""
    return "; ".join(str(x).strip() for x in investors if str(x).strip())


def fetch_cornerstone_from_hkex(
    stock_code: str,
    *,
    use_home: bool = False,
    headed: bool = False,
    timeout_ms: int = 60000,
    max_load_more: int = 200,
    data_dir: Path = Path("hkex/data"),
    skip_llm: bool = False,
    verbose: bool = False,
) -> dict:
    """
    HKEX crawl + optional LLM. Returns dict with cornerstone_cell (str|None), pdf_path, etc.
    """
    code = stock_code.strip().zfill(5)
    if not code.isdigit():
        raise ValueError("stock code must be numeric")

    _log(verbose, f"[hkex] search stock={code} ...")
    final_url, row_count, load_more_clicks, allotment_url, allotment_title = run_hkex_title_search(
        code,
        use_home=use_home,
        headed=headed,
        timeout_ms=timeout_ms,
        max_load_more=max_load_more,
    )
    out: dict = {
        "stock_code": code,
        "final_search_url": final_url,
        "load_more_clicks": load_more_clicks,
        "results_row_count": row_count,
        "allotment_url": allotment_url,
        "allotment_title": allotment_title,
        "pdf_url": None,
        "pdf_path": None,
        "result": None,
        "cornerstone_cell": None,
        "cache_csv": None,
    }
    _log(verbose, f"[hkex] rows={row_count} load_more_clicks={load_more_clicks}")

    if not allotment_url:
        raise RuntimeError("No allotment announcement link found on HKEXnews.")

    pdf_url = resolve_allotment_pdf_url(allotment_url)
    out["pdf_url"] = pdf_url
    _log(verbose, f"[hkex] allotment: {allotment_title}\n{allotment_url}")
    pdf_path = download_allotment_pdf(pdf_url, code, data_dir)
    out["pdf_path"] = str(pdf_path)
    _log(verbose, f"[hkex] pdf saved: {pdf_path}")

    if skip_llm:
        return out

    _log(verbose, "[extract] LLM ...")
    result = asyncio.run(extract_with_ai(pdf_path))
    out["result"] = result
    out["cornerstone_cell"] = cornerstone_cell_from_extract_result(result)
    cache_csv = data_dir / code / "extracted_cornerstone_only.csv"
    save_result_to_csv(result, cache_csv)
    out["cache_csv"] = str(cache_csv)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Stock code -> cornerstone (HKEX + LLM).")
    parser.add_argument("--stock", required=True, help="HK stock code, e.g. 03750")
    parser.add_argument("--home", action="store_true", help=f"Start from {HOME_URL} instead of title search")
    parser.add_argument("--headed", action="store_true", help="Show browser")
    parser.add_argument("--timeout", type=int, default=60000)
    parser.add_argument("--max-load-more", type=int, default=200)
    parser.add_argument("--data-dir", type=Path, default=Path("hkex/data"))
    parser.add_argument("--skip-llm", action="store_true", help="Download PDF only")
    parser.add_argument("--verbose", "-v", action="store_true", help="Progress on stderr")
    parser.add_argument("--print-json", action="store_true", help="After cornerstone line, print JSON on stderr")
    args = parser.parse_args()

    try:
        out = fetch_cornerstone_from_hkex(
            args.stock,
            use_home=args.home,
            headed=args.headed,
            timeout_ms=args.timeout,
            max_load_more=args.max_load_more,
            data_dir=args.data_dir,
            skip_llm=args.skip_llm,
            verbose=args.verbose,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    if args.skip_llm:
        _log(True, f"PDF only: {out.get('pdf_path')}")
        raise SystemExit(0)

    cell = out.get("cornerstone_cell")
    if cell is None:
        print("extractor returned no cornerstone_cell", file=sys.stderr)
        raise SystemExit(1)

    print(cell)
    if args.print_json and out.get("result"):
        print(json.dumps(out["result"], ensure_ascii=False, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
