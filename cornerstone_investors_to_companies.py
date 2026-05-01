"""
Build investor -> listings from hkex_listings_companies.csv.

Parses cornerstone_investor cells (semicolon-separated names, same as extract pipeline).
Skips empty cells and literal "none".

Usage:
    python cornerstone_investors_to_companies.py
    python cornerstone_investors_to_companies.py --csv hkex_listings_companies.csv
    python cornerstone_investors_to_companies.py --long cornerstone_investor_listings_long.csv
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path

import pandas as pd

_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_LISTINGS = _SCRIPT_DIR / "hkex_listings_companies.csv"
DEFAULT_OUT = _SCRIPT_DIR / "cornerstone_investor_to_listings.csv"
DEFAULT_LONG = _SCRIPT_DIR / "cornerstone_investor_listings_long.csv"


def _split_cornerstone_cell(cell: str) -> list[str]:
    raw = str(cell).strip()
    if not raw or raw.lower() == "none":
        return []
    parts = re.split(r"\s*;\s*", raw)
    return [p.strip() for p in parts if p.strip()]


def _invert_cornerstone_from_df(df: pd.DataFrame) -> tuple[dict[str, set[str]], dict[str, str]]:
    needed = {"stock_code", "company_name", "cornerstone_investors"}
    missing = needed - set(df.columns)
    if missing:
        raise ValueError(f"CSV missing columns: {sorted(missing)}")

    inv_to_codes: dict[str, set[str]] = defaultdict(set)
    code_to_name: dict[str, str] = {}

    for _, row in df.iterrows():
        code = str(row["stock_code"]).strip().zfill(5)
        name = str(row["company_name"]).strip()
        if not code or not code.isdigit():
            continue
        code_to_name[code] = name
        for inv in _split_cornerstone_cell(str(row["cornerstone_investors"])):
            inv_to_codes[inv].add(code)

    return inv_to_codes, code_to_name


def rebuild_cornerstone_long_csv(
    listings_csv: Path | None = None,
    long_csv: Path | None = None,
) -> int:
    """
    Write cornerstone_investor_listings_long.csv from listings CSV.
    Returns number of long rows. Writes header-only file if there are no cornerstone names.
    """
    listings_csv = (listings_csv or DEFAULT_LISTINGS).resolve()
    long_csv = (long_csv or DEFAULT_LONG).resolve()

    if not listings_csv.exists():
        raise FileNotFoundError(f"CSV not found: {listings_csv}")

    df = pd.read_csv(listings_csv, dtype=str).fillna("")
    inv_to_codes, code_to_name = _invert_cornerstone_from_df(df)

    long_rows = []
    for inv in sorted(inv_to_codes.keys(), key=str.casefold):
        for c in sorted(inv_to_codes[inv]):
            long_rows.append(
                {
                    "cornerstone_investor": inv,
                    "stock_code": c,
                    "company_name": code_to_name.get(c, ""),
                }
            )

    long_df = pd.DataFrame(long_rows)
    if long_df.empty:
        long_df = pd.DataFrame(columns=["cornerstone_investor", "stock_code", "company_name"])

    long_csv.parent.mkdir(parents=True, exist_ok=True)
    long_df.to_csv(long_csv, index=False, encoding="utf-8-sig")
    return len(long_rows)


def rebuild_cornerstone_wide_csv(
    listings_csv: Path | None = None,
    wide_csv: Path | None = None,
) -> int:
    """Write wide investor summary CSV. Returns investor row count."""
    listings_csv = (listings_csv or DEFAULT_LISTINGS).resolve()
    wide_csv = (wide_csv or DEFAULT_OUT).resolve()

    df = pd.read_csv(listings_csv, dtype=str).fillna("")
    inv_to_codes, code_to_name = _invert_cornerstone_from_df(df)

    if not inv_to_codes:
        out_df = pd.DataFrame(
            columns=["cornerstone_investor", "listing_count", "stock_codes", "company_names"]
        )
        wide_csv.parent.mkdir(parents=True, exist_ok=True)
        out_df.to_csv(wide_csv, index=False, encoding="utf-8-sig")
        return 0

    rows = []
    for inv in sorted(inv_to_codes.keys(), key=str.casefold):
        codes = sorted(inv_to_codes[inv])
        rows.append(
            {
                "cornerstone_investor": inv,
                "listing_count": len(codes),
                "stock_codes": "; ".join(codes),
                "company_names": "; ".join(code_to_name.get(c, "") for c in codes),
            }
        )

    out_df = pd.DataFrame(rows)
    wide_csv.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(wide_csv, index=False, encoding="utf-8-sig")
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Invert listings CSV: cornerstone investor -> companies.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_LISTINGS, help="Input listings CSV")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="Wide output: one row per investor, companies aggregated",
    )
    parser.add_argument(
        "--long",
        type=Path,
        default=DEFAULT_LONG,
        metavar="PATH",
        help="Long-format CSV (cornerstone_investor, stock_code, company_name)",
    )
    args = parser.parse_args()

    n_wide = rebuild_cornerstone_wide_csv(args.csv, args.out)
    print(f"Wrote {n_wide} investor row(s) -> {args.out}")

    n_long = rebuild_cornerstone_long_csv(args.csv, args.long)
    print(f"Wrote {n_long} long row(s) -> {args.long}")


if __name__ == "__main__":
    main()
