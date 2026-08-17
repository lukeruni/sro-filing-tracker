"""Export lanes: CSV and Excel.

Both render the same rows through ``models.COLUMNS``, so a column added to the
contract appears in every export at once and the two can never drift.
"""

from __future__ import annotations

import csv
import datetime as dt
import sqlite3
from pathlib import Path
from typing import Sequence

from ..models import COLUMNS

# Excel refuses to open a cell longer than this, and a truncated file is worse
# than a truncated cell.
_EXCEL_CELL_LIMIT = 32_000

# A leading =, +, - or @ makes Excel treat text as a formula. Filing summaries
# are attacker-influenced text from third-party websites, so they are prefixed
# to neutralise CSV injection before anyone opens the file.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _rows(records: Sequence[sqlite3.Row]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for record in records:
        keys = record.keys() if hasattr(record, "keys") else []
        out.append({column: (record[column] if column in keys else "") for column in COLUMNS})
    return out


def _safe(value: object, *, limit: int | None = None) -> object:
    if not isinstance(value, str):
        return value
    text = value
    if text.startswith(_FORMULA_PREFIXES):
        text = "'" + text
    if limit and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def timestamped(directory: Path, stem: str, suffix: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{stem}-{stamp}{suffix}"


def to_csv(records: Sequence[sqlite3.Row], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        writer.writeheader()
        for row in _rows(records):
            writer.writerow({k: _safe(v) for k, v in row.items()})
    return path


def to_excel(records: Sequence[sqlite3.Row], path: Path, *, title: str = "Rule Filings") -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    path.parent.mkdir(parents=True, exist_ok=True)
    rows = _rows(records)

    book = Workbook()
    sheet = book.active
    sheet.title = title[:31]

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F3864")
    for index, column in enumerate(COLUMNS, start=1):
        cell = sheet.cell(row=1, column=index, value=column.replace("_", " ").title())
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")

    for row_index, row in enumerate(rows, start=2):
        for col_index, column in enumerate(COLUMNS, start=1):
            sheet.cell(row=row_index, column=col_index,
                       value=_safe(row[column], limit=_EXCEL_CELL_LIMIT))

    widths = {
        "filing_no": 22, "sro": 28, "sro_family": 14, "filing_year": 12,
        "filing_date": 13, "status": 22, "summary": 90, "release_number": 15,
        "filing_url": 46, "source": 26, "source_url": 40, "notes": 40,
        "first_seen": 20, "last_seen": 20, "seen_by": 30,
    }
    for index, column in enumerate(COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = widths.get(column, 18)

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(len(rows) + 1, 1)}"

    book.save(path)
    return path
