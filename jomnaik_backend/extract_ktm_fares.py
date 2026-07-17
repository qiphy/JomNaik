#!/usr/bin/env python3
"""Extract the vector KTM Komuter cash-fare matrix from the supplied PDF."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pdfplumber


def reverse_pdf_label(value: str) -> str:
    lines = [line.strip() for line in (value or "").splitlines() if line.strip()]
    return re.sub(r"\s+", " ", " ".join(line[::-1] for line in reversed(lines))).strip()


def split_cell(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").splitlines()]


def extract(pdf_path: Path) -> dict:
    with pdfplumber.open(pdf_path) as pdf:
        table = pdf.pages[0].extract_tables()[0]

    station_names = [reverse_pdf_label(table[2][column]) for column in range(3, 60)]
    rows: list[list[float | None]] = []
    source_row = 3
    while source_row < 53:
        source = table[source_row]
        line_count = max(len(split_cell(value)) for value in source[2:60])
        for line in range(line_count):
            values: list[float | None] = []
            for column in range(3, 60):
                parts = split_cell(source[column])
                value = parts[line] if line < len(parts) else "-"
                try:
                    values.append(None if value in {"", "-", "—"} else float(value))
                except ValueError as error:
                    raise ValueError(f"Unexpected fare {value!r} at row {source_row}, column {column}") from error
            rows.append(values)
        source_row += 1

    if len(rows) != len(station_names) or any(len(row) != len(station_names) for row in rows):
        raise ValueError(f"Expected a square KTM matrix, got {len(station_names)} stations and {len(rows)} rows")
    return {
        "effectiveDate": "2015-12-02",
        "paymentType": "cash",
        "stationNames": station_names,
        "matrix": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(extract(args.pdf), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
