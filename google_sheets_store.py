"""
Google Sheets backend for HKEX pipeline tables (optional).

Enable with ``HKEX_USE_GOOGLE_SHEETS=1`` in ``hkex/.env`` plus the same service-account JSON
as XPLORE (``global-headlines-474905-9494f258e0a5.json``): use
``HKEX_GOOGLE_APPLICATION_CREDENTIALS``, or ``GOOGLE_APPLICATION_CREDENTIALS`` /
``GOOGLE_SERVICE_ACCOUNT_JSON`` (path to the file), or place the JSON under ``hkex/``.
Auth matches ``xplore_automation.py`` (explicit Spreadsheets scope + ``gspread.authorize``).

Worksheet titles default to ``Sheet1``..``Sheet5``; override with ``HKEX_SHEET_*`` env vars.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import gspread
import pandas as pd
from google.oauth2 import service_account as gcp_service_account

from env_loader import hkex_dir, load_hkex_dotenv

_SPREADSHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

_DEFAULT_SPREADSHEET_ID = "1AbTN_jSiXik1sKXGqa1Vh_VRqsX0b4yUs_oYLrPpXIo"
_DEFAULT_CREDS_NAME = "global-headlines-474905-9494f258e0a5.json"

# Logical table -> env var for worksheet title (defaults match typical Sheet1..Sheet5 layout)
_SHEET_ENV_KEYS: dict[str, str] = {
    "application_proof": "HKEX_SHEET_APPLICATION_PROOF",
    "phip": "HKEX_SHEET_PHIP",
    "listings": "HKEX_SHEET_LISTINGS",
    "long": "HKEX_SHEET_LONG",
    "csrc": "HKEX_SHEET_CSRC",
}
_DEFAULT_TITLES: dict[str, str] = {
    "application_proof": "Sheet1",
    "phip": "Sheet2",
    "listings": "Sheet3",
    "long": "Sheet4",
    "csrc": "Sheet5",
}

_gc: gspread.Client | None = None


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def hkex_sheets_enabled() -> bool:
    load_hkex_dotenv()
    return _truthy(os.getenv("HKEX_USE_GOOGLE_SHEETS"))


def get_spreadsheet_id() -> str:
    load_hkex_dotenv()
    sid = (
        os.getenv("HKEX_GOOGLE_SHEETS_ID")
        or os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
        or _DEFAULT_SPREADSHEET_ID
    ).strip()
    if not sid:
        raise ValueError("Set HKEX_GOOGLE_SHEETS_ID in hkex/.env (or rely on built-in default).")
    return sid


def get_credentials_path() -> str:
    """Resolve path to the same key file XPLORE uses for Sheets (see ``xplore_automation.py``)."""
    load_hkex_dotenv()
    for key in (
        "HKEX_GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
    ):
        p = (os.getenv(key) or "").strip()
        if p and os.path.isfile(p):
            return str(Path(p).resolve())
    base = Path(hkex_dir())
    for candidate in (
        base / _DEFAULT_CREDS_NAME,
        base.parent / _DEFAULT_CREDS_NAME,
        Path.home() / "Desktop" / "python" / "xplore" / _DEFAULT_CREDS_NAME,
    ):
        if candidate.is_file():
            return str(candidate.resolve())
    raise FileNotFoundError(
        f"Google service account JSON not found. Set GOOGLE_APPLICATION_CREDENTIALS (as in XPLORE) or "
        f"HKEX_GOOGLE_APPLICATION_CREDENTIALS, or place {_DEFAULT_CREDS_NAME} in {base}."
    )


def worksheet_title(kind: str) -> str:
    load_hkex_dotenv()
    env_key = _SHEET_ENV_KEYS.get(kind)
    if not env_key:
        raise ValueError(f"Unknown sheet kind: {kind}")
    return (os.getenv(env_key) or _DEFAULT_TITLES[kind]).strip()


def _client() -> gspread.Client:
    global _gc
    if _gc is None:
        creds = gcp_service_account.Credentials.from_service_account_file(
            get_credentials_path(),
            scopes=[_SPREADSHEETS_SCOPE],
        )
        _gc = gspread.authorize(creds)
    return _gc


def get_spreadsheet() -> gspread.Spreadsheet:
    return _client().open_by_key(get_spreadsheet_id())


def get_worksheet(kind: str) -> gspread.Worksheet:
    title = worksheet_title(kind)
    try:
        return get_spreadsheet().worksheet(title)
    except gspread.WorksheetNotFound as e:
        raise RuntimeError(
            f"Missing tab {title!r} (kind={kind!r}). Create it in the spreadsheet or set "
            f"{_SHEET_ENV_KEYS[kind]} to the exact tab name."
        ) from e


def read_sheet_df(kind: str) -> pd.DataFrame:
    ws = get_worksheet(kind)
    records = ws.get_all_records()
    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records)


def write_sheet_df(kind: str, df: pd.DataFrame) -> None:
    ws = get_worksheet(kind)
    ws.clear()
    if df.empty:
        if len(df.columns) > 0:
            ws.update([list(df.columns)], value_input_option="USER_ENTERED")
        return
    headers = [str(c) for c in df.columns.tolist()]
    body = df.fillna("").astype(str).values.tolist()
    ws.update([headers] + body, value_input_option="USER_ENTERED")


def read_listings_table(listings_csv: Path) -> pd.DataFrame:
    """Read listings from Google Sheet (listings kind) or from ``listings_csv``."""
    if hkex_sheets_enabled():
        return read_sheet_df("listings").fillna("")
    path = listings_csv.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Listings CSV not found: {path}")
    return pd.read_csv(path, dtype=str).fillna("")


def listings_source_ready(listings_csv: Path) -> bool:
    if hkex_sheets_enabled():
        return True
    return listings_csv.resolve().exists()


def _normalize_upsert_key_part(col: str, raw: Any) -> str:
    """
    Stable string for dedup keys. Handles Sheets float ids, date formats, and company_name whitespace.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    if col == "company_name":
        s = str(raw).strip()
        return " ".join(s.split()) if s else ""
    if col == "applicant_id":
        s = str(raw).strip().replace(",", "")
        if not s:
            return ""
        try:
            return str(int(float(s)))
        except ValueError:
            return s
    if col == "listing_date":
        s = str(raw).strip()
        if not s:
            return ""
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                chunk = s[:10] if fmt == "%Y-%m-%d" and len(s) >= 10 else s
                parsed = datetime.strptime(chunk, fmt)
                return parsed.strftime("%d/%m/%Y")
            except ValueError:
                continue
        return s
    return str(raw).strip()


