"""
Single entrypoint: fetch NLR, update listings + cornerstone long CSV, digest + email.

Each run:
1. Read existing listings from Google Sheet (or CSV when sheets are off).
2. Download latest Main Board NLR and ingest **only** rows not already in that store; update cornerstone long.
3. Pull HKEX Application Proof + PHIP JSON feeds and upsert **only** new rows vs Sheet/CSV (full active list, not a single calendar day).
4. Pull CSRC 全流通 approvals and upsert **only** new rows.
5. Email a digest **only when** at least one section had new rows; the body summarizes **those** new rows only.

Usage (from hkex/):
    python hkex_pipeline.py
    python hkex_pipeline.py --no-fetch-nlr
    python hkex_pipeline.py --no-email

Programmatic:
    from hkex_pipeline import run_hkex_daily
    run_hkex_daily()
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app_proof_today import (
    build_application_proof_markdown_from_rows,
    build_phip_markdown_from_rows,
    persist_all_active_app_docs,
)
from csrc_approval import (
    CSRC_APPROVAL_URL,
    build_csrc_new_rows_markdown,
    fetch_csrc_approvals,
    upsert_csrc_approvals,
)
from google_sheets_store import hkex_sheets_enabled
from listing_summary import (
    ListingEmailConfig,
    build_listing_summary_markdown,
    build_listing_summary_markdown_for_codes,
    send_listing_summary_email,
)
from nlr_listings import DEFAULT_LISTINGS_CSV, DEFAULT_LONG_CSV, DEFAULT_NLR, NlrIngestResult, ingest_new_listings_from_nlr

_HKT = ZoneInfo("Asia/Hong_Kong")

_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SUMMARY_MD = _SCRIPT_DIR / "listing_summary.md"


def _parse_date(s: str) -> date:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Invalid date {s!r}; use YYYY-MM-DD or DD/MM/YYYY")


@dataclass
class HkexDailyResult:
    ingest: NlrIngestResult
    summary_markdown: str
    listings_summarized: int
    application_proof_rows: int
    csrc_new_rows: int
    summary_path: Path | None
    email_sent: bool


def run_hkex_daily(
    *,
    fetch_nlr: bool = True,
    nlr_year: int = 2026,
    nlr_path: Path | None = None,
    listings_csv: Path | None = None,
    long_csv: Path | None = None,
    data_dir: Path | None = None,
    fetch_timeout: int = 120,
    headed: bool = False,
    max_load_more: int = 200,
    skip_llm_cornerstone: bool = False,
    summary_date: date | None = None,
    save_summary_md: bool = True,
    summary_md_path: Path | None = None,
    send_email: bool = True,
    email_config: ListingEmailConfig | None = None,
    include_application_proof: bool = True,
) -> HkexDailyResult:
    """
    Run the full HKEX listings pipeline.
    ``summary_date`` (if set) is used only for the markdown filename line and email subject; data is not filtered by it.
    When ``save_summary_md`` is True, writes markdown to ``summary_md_path`` or ``listing_summary.md`` in ``hkex/``.
    """
    listings_csv = (listings_csv or DEFAULT_LISTINGS_CSV).resolve()
    long_csv = (long_csv or DEFAULT_LONG_CSV).resolve()
    nlr_path = (nlr_path or DEFAULT_NLR).resolve()
    listing_day = summary_date if summary_date is not None else datetime.now(_HKT).date()

    ingest = ingest_new_listings_from_nlr(
        nlr_path=nlr_path,
        listings_csv=listings_csv,
        long_csv=long_csv,
        data_dir=data_dir,
        fetch_nlr=fetch_nlr,
        nlr_year=nlr_year,
        fetch_timeout=fetch_timeout,
        headed=headed,
        max_load_more=max_load_more,
        skip_llm=skip_llm_cornerstone,
        refresh_long=True,
    )

    n_list = 0
    if ingest.new_stock_codes:
        listings_md, n_list = build_listing_summary_markdown_for_codes(
            ingest.new_stock_codes, listings_csv, document_heading=False
        )
        listings_md = "### New listings (new vs sheet/CSV)\n\n" + listings_md.strip()
    else:
        listings_md = "### New listings (new vs sheet/CSV)\n\n_No new NLR listings this run._"

    n_app = 0
    n_phip = 0
    app_proof_md = "_Omitted (include_application_proof=False)._"
    phip_md = ""
    if include_application_proof:
        out_dir = _SCRIPT_DIR.resolve()
        added_app, added_phip, new_app_rows, new_phip_rows = persist_all_active_app_docs(out_dir)
        app_dest = "Google Sheets (application_proof / phip)" if hkex_sheets_enabled() else str(out_dir)
        print(f"[APP DOC] added app={added_app}, phip={added_phip} -> {app_dest}", file=sys.stderr)
        app_proof_md, n_app = build_application_proof_markdown_from_rows(new_app_rows)
        phip_md, n_phip = build_phip_markdown_from_rows(new_phip_rows)
        print(f"[APP PROOF] new rows this run: {n_app}", file=sys.stderr)
        print(f"[PHIP] new rows this run: {n_phip}", file=sys.stderr)

    csrc_csv = _SCRIPT_DIR / "csrc_approval.csv"
    # CSRC list page is JS-populated; fetch_csrc_approvals uses /searchList JSON (see csrc_approval.py).
    csrc_rows = fetch_csrc_approvals(CSRC_APPROVAL_URL, timeout=fetch_timeout)
    csrc_result = upsert_csrc_approvals(csrc_csv, csrc_rows)
    csrc_dest = "Google Sheet (csrc)" if hkex_sheets_enabled() else str(csrc_csv)
    print(f"[CSRC] fetched={len(csrc_rows)}, added={len(csrc_result.added_rows)} -> {csrc_dest}", file=sys.stderr)

    head = f"# HKEX pipeline digest — {listing_day.isoformat()}\n\n"
    if include_application_proof:
        head += "_Only rows newly added this run (vs Google Sheet / CSV)._"
    else:
        head += "_New NLR listings and CSRC rows only (Application Proof / PHIP fetch skipped)._"
    head += "\n\n---\n\n## New Listings\n\n" + listings_md.strip() + "\n"
    if include_application_proof:
        head += (
            "\n\n---\n\n## New Application Proof\n\n"
            + app_proof_md.strip()
            + "\n\n---\n\n## New PHIP\n\n"
            + phip_md.strip()
            + "\n"
        )
    head += (
        "\n\n---\n\n## CSRC Approval\n\n"
        + build_csrc_new_rows_markdown(csrc_result.added_rows).strip()
        + "\n"
    )
    digest = head

    out_path: Path | None = None
    if save_summary_md:
        out_path = (summary_md_path or DEFAULT_SUMMARY_MD).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(digest, encoding="utf-8")
        print(f"[SUMMARY] Wrote {out_path}", file=sys.stderr)

    has_news = bool(ingest.new_stock_codes) or n_app > 0 or n_phip > 0 or len(csrc_result.added_rows) > 0
    email_sent = False
    if send_email:
        if not has_news:
            print("[EMAIL] skipped — no new rows in any section", file=sys.stderr)
        else:
            cfg = email_config or ListingEmailConfig()
            email_sent = send_listing_summary_email(digest, listing_day, cfg)
            if not email_sent:
                raise RuntimeError("Mailjet send_email returned False (check API keys and logs).")

    return HkexDailyResult(
        ingest=ingest,
        summary_markdown=digest,
        listings_summarized=n_list,
        application_proof_rows=n_app + n_phip,
        csrc_new_rows=len(csrc_result.added_rows),
        summary_path=out_path,
        email_sent=email_sent,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="HKEX NLR ingest + cornerstone long + daily summary email.")
    parser.add_argument("--no-fetch-nlr", action="store_true", help="Use existing NLR xlsx at default path")
    parser.add_argument("--nlr-year", type=int, default=2026)
    parser.add_argument("--nlr", type=Path, default=None, help="NLR xlsx path")
    parser.add_argument("--csv", type=Path, default=None, help="hkex_listings_companies.csv")
    parser.add_argument("--long", type=Path, default=None, help="cornerstone_investor_listings_long.csv")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--max-load-more", type=int, default=200)
    parser.add_argument("--skip-llm-cornerstone", action="store_true")
    parser.add_argument(
        "--summary-date",
        type=_parse_date,
        default=None,
        help="Label date for digest file + email subject only (default: today Asia/Hong_Kong)",
    )
    parser.add_argument("--no-summary-file", action="store_true", help="Do not write listing_summary.md")
    parser.add_argument("--no-app-proof", action="store_true", help="Skip HKEX Application Proof / PHIP fetch and sheet upsert")
    parser.add_argument("--no-email", action="store_true")
    parser.add_argument("--email-to", default="davidlau512@gmail.com")
    args = parser.parse_args()

    try:
        run_hkex_daily(
            fetch_nlr=not args.no_fetch_nlr,
            nlr_year=args.nlr_year,
            nlr_path=args.nlr,
            listings_csv=args.csv,
            long_csv=args.long,
            data_dir=args.data_dir,
            headed=args.headed,
            max_load_more=args.max_load_more,
            skip_llm_cornerstone=args.skip_llm_cornerstone,
            summary_date=args.summary_date,
            save_summary_md=not args.no_summary_file,
            summary_md_path=DEFAULT_SUMMARY_MD,
            send_email=not args.no_email,
            email_config=ListingEmailConfig(to_email=args.email_to),
            include_application_proof=not args.no_app_proof,
        )
    except Exception as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
