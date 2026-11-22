"""
AI extractor for HK IPO allotment-result PDFs.

Returns strict JSON with:
- cornerstone_investors
- a_share_listed (bool): already listed on a PRC A-share exchange

Usage:
    python hkex/extract_allotment_info.py "hkex/data/06656/12107220-0.PDF"
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from pathlib import Path
from typing import List

from PyPDF2 import PdfReader

from openai_utils import (
    chat_completion_with_fallback,
    extract_json_text_from_llm_response,
    initialize_openai_client,
)

KEYWORDS = [
    "cornerstone investor",
    "cornerstone placing",
]

# Whole-word matches for "cornerstone" / "cornerstones" in extracted PDF text.
_CORNERSTONE_WORD_RE = re.compile(r"\bcornerstones?\b", re.IGNORECASE)
DEFAULT_CORNERSTONE_WORD_MIN = 3

# Gate for PRC A-share listing check (allotment PDF narrative).
_A_SHARES_RE = re.compile(r"\ba\s*shares?\b", re.IGNORECASE)
A_SHARE_CONTEXT_RADIUS = 10
A_SHARE_CONTEXT_MAX_HITS = 40
A_SHARE_CONTEXT_MAX_CHARS = 40000


def read_pdf_text(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    pages: List[str] = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n".join(pages)


def _clean_lines(text: str) -> List[str]:
    lines = [line.strip() for line in text.splitlines()]
    return [line for line in lines if line]


def _extract_relevant_context(text: str, radius: int = 14, max_hits: int = 80) -> str:
    """
    Keep contextual windows around keyword hits to avoid sending huge PDFs.
    """
    lines = _clean_lines(text)
    if not lines:
        return ""

    hit_indexes: List[int] = []
    for idx, line in enumerate(lines):
        line_l = line.lower()
        if any(kw in line_l for kw in KEYWORDS):
            hit_indexes.append(idx)
        if len(hit_indexes) >= max_hits:
            break

    if not hit_indexes:
        return "\n".join(lines[:600])

    selected = set()
    for idx in hit_indexes:
        for j in range(max(0, idx - radius), min(len(lines), idx + radius + 1)):
            selected.add(j)

    ordered = [lines[i] for i in sorted(selected)]
    # Keep prompt size reasonable.
    joined = "\n".join(ordered)
    return joined[:120000]


def _extract_cornerstone_section(text: str, max_len: int = 160000) -> str:
    """Extract full cornerstone table block for better investor completeness."""
    lower = text.lower()
    start = lower.find("cornerstone investors")
    if start == -1:
        return ""

    # Prefer stopping at "Total" row for cornerstone table.
    total_idx = lower.find("total", start)
    end = -1
    if total_idx != -1:
        # Keep notes right after total as they still help normalization.
        notes_idx = lower.find("notes:", total_idx)
        end = notes_idx if notes_idx != -1 else total_idx + 1200
    else:
        # Fallback boundary markers
        for marker in [
            "allottee with waivers/consents obtained",
            "allotment results details",
            "lock-up undertakings",
        ]:
            idx = lower.find(marker, start + 1)
            if idx != -1:
                end = idx
                break

    if end == -1:
        end = min(len(text), start + max_len)

    section = text[start:end]
    return section[:max_len]


def count_cornerstone_words(text: str) -> int:
    """Count whole-word occurrences of 'cornerstone' / 'cornerstones' in PDF text."""
    return len(_CORNERSTONE_WORD_RE.findall(text))


def precheck_suggests_cornerstone_investors(
    text: str, min_occurrences: int = DEFAULT_CORNERSTONE_WORD_MIN
) -> bool:
    """
    Non-LLM gate: if the PDF mentions "cornerstone" too rarely, assume there is
    no cornerstone investor section worth sending to the LLM (write "none").
    """
    return count_cornerstone_words(text) >= min_occurrences


def precheck_mentions_a_shares(text: str) -> bool:
    """Non-LLM gate: only call the LLM for A-share listing if 'A share(s)' appears."""
    return bool(_A_SHARES_RE.search(text or ""))


def _extract_a_shares_context(
    text: str,
    *,
    radius: int = A_SHARE_CONTEXT_RADIUS,
    max_hits: int = A_SHARE_CONTEXT_MAX_HITS,
    max_chars: int = A_SHARE_CONTEXT_MAX_CHARS,
) -> str:
    """Keep contextual windows around 'A share(s)' hits for the A-share listing LLM pass."""
    lines = _clean_lines(text)
    if not lines:
        return ""

    hit_indexes: List[int] = []
    for idx, line in enumerate(lines):
        if _A_SHARES_RE.search(line):
            hit_indexes.append(idx)
        if len(hit_indexes) >= max_hits:
            break

    if not hit_indexes:
        return ""

    selected: set[int] = set()
    for idx in hit_indexes:
        for j in range(max(0, idx - radius), min(len(lines), idx + radius + 1)):
            selected.add(j)

    ordered = [lines[i] for i in sorted(selected)]
    return "\n".join(ordered)[:max_chars]


def _extract_json_or_raise(content: str) -> dict:
    json_text = extract_json_text_from_llm_response(content)
    data = json.loads(json_text)
    if not isinstance(data, dict):
        raise ValueError("Model did not return a JSON object.")
    if "cornerstone_investors" in data and not isinstance(data["cornerstone_investors"], list):
        raise ValueError("cornerstone_investors must be a JSON list.")
    if "a_share_listed" in data and not isinstance(data["a_share_listed"], bool):
        # Accept common string/int encodings from models.
        raw = data["a_share_listed"]
        if isinstance(raw, str) and raw.strip().lower() in {"true", "yes", "1"}:
            data["a_share_listed"] = True
        elif isinstance(raw, str) and raw.strip().lower() in {"false", "no", "0"}:
            data["a_share_listed"] = False
        elif raw in (1, 0):
            data["a_share_listed"] = bool(raw)
        else:
            raise ValueError("a_share_listed must be a JSON boolean.")
    return data


def _build_prompt(document_excerpt: str) -> str:
    return f"""Role: You are a financial data extraction specialist.
