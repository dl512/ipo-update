"""
Backfill ``a_share_listed`` on listings (CSV and/or Google Sheet) via HKEX DI corp search.

For each stock code: search DI listed corporation → if any name row mentions A Shares → true.

Usage (from project dir):
    python backfill_a_share_listed.py
    python backfill_a_share_listed.py --limit 5
    python backfill_a_share_listed.py --codes 06935,02384
    python backfill_a_share_listed.py --dry-run -v
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from di_a_share import _A_SHARE_NAME_RE, _session, fetch_listed_corporation_names
from google_sheets_store import hkex_sheets_enabled, read_listings_table, write_sheet_df
from nlr_listings import DEFAULT_LISTINGS_CSV, LISTING_COLS, write_listings_csv


def _normalize_code(raw: object) -> str | None:
    s = str(raw or "").strip()
    if not s:
        return None
    if s.replace(".", "").isdigit():
        s = str(int(float(s)))
    if not s.isdigit():
        return None
    return s.zfill(5)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill a_share_listed from HKEX DI corp search.")
    parser.add_argument("--csv", type=Path, default=None, help="Listings CSV path (CSV mode / mirror)")
    parser.add_argument(
        "--codes",
        default="",
        help="Comma-separated stock codes only (default: all rows in listings store)",
    )
    parser.add_argument("--limit", type=int, default=0, help="Max codes to process (0 = all)")
    parser.add_argument("--sleep", type=float, default=0.4, help="Seconds between DI requests")
    parser.add_argument("--dry-run", action="store_true", help="Query DI and print; do not write")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    listings_csv = (args.csv or DEFAULT_LISTINGS_CSV).resolve()
    df = read_listings_table(listings_csv).fillna("")
    for col in LISTING_COLS:
        if col not in df.columns:
            df[col] = ""

    if args.codes.strip():
        codes = [c for c in (_normalize_code(x) for x in args.codes.split(",")) if c]
    else:
        codes = []
        seen: set[str] = set()
        for raw in df["stock_code"].tolist():
            c = _normalize_code(raw)
            if c and c not in seen:
                seen.add(c)
                codes.append(c)

    if args.limit and args.limit > 0:
        codes = codes[: args.limit]

    if not codes:
        print("No stock codes to process.", file=sys.stderr)
        raise SystemExit(1)

    print(f"Checking {len(codes)} code(s) via DI corp search ...", file=sys.stderr)
    results: dict[str, str] = {}
    with _session() as session:
        for i, code in enumerate(codes):
            try:
                names = fetch_listed_corporation_names(code, session=session)
                cell = "true" if any(_A_SHARE_NAME_RE.search(n) for n in names) else "false"
            except Exception as exc:
                print(f"[ERR] {code}: {exc}", file=sys.stderr)
                cell = ""
                names = []
            results[code] = cell
            if args.verbose or args.dry_run:
                print(f"{code}\t{cell}\t{names}", file=sys.stderr)
            else:
                print(f"[{i + 1}/{len(codes)}] {code} -> {cell or 'ERR'}", file=sys.stderr)
            if args.sleep > 0 and i + 1 < len(codes):
                time.sleep(args.sleep)

    true_n = sum(1 for v in results.values() if v == "true")
    false_n = sum(1 for v in results.values() if v == "false")
    err_n = sum(1 for v in results.values() if v == "")
    print(f"Done: true={true_n} false={false_n} errors={err_n}", file=sys.stderr)

    if args.dry_run:
        return

    df["stock_code"] = df["stock_code"].map(lambda x: _normalize_code(x) or str(x).strip())
    updated = 0
    for i, row in df.iterrows():
        code = _normalize_code(row["stock_code"])
        if not code or code not in results or results[code] == "":
            continue
        if str(row.get("a_share_listed", "")).strip() != results[code]:
            updated += 1
        df.at[i, "a_share_listed"] = results[code]

    cols = [c for c in LISTING_COLS if c in df.columns]
    extra = [c for c in df.columns if c not in cols]
    out_df = df[cols + extra]

    if hkex_sheets_enabled():
        write_sheet_df("listings", out_df)
        dest = "Google Sheet (listings)"
    else:
        write_listings_csv(out_df, listings_csv)
        dest = str(listings_csv)
    print(f"Wrote a_share_listed for {updated} changed row(s) -> {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
