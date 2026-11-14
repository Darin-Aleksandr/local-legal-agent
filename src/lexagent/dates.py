from __future__ import annotations

from datetime import date, datetime

FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%Y")


def parse_date(value: str) -> date | None:
    for fmt in FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None
