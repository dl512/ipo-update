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
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from google_sheets_store import hkex_sheets_enabled, upsert_rows_dataframe

# Chinese, Main Board, active applicants.
APPACTIVE_JSON_URL = "https://www1.hkexnews.hk/ncms/json/eds/appactive_app_sehk_c.json"
PHIPACTIVE_JSON_URL = "https://www1.hkexnews.hk/ncms/json/eds/appactive_appphip_sehk_c.json"
APP_INDEX_URL = "https://www1.hkexnews.hk/app/appindex.html?lang=zh"
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


def _pick_doc_link(entry: dict[str, Any], *, doc_type: str) -> tuple[str | None, str | None]:
    """
    From one applicant record, return (label, absolute_url) for the best matching line.
    doc_type: "application_proof" or "phip"
    """
    best: tuple[int, int, int, str | None, str | None] | None = None
    for item in entry.get("ls") or []:
        if not isinstance(item, dict):
            continue
        # zh feed commonly uses nS1 (not nF). Keep broad fallbacks for feed schema drift.
        label = str(
            item.get("nF")
            or item.get("nS1")
            or item.get("nS2")
            or item.get("nS")
            or item.get("n")
            or ""
        ).strip()
        u1 = str(item.get("u1") or "").strip()
        u2 = str(item.get("u2") or "").strip()
        rel = u1 or u2
        if not rel:
            continue

        label_l = label.lower()
        is_ap = ("application proof" in label_l) or ("申請版本" in label) or ("申请版本" in label)
        is_phip = ("phip" in label_l) or ("聆訊後資料集" in label) or ("聆讯后资料集" in label)
        if doc_type == "application_proof" and not is_ap:
            continue
        if doc_type == "phip" and not is_phip:
            continue

        # Prefer "全文檔案/全文档案/full file", then direct pdf.
        full_file_score = (
            2
            if ("全文檔案" in label or "全文档案" in label or "full file" in label_l)
            else 1
        )
        pdf_score = 2 if rel.lower().endswith(".pdf") else 1
        d = _parse_dd_mm_yyyy(str(item.get("d") or ""))
        day_ord = d.toordinal() if d else 0
        cand = (day_ord, full_file_score, pdf_score, rel, label or None)
        if best is None or cand[:3] > best[:3]:
            best = cand
    if best is None:
        return None, None
    _, _, _, rel, label = best
    rel = rel.lstrip("/")
    return label, APP_DOC_BASE + rel


def pick_application_proof_link(entry: dict[str, Any]) -> tuple[str | None, str | None]:
    return _pick_doc_link(entry, doc_type="application_proof")


def pick_phip_link(entry: dict[str, Any]) -> tuple[str | None, str | None]:
    return _pick_doc_link(entry, doc_type="phip")


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