Task: Analyze the provided document excerpt (an allotment results announcement) and extract one specific data point.

Data Points to Extract:
1) Cornerstone Investors: A complete list of the names from the **Cornerstone Investors table only** (the table headed "Cornerstone Investors" with columns such as Investor, No. of Offer Shares allocated, % of Offer Shares).

Extraction Rules:
- List Format: For cornerstone_investors, return clean entity names and remove parenthetical abbreviations unless required for disambiguation.
- Do NOT treat placees, syndicate parties, swap counterparties, or general "Global Offering" narrative as cornerstone investors unless they appear as rows in that cornerstone table.
- If the field is not found, return an empty list and do not hallucinate.

Output Format:
Return only valid JSON object:
{{
  "cornerstone_investors": ["string", "string"]
}}

Document excerpt:
\"\"\"
{document_excerpt}
\"\"\"
"""


def _build_cornerstone_prompt(cornerstone_section: str, baseline_json: dict) -> str:
    return f"""Role: You are a financial data extraction specialist.
Task: Extract ONLY the complete list of cornerstone investor names from the cornerstone table below.

Rules:
- Return all cornerstone investors listed in the table (do not return placees / connected clients sections).
- Preserve entity names, but remove parenthetical ticker-style short forms when redundant.
- Deduplicate exact duplicates while preserving original ordering.
- Return JSON only with one key: cornerstone_investors.

Expected JSON format:
{{
  "cornerstone_investors": ["string", "string"]
}}

Existing preliminary extraction (may be incomplete):
{json.dumps(baseline_json, ensure_ascii=False)}

Cornerstone table excerpt:
\"\"\"
{cornerstone_section}
\"\"\"
"""


def _build_a_share_prompt(document_excerpt: str) -> str:
    return f"""Role: You are a financial data extraction specialist.
Task: Decide whether the issuer (the listing company itself) is already listed on a PRC A-share market.

Return true only if the excerpt clearly states that the Company's A Shares are already listed
(or have been listed since a given date) on a Mainland China exchange such as the Shenzhen Stock
Exchange or the Shanghai Stock Exchange (including ChiNext / STAR Market boards of those exchanges).

Typical positive wording (wording may vary):
- "as the Company's A Shares are listed on the Shenzhen Stock Exchange since ..."
- "the Company's A Shares have been listed on the Shanghai Stock Exchange ..."

Return false if:
- "A Shares" are only mentioned hypothetically, as a future plan, as a comparison, or about other companies;
- the text only discusses H Shares / the Hong Kong offering without an existing A-share listing of this issuer;
- the evidence is ambiguous.

Output Format:
Return only a valid JSON object:
{{
  "a_share_listed": true
}}

