"""
Build markdown summary for listing date(s) from hkex_listings_companies.csv (LLM blurbs + rule-based facts).
Optional Mailjet email via ``mailjet_email`` (loads ``hkex/.env``).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import markdown
import pandas as pd

_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_LISTINGS_CSV = _SCRIPT_DIR / "hkex_listings_companies.csv"

from mailjet_email import send_email as _mailjet_send
from openai_utils import (
    chat_completion_with_fallback,
    extract_json_text_from_llm_response,
    initialize_openai_client,
)


def row_matches_listing_date(cell: str, target: date) -> bool:
    raw = str(cell).strip()
    if not raw:
        return False
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            d = datetime.strptime(raw, fmt).date()
            return d == target
        except ValueError:
            continue
    return False


def _format_currency_hkd(value: str) -> str:
    raw = str(value).strip().replace(",", "")
    if not raw or not re.fullmatch(r"-?\d+", raw):
        return str(value).strip() or "—"
    n = int(raw)
    if n >= 1_000_000_000:
        return f"HK${n / 1_000_000_000:.2f}b ({n:,} HKD)"
    if n >= 1_000_000:
        return f"HK${n / 1_000_000:.1f}m ({n:,} HKD)"
    return f"HK${n:,}"


def _cornerstone_display(cell: str) -> str:
    s = str(cell).strip()
    if not s or s.lower() == "none":
        return "none"
    return s


def _build_llm_prompt(rows: list[dict[str, Any]], listing_day: date) -> str:
    lines: list[str] = [
        f"These Hong Kong-listed issuers have listing date {listing_day.isoformat()} in our dataset.",
        "For EACH company below, write 2–4 sentences: what the company does (products, services, customers, geography when inferable).",
        "Use name hints: “- H Shares” mainland listing; “-W” weighted voting right; “-B” Chapter 18A biotech; “-P” pre-commercial (18C); “- S” secondary listing.",
        "Do not mention sponsors, IPO proceeds, or investors. If the name is too vague to say anything specific, say so in one short sentence.",
        "Return ONLY valid JSON (no markdown fences) with this shape:",
        '{"items":[{"stock_code":"00000","summary":"plain text, no heading"}]}',
        "Include every stock_code exactly once, same codes as below.",
        "",
        "Companies:",
    ]
    for r in rows:
        lines.append(f"- {r['stock_code']}: {r['company_name']}")
    return "\n".join(lines)


def _parse_summary_json(content: str, expected_codes: list[str]) -> dict[str, str]:
    raw = extract_json_text_from_llm_response(content)
    data = json.loads(raw)
    if not isinstance(data, dict) or "items" not in data:
        raise ValueError("Model JSON must be an object with key 'items'.")
    items = data["items"]
    if not isinstance(items, list):
        raise ValueError("'items' must be a JSON array.")
    out: dict[str, str] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        code = str(it.get("stock_code", "")).strip().zfill(5)
        summ = str(it.get("summary", "")).strip()
        if code.isdigit() and summ:
            out[code] = summ
    missing = [c for c in expected_codes if c not in out]
    if missing:
        raise ValueError(f"Model omitted summaries for: {missing}")
    return out


async def _llm_summaries_only(rows: list[dict[str, Any]], listing_day: date) -> dict[str, str]:
    client = initialize_openai_client()
    prompt = _build_llm_prompt(rows, listing_day)
    response = await chat_completion_with_fallback(
        client,
        tier="main",
        messages=[
            {
                "role": "system",
                "content": "You describe listed companies in plain English. Output strict JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )
    choice = response.choices[0].message.content
    if not choice:
        raise RuntimeError("Empty LLM response")
    codes = [r["stock_code"] for r in rows]
    return _parse_summary_json(choice, codes)


def _rule_based_markdown_section(r: dict[str, Any], summary: str) -> str:
    parts: list[str] = [
        f"### {r['stock_code']} — {r['company_name']}",
        "",
        summary.strip(),
        "",
        f"- **Sponsors:** {r['sponsors'] if r['sponsors'] else '—'}",
        f"- **Gross proceeds:** {_format_currency_hkd(r['gross_proceeds_hkd'])}",
        f"- **Cornerstone investors:** {_cornerstone_display(r['cornerstone_investors'])}",
    ]
    st = str(r.get("cornerstone_status", "")).strip()
    if st:
        parts.append(f"- **Cornerstone data status:** {st}")
    parts.append("")
    return "\n".join(parts)


def build_listing_summary_markdown(
    listing_day: date,
    listings_csv: Path | None = None,
    *,
    document_heading: bool = True,
) -> tuple[str, int]:
    """
    Returns (markdown body, number of companies summarized).
    If none listed that day, returns a short note and 0 (no LLM call).

    If ``document_heading`` is False, omits the top ``# HKEX listings summary`` line so the
    block can be embedded under a parent digest (e.g. ``hkex_pipeline``).
    """
    listings_csv = (listings_csv or DEFAULT_LISTINGS_CSV).resolve()
    if not listings_csv.exists():
        raise FileNotFoundError(f"CSV not found: {listings_csv}")

    df = pd.read_csv(listings_csv, dtype=str).fillna("")
    needed = {"stock_code", "company_name", "date_of_listing", "sponsors", "gross_proceeds_hkd"}
    missing = needed - set(df.columns)
    if missing:
        raise ValueError(f"CSV missing columns: {sorted(missing)}")

    mask = df["date_of_listing"].apply(lambda c: row_matches_listing_date(c, listing_day))
    sub = df.loc[mask]
    if sub.empty:
        note = f"_No rows in {listings_csv.name} with date_of_listing = {listing_day.isoformat()}._\n"
        if document_heading:
            body = f"# HKEX listings summary — {listing_day.isoformat()}\n\n{note}"
        else:
            body = note
        return body, 0

    rows: list[dict[str, Any]] = []
    for _, row in sub.iterrows():
        code = str(row["stock_code"]).strip().zfill(5)
        rows.append(
            {
                "stock_code": code,
                "company_name": str(row["company_name"]).strip(),
                "sponsors": str(row["sponsors"]).strip(),
                "gross_proceeds_hkd": str(row["gross_proceeds_hkd"]).strip(),
                "cornerstone_investors": str(row.get("cornerstone_investors", "")).strip(),
                "cornerstone_status": str(row.get("cornerstone_status", "")).strip(),
            }
        )

    by_code = asyncio.run(_llm_summaries_only(rows, listing_day))
    sections = [_rule_based_markdown_section(r, by_code[r["stock_code"]]) for r in rows]
    meta = (
        f"_Source: {listings_csv.name} ({len(rows)} row(s)). Company blurbs from LLM; other fields from CSV._\n\n"
    )
    if document_heading:
        header = f"# HKEX listings summary — {listing_day.isoformat()}\n\n{meta}"
    else:
        header = meta
    return header + "\n".join(sections), len(rows)


@dataclass
class ListingEmailConfig:
    to_email: str = "davidlau512@gmail.com"
    from_email: str = "david@xplorehk.com"
    from_name: str = "HKEX Daily Digest"
    subject_template: str = "HKEX daily digest — {date}"


def send_listing_summary_email(markdown_body: str, listing_day: date, cfg: ListingEmailConfig | None = None) -> bool:
    cfg = cfg or ListingEmailConfig()
    subject = cfg.subject_template.format(date=listing_day.isoformat())
    html_content = markdown.markdown(markdown_body, extensions=["tables"])
    return _mailjet_send(
        cfg.to_email,
        subject,
        html_content,
        from_email=cfg.from_email,
        from_name=cfg.from_name,
    )