def phip_for_listing_date(target: date, feed: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Applicants whose main date field ``d`` equals ``target``, with best PHIP link."""
    if feed is None:
        feed = fetch_active_applicants_feed(PHIPACTIVE_JSON_URL)
    out: list[dict[str, Any]] = []
    for entry in feed.get("app") or []:
        if not isinstance(entry, dict):
            continue
        d = _parse_dd_mm_yyyy(str(entry.get("d") or ""))
        if d != target:
            continue
        label, url = pick_phip_link(entry)
        out.append(
            {
                "company_name": str(entry.get("a") or "").strip(),
                "phip_label": label,
                "phip_url": url,
                "listing_date": str(entry.get("d") or "").strip(),
                "posting_date": str(entry.get("postingDate") or "").strip(),
                "applicant_id": entry.get("id"),
            }
        )
    return out


def all_active_application_proof_rows(feed: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """All Main Board active applicants in the feed with best Application Proof link (any listing date)."""
    if feed is None:
        feed = fetch_active_applicants_feed(APPACTIVE_JSON_URL)
    out: list[dict[str, Any]] = []
    for entry in feed.get("app") or []:
        if not isinstance(entry, dict):
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


def all_active_phip_rows(feed: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """All active applicants with best PHIP link (any listing date)."""
    if feed is None:
        feed = fetch_active_applicants_feed(PHIPACTIVE_JSON_URL)
    out: list[dict[str, Any]] = []
    for entry in feed.get("app") or []:
        if not isinstance(entry, dict):
            continue
        label, url = pick_phip_link(entry)
        out.append(
            {
                "company_name": str(entry.get("a") or "").strip(),
                "phip_label": label,
                "phip_url": url,
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
    if not rows:
        return "_No new Application Proof entries for this date._\n", 0

    lines: list[str] = []
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


def build_phip_markdown_section(
    target: date,
    feed: dict[str, Any] | None = None,
) -> tuple[str, int]:
    rows = phip_for_listing_date(target, feed)
    if not rows:
        return "_No new PHIP entries for this date._\n", 0

    lines: list[str] = []
    for r in rows:
        name = r["company_name"]
        url = r["phip_url"]
        label = (r.get("phip_label") or "PHIP").strip()
        if url:
            lines.append(f"- **{name}** — [{label}]({url})")
        else:
            lines.append(f"- **{name}** — _no PHIP URL in feed_")
    lines.append("")
    return "\n".join(lines), len(rows)


def build_application_proof_markdown_from_rows(rows: list[dict[str, Any]]) -> tuple[str, int]:
    """Markdown for a list of applicant rows (e.g. newly upserted). Returns (markdown, row count)."""
    if not rows:
        return "_No new Application Proof entries in this run._\n", 0
    lines: list[str] = []
    for r in rows:
        name = str(r.get("company_name") or "").strip() or "—"
        url = r.get("application_proof_url")
        label = str(r.get("application_proof_label") or "Application Proof").strip()
        d = str(r.get("listing_date") or "").strip()
        suffix = f" (listing date {d})" if d else ""
        if url:
            lines.append(f"- **{name}**{suffix} — [{label}]({url})")
        else:
            lines.append(f"- **{name}**{suffix} — _no Application Proof URL in feed_")
    lines.append("")
    return "\n".join(lines), len(rows)


def build_phip_markdown_from_rows(rows: list[dict[str, Any]]) -> tuple[str, int]:
    if not rows:
        return "_No new PHIP entries in this run._\n", 0
    lines: list[str] = []
    for r in rows:
        name = str(r.get("company_name") or "").strip() or "—"
        url = r.get("phip_url")
        label = str(r.get("phip_label") or "PHIP").strip()
        d = str(r.get("listing_date") or "").strip()
        suffix = f" (listing date {d})" if d else ""
        if url:
            lines.append(f"- **{name}**{suffix} — [{label}]({url})")
        else:
            lines.append(f"- **{name}**{suffix} — _no PHIP URL in feed_")
    lines.append("")
    return "\n".join(lines), len(rows)


def _app_doc_sheet_kind(path: Path) -> str | None:
    if not hkex_sheets_enabled():
        return None
    name = path.name
    if name == "application_proof_current.csv":
        return "application_proof"
    if name == "phip_current.csv":
        return "phip"
    return None


def _upsert_rows_to_csv(
    path: Path, rows: list[dict[str, Any]], key_cols: list[str]
) -> tuple[int, list[dict[str, Any]]]:
    """Append only rows whose key tuple is not already present. Returns (count appended, new row dicts)."""
    path = path.resolve()
    kind = _app_doc_sheet_kind(path)
    n, _, added = upsert_rows_dataframe(csv_path=path, sheet_kind=kind, rows=rows, key_cols=key_cols)
    return n, added


def persist_app_docs_for_dates(
    dates: list[date],
    out_dir: Path,
    *,
    app_feed: dict[str, Any] | None = None,
    phip_feed: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """
    Upsert AP + PHIP rows for given listing dates into CSV snapshots.
    Returns (new_app_rows_added, new_phip_rows_added).
    """
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    app_feed = app_feed or fetch_active_applicants_feed(APPACTIVE_JSON_URL)
    phip_feed = phip_feed or fetch_active_applicants_feed(PHIPACTIVE_JSON_URL)

    app_rows: list[dict[str, Any]] = []
    phip_rows: list[dict[str, Any]] = []
    snapshot_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for d in dates:
        for r in applicants_for_listing_date(d, app_feed):
            r = dict(r)
            r["doc_type"] = "application_proof"
            r["snapshot_time"] = snapshot_time
            app_rows.append(r)
        for r in phip_for_listing_date(d, phip_feed):
            r = dict(r)
            r["doc_type"] = "phip"
            r["snapshot_time"] = snapshot_time
            phip_rows.append(r)

    n_app, _ = _upsert_rows_to_csv(
        out_dir / "application_proof_current.csv",
        app_rows,
        key_cols=["company_name"],
    )
    n_phip, _ = _upsert_rows_to_csv(
        out_dir / "phip_current.csv",
        phip_rows,
        key_cols=["company_name"],
    )
    return n_app, n_phip


def persist_all_active_app_docs(
    out_dir: Path,
    *,
    app_feed: dict[str, Any] | None = None,
    phip_feed: dict[str, Any] | None = None,
) -> tuple[int, int, list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Upsert every active Main Board Application Proof / PHIP row from the feeds against CSV or Google Sheet.
    Returns (new_app_count, new_phip_count, new_app_rows, new_phip_rows) for digest/email.
    """
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    app_feed = app_feed or fetch_active_applicants_feed(APPACTIVE_JSON_URL)
    phip_feed = phip_feed or fetch_active_applicants_feed(PHIPACTIVE_JSON_URL)

    snapshot_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    app_rows: list[dict[str, Any]] = []
    for r in all_active_application_proof_rows(app_feed):
        r = dict(r)
        r["doc_type"] = "application_proof"
        r["snapshot_time"] = snapshot_time
        app_rows.append(r)
    phip_rows: list[dict[str, Any]] = []
    for r in all_active_phip_rows(phip_feed):
        r = dict(r)
        r["doc_type"] = "phip"
        r["snapshot_time"] = snapshot_time
        phip_rows.append(r)

    n_app, added_app = _upsert_rows_to_csv(
        out_dir / "application_proof_current.csv",
        app_rows,
        key_cols=["company_name"],
    )
    n_phip, added_phip = _upsert_rows_to_csv(
        out_dir / "phip_current.csv",
        phip_rows,
        key_cols=["company_name"],
    )
    return n_app, n_phip, added_app, added_phip


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
