"""
Single entrypoint: fetch NLR, update listings + cornerstone long CSV, digest + email.

Steps:
1. Download latest Main Board NLR (e.g. NLR2026_Eng.xlsx)
2. Ingest new rows into hkex_listings_companies.csv (with HKEX+LLM cornerstone)
3. Rebuild cornerstone_investor_listings_long.csv
4. Build email body: (a) new listings from CSV for the digest date, (b) Application Proof rows from HKEX app index feed
5. Send combined digest via Mailjet

Digest date defaults to **today in Asia/Hong_Kong** (aligned with HKEX app feed field ``d``).

Usage (from hkex/):
    python hkex_pipeline.py
    python hkex_pipeline.py --no-fetch-nlr
    python hkex_pipeline.py --summary-date 2026-04-29 --no-email

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

from app_proof_today import build_application_proof_markdown_section
from listing_summary import ListingEmailConfig, build_listing_summary_markdown, send_listing_summary_email
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
    ``summary_date`` defaults to today in **Asia/Hong_Kong** (same calendar as HKEX app index feed).
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

    listings_md, n_list = build_listing_summary_markdown(
        listing_day, listings_csv, document_heading=False
    )
    n_app = 0
    app_md = ""
    if include_application_proof:
        app_md, n_app = build_application_proof_markdown_section(listing_day)
        print(f"[APP PROOF] rows for {listing_day.isoformat()}: {n_app}", file=sys.stderr)

    head = (
        f"# HKEX daily digest — {listing_day.isoformat()}\n\n"
        f"_Digest date **{listing_day.isoformat()}**._ "
        f"**Part 1:** `date_of_listing` in `hkex_listings_companies.csv`. "
    )
    if include_application_proof:
        head += (
            f"**Part 2:** HKEX active Main Board feed (field `d`). "
            f"[App index](https://www1.hkexnews.hk/app/appindex.html)._"
        )
    else:
        head += "_"
    head += "\n\n---\n\n## 1) New listings (`hkex_listings_companies.csv`)\n\n" + listings_md.strip() + "\n"
    if include_application_proof:
        head += (
            "\n\n---\n\n## 2) New Application Proof (HKEX Main Board active feed)\n\n"
            + app_md.strip()
            + "\n"
        )
    digest = head

    out_path: Path | None = None
    if save_summary_md:
        out_path = (summary_md_path or DEFAULT_SUMMARY_MD).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(digest, encoding="utf-8")
        print(f"[SUMMARY] Wrote {out_path}", file=sys.stderr)

    email_sent = False
    if send_email:
        cfg = email_config or ListingEmailConfig()
        email_sent = send_listing_summary_email(digest, listing_day, cfg)
        if not email_sent:
            raise RuntimeError("Mailjet send_email returned False (check API keys and logs).")

    return HkexDailyResult(
        ingest=ingest,
        summary_markdown=digest,
        listings_summarized=n_list,
        application_proof_rows=n_app,
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
        help="Digest date: CSV listing date + app feed `d` (default: today Asia/Hong_Kong)",
    )
    parser.add_argument("--no-summary-file", action="store_true", help="Do not write listing_summary.md")
    parser.add_argument("--no-app-proof", action="store_true", help="Omit Application Proof section (Part 2)")
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