Document excerpt:
\"\"\"
{document_excerpt}
\"\"\"
"""


async def _extract_a_share_listed(full_text: str, client) -> bool:
    """Keyword gate then LLM. No 'A share(s)' mention => False without an LLM call."""
    if not precheck_mentions_a_shares(full_text):
        return False
    excerpt = _extract_a_shares_context(full_text)
    if not excerpt.strip():
        return False
    response = await chat_completion_with_fallback(
        client,
        tier="light",
        messages=[
            {"role": "system", "content": "You extract financial data and return strict JSON only."},
            {"role": "user", "content": _build_a_share_prompt(excerpt)},
        ],
        temperature=0,
    )
    content = response.choices[0].message.content or ""
    data = _extract_json_or_raise(content)
    return bool(data.get("a_share_listed", False))


async def extract_with_ai(pdf_path: Path) -> dict:
    full_text = read_pdf_text(pdf_path)
    if not full_text.strip():
        raise ValueError("No readable text extracted from PDF.")

    needs_cornerstone_llm = precheck_suggests_cornerstone_investors(full_text)
    needs_a_share_llm = precheck_mentions_a_shares(full_text)
    client = initialize_openai_client() if (needs_cornerstone_llm or needs_a_share_llm) else None

    a_share_listed = False
    if client is not None and needs_a_share_llm:
        a_share_listed = await _extract_a_share_listed(full_text, client)

    if not needs_cornerstone_llm:
        return {
            "cornerstone_investors": [],
            "no_cornerstone_table": True,
            "a_share_listed": a_share_listed,
        }

    excerpt = _extract_relevant_context(full_text)
    if not excerpt:
        raise ValueError("No readable text extracted from PDF.")

    assert client is not None
    prompt = _build_prompt(excerpt)
    response = await chat_completion_with_fallback(
        client,
        tier="main",
        messages=[
            {"role": "system", "content": "You extract financial data and return strict JSON only."},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
    )
    content = response.choices[0].message.content or ""
    data = _extract_json_or_raise(content)

    # Second pass focused on cornerstone table to improve completeness.
    cornerstone_section = _extract_cornerstone_section(full_text)
    if cornerstone_section:
        second_prompt = _build_cornerstone_prompt(cornerstone_section, data)
        second_resp = await chat_completion_with_fallback(
            client,
            tier="main",
            messages=[
                {"role": "system", "content": "Return strict JSON only."},
                {"role": "user", "content": second_prompt},
            ],
            temperature=0,
        )
        second_content = second_resp.choices[0].message.content or ""
        second_data = _extract_json_or_raise(second_content)
        investors = second_data.get("cornerstone_investors", [])
        if isinstance(investors, list) and investors:
            data["cornerstone_investors"] = investors

    data.pop("no_cornerstone_table", None)
    data["a_share_listed"] = a_share_listed
    return data


def a_share_listed_cell(result: dict) -> str:
    """Canonical listings-cell value: ``true`` / ``false``."""
    return "true" if bool(result.get("a_share_listed")) else "false"


def save_result_to_csv(result: dict, csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "cornerstone_investors",
                "a_share_listed",
            ],
        )
        writer.writeheader()
        if result.get("no_cornerstone_table"):
            cell = "none"
        else:
            cell = "; ".join(result.get("cornerstone_investors", []))
        writer.writerow(
            {
                "cornerstone_investors": cell,
                "a_share_listed": a_share_listed_cell(result),
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract key IPO fields from allotment-results PDF.")
    parser.add_argument("pdf_path", help="Path to allotment results PDF")
    parser.add_argument(
        "--save-json",
        help="Optional output file path to save JSON",
        default="",
    )
    parser.add_argument(
        "--save-csv",
        help="Optional output file path to save CSV",
        default="",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")

    try:
        result = asyncio.run(extract_with_ai(pdf_path))
    except Exception as exc:
        print(f"Error: {exc}")
        raise SystemExit(1) from exc

    output = json.dumps(result, ensure_ascii=False, indent=2)
    print(output)

    if args.save_json:
        out_path = Path(args.save_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output + "\n", encoding="utf-8")
        print(f"Saved: {out_path}")

    if args.save_csv:
        csv_path = Path(args.save_csv)
        save_result_to_csv(result, csv_path)
        print(f"Saved: {csv_path}")


if __name__ == "__main__":
    main()
