"""
NLR workbook download, parsing, and ingest into hkex_listings_companies.csv (with cornerstone fetch).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests

from cornerstone_investors_to_companies import rebuild_cornerstone_long_csv
from google_sheets_store import hkex_sheets_enabled, listings_source_ready, read_listings_table, write_sheet_df
from hkex_cornerstone_from_stock import fetch_cornerstone_from_hkex

_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_NLR = _SCRIPT_DIR / "NLR2026_Eng.xlsx"
DEFAULT_LISTINGS_CSV = _SCRIPT_DIR / "hkex_listings_companies.csv"
DEFAULT_LONG_CSV = _SCRIPT_DIR / "cornerstone_investor_listings_long.csv"
DEFAULT_DATA_DIR = _SCRIPT_DIR / "data"

MAIN_BOARD_NLR_PAGE = (
    "https://www2.hkexnews.hk/New-Listings/New-Listing-Information/Main-Board?sc_lang=en"
)
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

LISTING_COLS = [
    "stock_code",
    "company_name",
    "date_of_listing",
    "sponsors",
    "gross_proceeds_hkd",
    "cornerstone_investors",
    "cornerstone_status",
]


def _clean_text(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value).replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\s+", " ", s.replace("\n", " ")).strip()
    return s.strip('"')


def _format_listing_date(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return ""
    return ts.strftime("%d/%m/%Y")


def _normalize_stock_code(value: object) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip().strip('"')
    if s in {"", '"'}:
        return None
    if s.replace(".", "").isdigit():
        s = str(int(float(s)))
    if not s.isdigit():
        return None
    return s.zfill(5)


def parse_nlr_dataframe(df: pd.DataFrame) -> list[dict[str, str | int]]:
    """Rows from NLR sheet (header in row 1, data from row 2)."""
    out: list[dict[str, str | int]] = []
    i = 2
    while i < len(df):
        row = df.iloc[i]
        tag_a = row[10] if len(row) > 10 else None
        stock = _normalize_stock_code(row[1])
        idx = row[0]
        if stock is None or pd.isna(idx):
            i += 1
            continue
        try:
            float(idx)
        except (TypeError, ValueError):
            i += 1
            continue
        if str(tag_a).strip().lower() != "(a)":
            i += 1
            continue
        if i + 1 >= len(df):
            break
        row_b = df.iloc[i + 1]
        tag_b = row_b[10] if len(row_b) > 10 else None
        if str(tag_b).strip().lower() != "(b)":
            i += 1
            continue
        try:
            fa = float(row[8])
            fb = float(row_b[8])
        except (TypeError, ValueError):
            i += 1
            continue
        gross = int(round(fa + fb))

        out.append(
            {
                "stock_code": stock,
                "company_name": _clean_text(row[2]),
                "date_of_listing": _format_listing_date(row[4]),
                "sponsors": _clean_text(row[5]),
                "gross_proceeds_hkd": gross,
            }
        )
        i += 2
    return out


def download_main_board_nlr_eng(year: int, dest: Path, *, timeout: int = 120) -> Path:
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fname = f"NLR{year}_Eng.xlsx"
    r = requests.get(MAIN_BOARD_NLR_PAGE, headers=_HTTP_HEADERS, timeout=60)
    r.raise_for_status()
    m = re.search(
        r'href="(/-/media/HKEXnews/Homepage/New-Listings/New-Listing-Information/New-Listing-Report/Main/'
        + re.escape(fname)
        + r')"',
        r.text,
    )
    if not m:
        raise RuntimeError(
            f"Could not find {fname} link on {MAIN_BOARD_NLR_PAGE} (HKEX may have changed the page)."
        )
    xlsx_url = urljoin(MAIN_BOARD_NLR_PAGE, m.group(1))
    r2 = requests.get(xlsx_url, headers=_HTTP_HEADERS, timeout=timeout, stream=True)
    r2.raise_for_status()
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        with part.open("wb") as f:
            for chunk in r2.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
        part.replace(dest)
    except Exception:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return dest


def write_listings_csv(df: pd.DataFrame, path: Path) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f"{path.stem}.writing{path.suffix}")
    try:
        df.to_csv(staging, index=False, encoding="utf-8-sig")
    except OSError as e:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(
            f"Could not write staging file {staging}: {e}\n"
            "Check folder permissions or whether another program is locking temp files."
        ) from e
    try:
        staging.replace(path)
    except OSError as e:
        raise RuntimeError(
            f"Could not update {path}: {e}\n\n"
            "This usually means the CSV is open in Excel or another editor (Windows locks the file).\n"
            f"Close it, then run again. Your latest merged table is in:\n  {staging}\n"
            f"You can copy/rename that file over {path.name} after closing the lock.\n"
        ) from e


def existing_stock_codes(df: pd.DataFrame) -> set[str]:
    out: set[str] = set()
    for c in df["stock_code"].tolist():
        s = str(c).strip()
        if not s:
            continue
        if s.replace(".", "").isdigit():
            s = str(int(float(s)))
        if s.isdigit():
            out.add(s.zfill(5))
    return out


@dataclass
class NlrIngestResult:
    parsed_total: int
    new_added: int
    cornerstone_ok: int
    cornerstone_err: int
    long_csv_refreshed: bool
    new_stock_codes: list[str]


def ingest_new_listings_from_nlr(
    *,
    nlr_path: Path | None = None,
    listings_csv: Path | None = None,
    long_csv: Path | None = None,
    data_dir: Path | None = None,
    fetch_nlr: bool = True,
    nlr_year: int = 2026,
    fetch_timeout: int = 120,
    headed: bool = False,
    max_load_more: int = 200,
    skip_llm: bool = False,
    refresh_long: bool = True,
) -> NlrIngestResult:
    """
    Download NLR (optional), append rows not yet in listings CSV, fetch cornerstone per new row,
    optionally rebuild cornerstone_investor_listings_long.csv.
    """
    nlr_path = (nlr_path or DEFAULT_NLR).resolve()
    listings_csv = (listings_csv or DEFAULT_LISTINGS_CSV).resolve()
    long_csv = (long_csv or DEFAULT_LONG_CSV).resolve()
    data_dir = (data_dir or DEFAULT_DATA_DIR).resolve()

    if fetch_nlr:
        print(f"[FETCH] Main Board NLR {nlr_year}_Eng.xlsx -> {nlr_path}", file=sys.stderr)
        download_main_board_nlr_eng(nlr_year, nlr_path, timeout=fetch_timeout)
        print(f"[FETCH] OK ({nlr_path.stat().st_size} bytes)", file=sys.stderr)
    elif not nlr_path.exists():
        raise FileNotFoundError(f"NLR file not found: {nlr_path}")

    if not listings_source_ready(listings_csv):
        raise FileNotFoundError(f"Listings CSV not found: {listings_csv}")

    raw = pd.read_excel(nlr_path, sheet_name="NLR", header=None)
    parsed = parse_nlr_dataframe(raw)
    if not parsed:
        raise ValueError("No listing rows parsed from NLR sheet.")

    existing = read_listings_table(listings_csv)
    for col in LISTING_COLS:
        if col not in existing.columns:
            existing[col] = ""
    existing = existing.fillna("")

    codes_in_file = existing_stock_codes(existing)
    to_add: list[dict[str, str | int]] = []
    for rec in parsed:
        code = str(rec["stock_code"]).zfill(5)
        if code in codes_in_file:
            continue
        to_add.append(rec)
        codes_in_file.add(code)

    print(
        f"NLR: {len(parsed)} listing(s) parsed; "
        f"new to ingest: {len(to_add)}, already in CSV: {len(parsed) - len(to_add)}",
        file=sys.stderr,
    )

    cornerstone_ok = 0
    cornerstone_err = 0
    long_refreshed = False

    if not to_add:
        print("No new listings to add.", file=sys.stderr)
        if refresh_long:
            rebuild_cornerstone_long_csv(listings_csv, long_csv)
            long_refreshed = True
            _long_dest = "Google Sheet (long)" if hkex_sheets_enabled() else str(long_csv)
            print(f"Refreshed: {_long_dest}", file=sys.stderr)
        return NlrIngestResult(
            parsed_total=len(parsed),
            new_added=0,
            cornerstone_ok=0,
            cornerstone_err=0,
            long_csv_refreshed=long_refreshed,
            new_stock_codes=[],
        )

    new_frames: list[pd.DataFrame] = []
    for rec in to_add:
        stock = str(rec["stock_code"]).strip().zfill(5)
        inv_cell = ""
        status = ""

        if skip_llm:
            inv_cell = ""
            status = "skip_llm"
        else:
            print(f"[RUN] cornerstone for {stock} {rec.get('company_name', '')}", file=sys.stderr)
            try:
                out = fetch_cornerstone_from_hkex(
                    stock,
                    headed=headed,
                    max_load_more=max_load_more,
                    data_dir=data_dir,
                    skip_llm=False,
                    verbose=False,
                )
            except Exception as exc:
                cornerstone_err += 1
                status = f"error: {exc}"
                print(f"[ERR] {stock}: {exc}", file=sys.stderr)
            else:
                cell = out.get("cornerstone_cell")
                if cell is None:
                    cornerstone_err += 1
                    status = "error: extractor returned no cornerstone_cell"
                    print(f"[ERR] {stock}: no cornerstone_cell", file=sys.stderr)
                else:
                    inv_cell = cell
                    status = "done"
                    cornerstone_ok += 1
                    print(f"[OK] {stock}", file=sys.stderr)

        row = {
            "stock_code": stock,
            "company_name": str(rec["company_name"]),
            "date_of_listing": str(rec["date_of_listing"]),
            "sponsors": str(rec["sponsors"]),
            "gross_proceeds_hkd": str(rec["gross_proceeds_hkd"]),
            "cornerstone_investors": inv_cell,
            "cornerstone_status": status,
        }
        new_frames.append(pd.DataFrame([row]))

        append_block = pd.concat(new_frames, ignore_index=True)
        merged = pd.concat([append_block, existing[LISTING_COLS]], ignore_index=True)
        if hkex_sheets_enabled():
            write_sheet_df("listings", merged[LISTING_COLS])
        else:
            write_listings_csv(merged, listings_csv)

    dest = "Google Sheet (listings)" if hkex_sheets_enabled() else str(listings_csv)
    print(
        f"Wrote {len(to_add)} new row(s) to top of {dest} "
        f"(cornerstone ok={cornerstone_ok}, errors={cornerstone_err})",
        file=sys.stderr,
    )

    if refresh_long:
        rebuild_cornerstone_long_csv(listings_csv, long_csv)
        long_refreshed = True
        _long_dest = "Google Sheet (long)" if hkex_sheets_enabled() else str(long_csv)
        print(f"Refreshed: {_long_dest}", file=sys.stderr)

    return NlrIngestResult(
        parsed_total=len(parsed),
        new_added=len(to_add),
        cornerstone_ok=cornerstone_ok,
        cornerstone_err=cornerstone_err,
        long_csv_refreshed=long_refreshed,
        new_stock_codes=[str(rec["stock_code"]).strip().zfill(5) for rec in to_add],
    )
