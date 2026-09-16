"""
Helpers for reading the legacy CSV exports in `tables/`.

Those files need care. Every one is padded with thousands of comma-only rows (an
Excel export artefact), the header carries a UTF-8 BOM, and several files have
headers that do not describe their columns — `static_BCM_Sections_Questions.csv`
puts the question text in `Question_Description` while `Title` and `Question` hold
integers.
"""

import csv
from pathlib import Path

from django.conf import settings


def legacy_table_path(filename: str) -> Path:
    """Absolute path to a CSV in the repo's `tables/` directory."""
    return Path(settings.REPO_ROOT) / "tables" / filename


def read_legacy_csv(filename: str) -> list[dict[str, str]]:
    """Return only the rows that carry real data.

    Drops the padding rows: anything where every value is empty or whitespace.
    """
    path = legacy_table_path(filename)
    if not path.exists():
        raise FileNotFoundError(f"Legacy CSV not found: {path}")

    # utf-8-sig strips the BOM that would otherwise corrupt the first header name.
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = []
        for raw in csv.DictReader(handle):
            cleaned = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k}
            if any(cleaned.values()):
                rows.append(cleaned)
    return rows


def to_int(value: str) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def to_decimal_str(value: str) -> str | None:
    value = (value or "").strip()
    return value or None
