"""
Print Initial SQL and Custom SQL queries from a Tableau workbook/datasource.

This is a small backend-only runner that reuses the project workbook parsing
logic. It does not start FastAPI, use the UI, convert SQL, or write files.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.workbook.file_handler import process_file_input
from backend.workbook.tableau_parser import TableauParser


def _build_parser(file_path: str) -> tuple[TableauParser, object | None]:
    file_info = process_file_input(file_path)
    parser = TableauParser(
        file_info["twb"],
        datasource_paths=file_info.get("datasources", []),
        package_root=file_info.get("root_dir"),
    )
    parser.parse()
    return parser, file_info.get("temp_dir")


def _print_entry(index: int, entry: dict) -> None:
    source_files = entry.get("source_files") or []
    targets = entry.get("targets") or []

    print(f"\n[{index}] {entry.get('type', 'Query')}")
    print(f"Datasource : {entry.get('datasource', 'Unknown')}")
    print(f"Name       : {entry.get('name', 'Unnamed')}")
    if source_files:
        print(f"Source     : {', '.join(source_files)}")
    if targets:
        target_locations = [
            target.get("source_relpath") or target.get("source_kind") or "unknown"
            for target in targets
        ]
        print(f"XML target : {', '.join(dict.fromkeys(target_locations))}")
    print("SQL:")
    print(entry.get("query", "").strip())


def scan_tableau_queries(file_path: str) -> list[dict]:
    parser = None
    temp_dir = None

    try:
        parser, temp_dir = _build_parser(file_path)
        initial_entries = parser.extract_initial_sql()
        custom_entries = parser.extract_custom_sql()
        return initial_entries + custom_entries
    finally:
        if parser is not None:
            parser.cleanup()
        if temp_dir is not None:
            temp_dir.cleanup()


def main() -> int:
    arg_parser = argparse.ArgumentParser(
        description="Scan a .twb/.twbx/.tds/.tdsx file and print Initial SQL and Custom SQL queries."
    )
    arg_parser.add_argument(
        "path",
        nargs="?",
        help="Path to the Tableau .twb/.twbx/.tds/.tdsx file.",
    )
    args = arg_parser.parse_args()

    file_path = args.path or input("Enter Tableau file path: ").strip().strip('"')
    if not file_path:
        print("No file path provided.")
        return 2

    file_path = os.path.abspath(file_path)
    print(f"Scanning: {file_path}")

    try:
        entries = scan_tableau_queries(file_path)
    except Exception as exc:
        print(f"Scan failed: {exc}")
        return 1

    initial_count = sum(1 for entry in entries if entry.get("type") == "Initial SQL")
    custom_count = sum(1 for entry in entries if entry.get("type") == "Custom SQL")

    print("\nScan result")
    print(f"Initial SQL queries : {initial_count}")
    print(f"Custom SQL queries  : {custom_count}")
    print(f"Total queries       : {len(entries)}")

    if not entries:
        print("\nNo Initial SQL or Custom SQL queries found.")
        return 0

    for index, entry in enumerate(entries, start=1):
        _print_entry(index, entry)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
