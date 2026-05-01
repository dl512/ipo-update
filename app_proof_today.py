"""
Fetch HKEX New Listing Information (Application Proof & PHIP) and list today's Main Board rows.

The HTML page https://www1.hkexnews.hk/app/appindex.html loads applicant data from JSON (no Playwright needed).
Application Proof document URLs use base https://www1.hkexnews.hk/app/ + relative path from the feed.

Usage (from hkex/):
    python app_proof_today.py
    python app_proof_today.py --date 2026-05-01
    python app_proof_today.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

# English, Main Board, active applicants (matches default tab on app index).
APPACTIVE_JSON_URL = "https://www1.hkexnews.hk/ncms/json/eds/appactive_app_sehk_e.json"
APP_DOC_BASE = "https://www1.hkexnews.hk/app/"
_HKT = ZoneInfo("Asia/Hong_Kong")
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def fetch_active_applicants_feed(url: str = APPACTIVE_JSON_URL, timeout: int = 60) -> dict[str, Any]:
    r = requests.get(url, headers=_HTTP_HEADERS, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict) or "app" not in data:
        raise ValueError("Unexpected JSON shape: expected top-level 'app' array")
    return data


def _parse_dd_mm_yyyy(s: str) -> date | None:
    s = str(s).strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _hk_today() -> date:
    return datetime.now(_HKT).date()


def pick_application_proof_link(entry: dict[str, Any]) -> tuple[str | None, str | None]:
    """
    From one applicant record, return (label, absolute_url) for the best Application Proof line.
    Prefers PDF (u1) when present; otherwise notice page (u2).
    """
    best: tuple[int, str | None, str | None] | None = None
    for item in entry.get("ls") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("nF") or "").strip()
        if "application proof" not in label.lower():
            continue
        u1 = str(item.get("u1") or "").strip()
        u2 = str(item.get("u2") or "").strip()
        rel = u1 or u2
        if not rel:
            continue
        # Prefer PDF path
        score = 2 if rel.lower().endswith(".pdf") else 1
        d = _parse_dd_mm_yyyy(str(item.get("d") or ""))
        day_ord = d.toordinal() if d else 0
        cand = (day_ord, score, rel, label or None)
        if best is None or cand[:2] > best[:2]:
            best = cand
    if best is None:
        return None, None
    _, _, rel, label = best
    rel = rel.lstrip("/")
    return label, APP_DOC_BASE + rel


def applicants_for_listing_date(target: date, feed: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Applicants whose main date field ``d`` (DD/MM/YYYY) equals ``target`` (Hong Kong calendar day)."""
    if feed is None:
        feed = fetch_active_applicants_feed()
    out: list[dict[str, Any]] = []
    for entry in feed.get("app") or []:
        if not isinstance(entry, dict):
            continue
        d = _parse_dd_mm_yyyy(str(entry.get("d") or ""))
        if d != target:
            continue
        label, url = pick_application_proof_link(entry)
        out.append(
            {
                "company_name": str(entry.get("a") or "").strip(),
                "application_proof_label": label,
                "application_proof_url": url,
                "listing_date": str(entry.get("d") or "").strip(),
                "posting_date": str(entry.get("postingDate") or "").strip(),
                "applicant_id": entry.get("id"),
            }
        )
    return out


def build_application_proof_markdown_section(
    target: date,
    feed: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """
    Markdown fragment (no top-level heading) for applicants with main feed date ``d`` = ``target``,
    with Application Proof links. For use inside a larger digest email.
    Returns (markdown, row count).
    """
    rows = applicants_for_listing_date(target, feed)
    intro = (
        f"_Listing date field `d` = **{target.isoformat()}**. "
        f"Data: [New Listing Information – AP & PHIP](https://www1.hkexnews.hk/app/appindex.html) "
        f"(Main Board active English JSON feed)._"
    )
    if not rows:
        body = (
            intro
            + "\n\n"
            + "_No matching active applicants in the feed for this date, or no Application Proof entries._\n"
        )
        return body, 0

    lines = [intro, ""]
    for r in rows:
        name = r["company_name"]
        url = r["application_proof_url"]
        label = (r.get("application_proof_label") or "Application Proof").strip()
        if url:
            lines.append(f"- **{name}** — [{label}]({url})")
        else:
            lines.append(f"- **{name}** — _no Application Proof URL in feed_")
    lines.append("")
    return "\n".join(lines), len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List Main Board active applicants from HKEX app index feed for a given listing date."
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Target listing date (YYYY-MM-DD or DD/MM/YYYY). Default: today in Asia/Hong_Kong.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON array to stdout")
    parser.add_argument("--feed-url", type=str, default=APPACTIVE_JSON_URL, help="Override JSON endpoint")
    args = parser.parse_args()

    if args.date:
        target = None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                target = datetime.strptime(args.date.strip(), fmt).date()
                break
            except ValueError:
                continue
        if target is None:
            raise SystemExit(f"Invalid --date {args.date!r}")
    else:
        target = _hk_today()

    feed = fetch_active_applicants_feed(args.feed_url)
    rows = applicants_for_listing_date(target, feed)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    print(f"Listing date (HKT): {target.isoformat()} | rows: {len(rows)} | feed: {args.feed_url}", file=sys.stderr)
    for r in rows:
        name = r["company_name"]
        url = r["application_proof_url"] or "—"
        print(f"{name}\t{url}")
    if not rows:
        print("(no applicants with this listing date in the active Main Board feed)", file=sys.stderr)


if __name__ == "__main__":
    main()