def upsert_rows_dataframe(
    *,
    csv_path: Path,
    sheet_kind: str | None,
    rows: list[dict[str, Any]],
    key_cols: list[str],
) -> tuple[int, pd.DataFrame, list[dict[str, Any]]]:
    """
    Upsert rows by key tuple (all key_cols must be present on incoming rows).
    Writes to sheet when ``hkex_sheets_enabled()`` and ``sheet_kind`` is set; else CSV.
    Returns (number of new rows appended, final merged DataFrame, list of newly appended row dicts).
    """
    csv_path = csv_path.resolve()
    incoming = pd.DataFrame(rows)
    if incoming.empty:
        if hkex_sheets_enabled() and sheet_kind:
            existing = read_sheet_df(sheet_kind)
            return 0, existing, []
        if not csv_path.exists():
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            incoming.to_csv(csv_path, index=False, encoding="utf-8-sig")
        return 0, incoming, []

    for c in key_cols:
        if c not in incoming.columns:
            incoming[c] = ""
    incoming = incoming.fillna("")

    use_sheet = hkex_sheets_enabled() and sheet_kind is not None
    if use_sheet:
        existing = read_sheet_df(sheet_kind)
    elif csv_path.exists():
        existing = pd.read_csv(csv_path, dtype=str).fillna("")
    else:
        existing = pd.DataFrame(columns=incoming.columns)

    for c in incoming.columns:
        if c not in existing.columns:
            existing[c] = ""
    for c in existing.columns:
        if c not in incoming.columns:
            incoming[c] = ""
    existing = existing[incoming.columns]

    def _row_key(row: pd.Series) -> tuple[str, ...]:
        return tuple(_normalize_upsert_key_part(c, row.get(c)) for c in key_cols)

    existing_keys = {_row_key(r) for _, r in existing.iterrows()}
    new_rows: list[dict[str, Any]] = []
    for _, r in incoming.iterrows():
        k = _row_key(r)
        if k in existing_keys:
            continue
        existing_keys.add(k)
        new_rows.append(r.to_dict())

    if not new_rows:
        return 0, existing, []

    merged = pd.concat([existing, pd.DataFrame(new_rows)], ignore_index=True)
    if use_sheet:
        write_sheet_df(sheet_kind, merged)
    else:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        merged.to_csv(csv_path, index=False, encoding="utf-8-sig")
    return len(new_rows), merged, new_rows
