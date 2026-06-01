"""
Temp Query Scan Service

Deterministic pipeline:
1. Parse workbook XML and record exact target nodes for Initial SQL and Custom SQL.
2. Convert Initial SQL producers into setup SQL plus runtime temp definitions.
3. Convert Custom SQL consumers using the runtime temp definitions from related Initial SQL.
4. Return rows that carry both converted SQL and exact XML targets for write-back.
"""

import os
import shutil
import tempfile
from pathlib import Path

from backend.conversion.initial_sql_converter import InitialSQLConverter
from backend.conversion.sql_refactorer import SQLRefactorer
from backend.workbook.file_handler import process_file_input
from backend.workbook.tableau_parser import TableauParser
from backend.workbook.twbx_writer import TWBxWriter


class TempQueryScanService:

    def __init__(self):
        self.refactorer = SQLRefactorer()
        self.initial_converter = InitialSQLConverter()

    def scan_workbook_file(self, file_path: str) -> list[dict]:
        file_info = process_file_input(file_path)
        extract_temp_dir = file_info.get("temp_dir")

        try:
            parser = TableauParser(
                file_info["twb"],
                datasource_paths=file_info.get("datasources", []),
                package_root=file_info.get("root_dir"),
            )
            parser.parse()

            custom_entries = parser.extract_custom_sql()
            initial_entries = parser.extract_initial_sql()

            results: list[dict] = []
            initial_rows: list[dict] = []

            for entry in initial_entries:
                row = self._process_initial_sql_entry(entry, file_path)
                if row is None:
                    continue
                initial_rows.append(row)
                results.append(row)

            for entry in custom_entries:
                row = self._process_custom_sql_entry(entry, file_path, initial_rows)
                if row is None:
                    continue
                results.append(row)
                if row.get("targets"):
                    for initial_row in row.get("related_initial_rows", []):
                        self._append_unique_targets(
                            initial_row["related_runtime_targets"],
                            row["targets"],
                        )
                row.pop("related_initial_rows", None)

            return results
        finally:
            if "parser" in locals():
                parser.cleanup()
            if extract_temp_dir:
                extract_temp_dir.cleanup()

    def scan_uploaded_workbook(self, upload_file) -> list[dict]:
        upload_dir = tempfile.TemporaryDirectory()
        try:
            input_path = os.path.join(upload_dir.name, upload_file.filename)
            with open(input_path, "wb") as buffer:
                shutil.copyfileobj(upload_file.file, buffer)
            return self.scan_workbook_file(input_path)
        finally:
            upload_dir.cleanup()

    def scan_and_write_back(self, file_path: str, output_path: str = None) -> dict:
        results = self.scan_workbook_file(file_path)
        if not results:
            return {"results": [], "output_path": None, "message": "No temp-table queries found"}

        writer = TWBxWriter()
        written_path = writer.write_back(file_path, results, output_path)
        return {
            "results": results,
            "output_path": written_path,
            "message": f"Write-back complete. {len(results)} queries converted.",
        }

    def write_back_selected_queries(self, workbook_path: str, selected_queries: list[dict], output_path: str = None) -> dict:
        applicable = [
            query
            for query in selected_queries
            if query.get("source_path") == workbook_path and query.get("status") == "converted"
        ]
        if not applicable:
            return {
                "results": [],
                "output_path": None,
                "message": "No selected converted queries found for this workbook.",
            }

        writer = TWBxWriter()
        written_path = writer.write_back(workbook_path, applicable, output_path)
        return {
            "results": applicable,
            "output_path": written_path,
            "message": f"Write-back complete. {len(applicable)} selected queries converted.",
        }

    def _process_initial_sql_entry(self, entry: dict, file_path: str) -> dict | None:
        original_sql = entry["query"]
        initial_analysis = self.initial_converter.analyze_initial_sql(original_sql)
        analysis_sql = "\n".join(initial_analysis.get("data_statements", [])).strip() or original_sql

        detection = self.refactorer.detect_temp_tables(analysis_sql)
        if not detection["has_convertible_temp_tables"]:
            return None

        refactor_result = self.refactorer.refactor_to_permanent_tables(analysis_sql, dialect="tsql")
        return {
            "workbook_name": Path(file_path).name,
            "source_path": file_path,
            "datasource": entry.get("datasource", "Unknown"),
            "query_name": entry["name"],
            "query_hash": entry.get("query_hash"),
            "type": "Initial SQL",
            "original_temp_query": analysis_sql,
            "raw_initial_sql": original_sql,
            "converted_permanent_query": refactor_result.get("setup_sql", refactor_result.get("permanent_table_version", "")),
            "setup_sql": refactor_result.get("setup_sql", ""),
            "runtime_sql": refactor_result.get("runtime_sql", ""),
            "runtime_temp_definitions": refactor_result.get("runtime_temp_definitions", {}),
            "runtime_cte_prefix": refactor_result.get("runtime_cte_prefix", "perm_"),
            "targets": entry.get("targets", []),
            "related_runtime_targets": [],
            "temp_table_tokens": detection.get("temp_table_tokens", []),
            "status": "converted" if refactor_result.get("success") else "failed",
            "error_message": refactor_result.get("error", ""),
        }

    def _process_custom_sql_entry(self, entry: dict, file_path: str, initial_rows: list[dict]) -> dict | None:
        original_sql = entry["query"]
        detection = self.refactorer.detect_temp_tables(original_sql)
        if not detection["has_temp_tables"]:
            return None

        related_initial_rows = self._find_related_initial_rows(entry, detection, initial_rows)
        runtime_temp_definitions = self._merge_runtime_definitions(related_initial_rows)

        runtime_sql = ""
        setup_sql = ""
        display_sql = ""
        error_message = ""
        success = False

        if runtime_temp_definitions:
            runtime_sql = self.refactorer.build_runtime_sql_from_definitions(
                original_sql,
                runtime_temp_definitions,
            )
            display_sql = runtime_sql
            success = bool(runtime_sql)
        elif detection["has_convertible_temp_tables"]:
            refactor_result = self.refactorer.refactor_to_permanent_tables(original_sql, dialect="tsql")
            runtime_sql = refactor_result.get("runtime_sql", "")
            setup_sql = refactor_result.get("setup_sql", "")
            runtime_temp_definitions = refactor_result.get("runtime_temp_definitions", {})
            display_sql = runtime_sql or refactor_result.get("permanent_table_version", "")
            error_message = refactor_result.get("error", "")
            success = bool(refactor_result.get("success"))
        else:
            error_message = "Temp table is referenced but no related Initial SQL producer was identified."

        return {
            "workbook_name": Path(file_path).name,
            "source_path": file_path,
            "datasource": entry.get("datasource", "Unknown"),
            "query_name": entry["name"],
            "query_hash": entry.get("query_hash"),
            "type": "Custom SQL",
            "original_temp_query": original_sql,
            "raw_initial_sql": None,
            "converted_permanent_query": display_sql,
            "setup_sql": setup_sql,
            "runtime_sql": runtime_sql,
            "runtime_temp_definitions": runtime_temp_definitions,
            "runtime_cte_prefix": "perm_",
            "targets": entry.get("targets", []),
            "related_runtime_targets": [],
            "temp_table_tokens": detection.get("temp_table_tokens", []),
            "status": "converted" if success else "failed",
            "error_message": error_message,
            "related_initial_rows": related_initial_rows,
        }

    def _find_related_initial_rows(self, custom_entry: dict, detection: dict, initial_rows: list[dict]) -> list[dict]:
        related = []
        referenced_tokens = {
            token.lower()
            for token in (
                detection.get("referenced_temp_table_tokens")
                or detection.get("temp_table_tokens")
                or []
            )
        }

        for row in initial_rows:
            if row.get("datasource") != custom_entry.get("datasource"):
                continue
            available_tokens = {
                token.lower() for token in (row.get("runtime_temp_definitions") or {}).keys()
            }
            if referenced_tokens & available_tokens:
                related.append(row)
        return related

    def _merge_runtime_definitions(self, initial_rows: list[dict]) -> dict[str, str]:
        merged = {}
        for row in initial_rows:
            for token, sql_text in (row.get("runtime_temp_definitions") or {}).items():
                if token not in merged:
                    merged[token] = sql_text
        return merged

    def _append_unique_targets(self, existing_targets: list[dict], new_targets: list[dict]):
        seen = {
            (
                target.get("source_kind"),
                target.get("source_relpath"),
                target.get("nested_inner_relpath"),
                target.get("target_kind"),
                target.get("xpath"),
                target.get("target_attr_name"),
            )
            for target in existing_targets
        }
        for target in new_targets:
            signature = (
                target.get("source_kind"),
                target.get("source_relpath"),
                target.get("nested_inner_relpath"),
                target.get("target_kind"),
                target.get("xpath"),
                target.get("target_attr_name"),
            )
            if signature in seen:
                continue
            seen.add(signature)
            existing_targets.append(target)
