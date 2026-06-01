"""
Initial SQL Converter

Analyzes Initial SQL and separates it into:
  - setup statements  (USE, SET, connection config — discarded from data flow)
  - data statements   (SELECT INTO #temp, CREATE TABLE #temp, final SELECT)

The data statements are handed to SQLRefactorer.
Result: setup_sql goes into Initial SQL attribute, runtime_sql goes into relation node.
"""

import logging
import re
from typing import Dict, List

logger = logging.getLogger(__name__)


class InitialSQLConverter:

    def analyze_initial_sql(self, sql_text: str) -> Dict:
        """
        Split Initial SQL into setup statements and data statements.
        Setup = USE, SET, EXEC sp_, GO (batch separators treated as boundaries).
        Data = everything else (SELECT, SELECT INTO #temp, CREATE TABLE #temp, DROP TABLE).

        Returns dict with keys:
          has_setup, has_temp_tables, setup_statements, data_statements,
          complexity, total_statements
        """
        statements = self._split_statements(sql_text)

        setup_stmts: List[str] = []
        data_stmts: List[str] = []
        has_temp_tables = False

        for stmt in statements:
            s = stmt.strip()
            if not s:
                continue
            upper = s.upper()

            if self._is_setup_statement(upper):
                setup_stmts.append(s)
                continue

            if re.search(r"\bINTO\s+##?\w+\b", s, re.IGNORECASE) or \
               re.search(r"\bCREATE\s+TABLE\s+##?\w+\b", s, re.IGNORECASE):
                has_temp_tables = True

            data_stmts.append(s)

        complexity = "simple"
        if has_temp_tables:
            complexity = "medium"
        if len(data_stmts) > 5 or any("CURSOR" in s.upper() for s in data_stmts):
            complexity = "complex"

        return {
            "has_setup": bool(setup_stmts),
            "has_temp_tables": has_temp_tables,
            "setup_statements": setup_stmts,
            "data_statements": data_stmts,
            "complexity": complexity,
            "total_statements": len(statements),
        }

    def _is_setup_statement(self, upper_stmt: str) -> bool:
        setup_prefixes = ("USE ", "SET ", "EXEC SP_", "EXEC(", "GO")
        return any(upper_stmt.startswith(p) for p in setup_prefixes)

    def _split_statements(self, sql_text: str) -> List[str]:
        """Split on semicolons and GO, normalising whitespace."""
        # Replace GO batch separator with semicolon
        sql_text = re.sub(r"(?m)^\s*GO\s*$", ";", sql_text, flags=re.IGNORECASE)
        parts = [s.strip() for s in sql_text.split(";") if s.strip()]
        return parts
