"""
One-off: repair malformed CSRC source_url values in csrc_approval.csv or Google Sheet.

Wrong (legacy crawl): https://www.csrc.gov.cn//www.csrc.gov.cn/csrc/.../content.shtml
Correct:              https://www.csrc.gov.cn/csrc/.../content.shtml

Usage (from hkex/):
    python fix_csrc_source_urls.py
    python fix_csrc_source_urls.py --csv /path/to/csrc_approval.csv
    python fix_csrc_source_urls.py --dry-run
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from csrc_approval import _standardize_csrc_columns, normalize_csrc_source_url
from google_sheets_store import hkex_sheets_enabled, read_sheet_df, write_sheet_df


def _load_csrc_table(csv_path: Path) -> pd.DataFrame:
    if hkex_sheets_enabled():
        return _standardize_csrc_columns(read_sheet_df("csrc").fillna(""))
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")
    return _standardize_csrc_columns(pd.read_csv(csv_path, dtype=str).fillna(""))


def _save_csrc_csv(csv_path: Path, df: pd.DataFrame) -> None:
    cols = ["company_name", "title", "announcement_date", "source_url"]
    out = df[cols].astype(str)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(csv_path, index=False, encoding="utf-8-sig")


def _save_csrc_sheet(df: pd.DataFrame) -> None:
    cols = ["company_name", "title", "announcement_date", "source_url"]
    write_sheet_df("csrc", df[cols].astype(str))


def fix_csrc_source_urls(csv_path: Path, *, dry_run: bool = False) -> int:
    csv_path = csv_path.resolve()
    targets: list[tuple[str, pd.DataFrame | None]] = []

    if hkex_sheets_enabled():
        targets.append(("Google Sheet (csrc)", _load_csrc_table(csv_path)))
    elif csv_path.exists():
        targets.append((str(csv_path), _load_csrc_table(csv_path)))
    elif not dry_run:
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    # When sheets are on, also repair the local CSV if present (often stale vs sheet).
    if hkex_sheets_enabled() and csv_path.exists():
        targets.append((str(csv_path), _standardize_csrc_columns(pd.read_csv(csv_path, dtype=str).fillna(""))))

    if not targets:
        print("No CSRC rows to fix.")
        return 0

    total_fixed = 0
    for label, df in targets:
        if df is None or df.empty:
            continue
        fixed = 0
        for i in df.index:
            raw = str(df.at[i, "source_url"])
            canon = normalize_csrc_source_url(raw)
            if canon and canon != raw:
                fixed += 1
                if not dry_run:
                    df.at[i, "source_url"] = canon
        total_fixed += fixed
        if dry_run:
            print(f"Would fix {fixed} source_url value(s) in {label}.")
        elif fixed:
            if label == "Google Sheet (csrc)":
                _save_csrc_sheet(df)
            else:
                _save_csrc_csv(Path(label), df)
            print(f"Fixed {fixed} source_url value(s) in {label}.")
        else:
            print(f"No changes needed in {label}.")

    return total_fixed


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair malformed CSRC source_url values.")
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="csrc_approval.csv path (default: next to this script)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    args = parser.parse_args()

    out = (args.csv or Path(__file__).resolve().parent / "csrc_approval.csv").resolve()
    fix_csrc_source_urls(out, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
