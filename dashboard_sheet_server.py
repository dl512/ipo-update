"""
Local HTTP server: serves the live dashboard HTML and JSON from Google Sheets (or CSV fallback).

The browser cannot hold your service account key; this process loads hkex/.env and
google_sheets_store, then exposes /api/all for the dashboard to poll.

XPLORE uses two patterns: (1) gspread + JSON key to **write**; (2) public
``/export?format=csv&gid=`` URLs to **read** in apps. This server is pattern (1) for **private**
sheets. For **published** (link-viewable) sheets without keys, use
``streamlit_publish_dashboard.py`` + ``publish_sheet_loader.py``.

Usage (from hkex/):
    pip install -r requirements.txt
    python dashboard_sheet_server.py

Open http://127.0.0.1:8787/ in your browser. Data refreshes on an interval set in the page.

Options:
    python dashboard_sheet_server.py --port 9000
    python dashboard_sheet_server.py --host 0.0.0.0   # LAN (less safe)
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd

from env_loader import hkex_dir, load_hkex_dotenv
from google_sheets_store import hkex_sheets_enabled, read_sheet_df

_SCRIPT_DIR = Path(__file__).resolve().parent
_LIVE_HTML = _SCRIPT_DIR / "hkex_dashboard_live.html"

_TAB_ORDER: list[tuple[str, str, str, Path]] = [
    ("application_proof", "Application proof", "Sheet1", _SCRIPT_DIR / "application_proof_current.csv"),
    ("phip", "PHIP", "Sheet2", _SCRIPT_DIR / "phip_current.csv"),
    ("listings", "HKEX listings", "Sheet3", _SCRIPT_DIR / "hkex_listings_companies.csv"),
    ("long", "Cornerstone (long)", "Sheet4", _SCRIPT_DIR / "cornerstone_investor_listings_long.csv"),
    ("csrc", "CSRC approvals", "Sheet5", _SCRIPT_DIR / "csrc_approval.csv"),
]


def _df_payload(df: pd.DataFrame) -> dict[str, Any]:
    df = df.fillna("")
    cols = [str(c) for c in df.columns.tolist()]
    rows = df.astype(str).to_dict(orient="records") if not df.empty else []
    return {"columns": cols, "rows": rows, "row_count": len(rows)}


def _load_kind(kind: str) -> dict[str, Any]:
    csv_path = next((t[3] for t in _TAB_ORDER if t[0] == kind), None)
    if csv_path is None:
        raise KeyError(kind)
    if hkex_sheets_enabled():
        try:
            return _df_payload(read_sheet_df(kind))
        except Exception as e:
            return {"error": str(e), "columns": [], "rows": [], "row_count": 0}
    if csv_path.is_file():
        return _df_payload(pd.read_csv(csv_path, dtype=str, keep_default_na=False).fillna(""))
    return {"error": "file not found", "columns": [], "rows": [], "row_count": 0}


def _all_payload() -> dict[str, Any]:
    load_hkex_dotenv()
    out: dict[str, Any] = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "source": "google_sheets" if hkex_sheets_enabled() else "local_csv",
        "tabs": {},
    }
    for kind, label, sheet_tab, _ in _TAB_ORDER:
        block = _load_kind(kind)
        block["label"] = f"{label} · {sheet_tab}"
        block["kind"] = kind
        out["tabs"][kind] = block
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "HkexDashboard/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {args[0]}")

    def _send(self, code: int, body: bytes, content_type: str, cors: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path in ("/", "/hkex_dashboard_live.html"):
            if not _LIVE_HTML.is_file():
                self._send(HTTPStatus.NOT_FOUND, b"hkex_dashboard_live.html missing", "text/plain; charset=utf-8")
                return
            self._send(HTTPStatus.OK, _LIVE_HTML.read_bytes(), "text/html; charset=utf-8")
            return

        if path == "/api/health":
            load_hkex_dotenv()
            payload = {
                "ok": True,
                "sheets_enabled": hkex_sheets_enabled(),
                "spreadsheet_configured": True,
            }
            self._send(
                HTTPStatus.OK,
                json.dumps(payload).encode("utf-8"),
                "application/json; charset=utf-8",
                cors=True,
            )
            return

        if path == "/api/all":
            try:
                payload = _all_payload()
                self._send(
                    HTTPStatus.OK,
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                    cors=True,
                )
            except Exception as e:
                self._send(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    json.dumps({"ok": False, "error": str(e)}).encode("utf-8"),
                    "application/json; charset=utf-8",
                    cors=True,
                )
            return

        if path.startswith("/api/data/"):
            kind = path.removeprefix("/api/data/").strip("/")
            try:
                block = _load_kind(kind)
                block["kind"] = kind
                block["label"] = next((f"{a} · {b}" for a, b, c, _ in _TAB_ORDER if a == kind), kind)
                self._send(
                    HTTPStatus.OK,
                    json.dumps(block, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                    cors=True,
                )
            except KeyError:
                self._send(HTTPStatus.NOT_FOUND, b'{"error":"unknown kind"}', "application/json; charset=utf-8", cors=True)
            return

        self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")


def main() -> None:
    load_hkex_dotenv()
    parser = argparse.ArgumentParser(description="HKEX live dashboard + Google Sheets API server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving live dashboard at http://{args.host}:{args.port}/")
    print("  API: GET /api/all  |  GET /api/health")
    if not hkex_sheets_enabled():
        print("  [note] HKEX_USE_GOOGLE_SHEETS is off — using CSV files on each refresh.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
