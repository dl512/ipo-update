"""
HK last close prices via yfinance (``{code}.HK``, unpadded stock code).
"""

from __future__ import annotations

import sys
from typing import Iterable

import pandas as pd
import yfinance as yf

from google_sheets_store import hkex_sheets_enabled, read_listings_table, write_sheet_df
from nlr_listings import LISTING_COLS, write_listings_csv


def hk_yahoo_symbol(stock_code: str) -> str:
    """
    Yahoo Finance HK symbols are 4-digit zero-padded (e.g. ``0668.HK``, ``0068.HK``).
    Fully stripped forms like ``668.HK`` / ``68.HK`` usually 404.
    """
    s = str(stock_code).strip()
    if s.replace(".", "").isdigit():
        s = str(int(float(s)))
    if not s.isdigit():
        raise ValueError(f"stock code must be numeric, got {stock_code!r}")
    return f"{int(s):04d}.HK"


def _format_price(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    if f != f:  # NaN
        return ""
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return f"{f:.4f}".rstrip("0").rstrip(".")


def _parse_price(value: object) -> float | None:
    s = str(value or "").strip().replace(",", "").replace("HK$", "").replace("$", "")
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    if f != f:
        return None
    return f


def pct_change_vs_offer(offer_price: object, last_close: object) -> str:
    """(last_close - offer) / offer as e.g. ``+2.57%`` / ``-5.20%``; empty if not computable."""
    offer = _parse_price(offer_price)
    close = _parse_price(last_close)
    if offer is None or close is None or offer == 0:
        return ""
    pct = (close - offer) / offer * 100.0
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"


def fetch_last_close(stock_code: str) -> str:
    """Last available close for one HK listing; empty string if unavailable."""
    sym = hk_yahoo_symbol(stock_code)
    hist = yf.Ticker(sym).history(period="10d")
    if hist is None or hist.empty or "Close" not in hist.columns:
        return ""
    return _format_price(hist["Close"].iloc[-1])


def fetch_last_closes(stock_codes: Iterable[str], *, period: str = "10d") -> dict[str, str]:
    """
    Batch last closes. Returns map of zero-padded stock_code -> price string.
    Missing / failed symbols are omitted (caller may leave blank).
    """
    code5_list: list[str] = []
    symbols: list[str] = []
    seen: set[str] = set()
    for raw in stock_codes:
        s = str(raw).strip()
        if not s:
            continue
        if s.replace(".", "").isdigit():
            s = str(int(float(s))).zfill(5)
        elif s.isdigit():
            s = s.zfill(5)
        else:
            continue
        if s in seen:
            continue
        seen.add(s)
        code5_list.append(s)
        symbols.append(hk_yahoo_symbol(s))

    out: dict[str, str] = {}
    if not symbols:
        return out

    # One request for many tickers when possible.
    try:
        data = yf.download(
            symbols,
            period=period,
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[yfinance] batch download failed: {exc}", file=sys.stderr)
        data = None

    if data is not None and not data.empty:
        if len(symbols) == 1:
            # Single-ticker frame: columns are OHLCV directly
            sym = symbols[0]
            code = code5_list[0]
            if "Close" in data.columns and len(data["Close"].dropna()):
                out[code] = _format_price(data["Close"].dropna().iloc[-1])
            return out

        for code, sym in zip(code5_list, symbols):
            try:
                if sym in data.columns.get_level_values(0):
                    closes = data[sym]["Close"].dropna()
                    if len(closes):
                        out[code] = _format_price(closes.iloc[-1])
            except Exception:
                continue

    # Fallback per-symbol for any missing
    for code, sym in zip(code5_list, symbols):
        if code in out:
            continue
        try:
            hist = yf.Ticker(sym).history(period=period)
            if hist is not None and not hist.empty and "Close" in hist.columns:
                out[code] = _format_price(hist["Close"].iloc[-1])
        except Exception as exc:  # noqa: BLE001
            print(f"[yfinance] {sym}: {exc}", file=sys.stderr)
    return out


def refresh_listings_last_close(listings_csv, *, period: str = "10d") -> int:
    """
    Update ``last_close_hkd`` and ``pct_vs_offer`` for all listings rows; write Sheet and/or CSV.
    Returns number of rows that received a non-empty close.
    """
    df = read_listings_table(listings_csv).fillna("").astype(str)
    for col in LISTING_COLS:
        if col not in df.columns:
            df[col] = ""

    codes = []
    for raw in df["stock_code"].tolist():
        s = str(raw).strip()
        if s.replace(".", "").isdigit():
            codes.append(str(int(float(s))).zfill(5))
        elif s.isdigit():
            codes.append(s.zfill(5))

    prices = fetch_last_closes(codes, period=period)
    filled = 0
    df["stock_code"] = df["stock_code"].map(
        lambda x: str(int(float(x))).zfill(5)
        if str(x).replace(".", "").isdigit()
        else str(x).strip().zfill(5)
        if str(x).strip().isdigit()
        else str(x).strip()
    )
    for i, row in df.iterrows():
        code = (
            str(row["stock_code"]).strip().zfill(5)
            if str(row["stock_code"]).strip().isdigit()
            else str(row["stock_code"]).strip()
        )
        price = prices.get(code, "")
        offer = row.get("offer_price_hkd", "")
        df.at[i, "last_close_hkd"] = price
        df.at[i, "pct_vs_offer"] = pct_change_vs_offer(offer, price)
        if price:
            filled += 1

    cols = [c for c in LISTING_COLS if c in df.columns]
    extra = [c for c in df.columns if c not in cols]
    out = df[cols + extra]
    if hkex_sheets_enabled():
        write_sheet_df("listings", out)
    write_listings_csv(out, listings_csv)
    return filled
