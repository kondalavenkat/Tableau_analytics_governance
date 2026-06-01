"""
SQL Refactorer — T-SQL temp-table to permanent-table conversion.

Contract (strictly enforced):
  setup_sql    — DROP IF EXISTS perm_X + SELECT INTO perm_X statements only.
                 No final SELECT. Goes into Initial SQL attribute on the connection.
  runtime_sql  — Single self-contained SELECT with all temp deps inlined as
                 derived subqueries. No physical perm tables required at runtime.
                 Goes into the Custom SQL relation node Tableau executes at query time.
  permanent_table_version — Full combined script for reference/download only.

Key fixes vs old version:
- Guard stripping removes entire IF OBJECT_ID(...) block, no orphan semicolons left
- No double-rewrite: token replacement done exactly once per statement
- DROP only generated for tables actually created in the script
- setup_sql excludes the final SELECT entirely (Initial SQL = setup only)
- runtime_sql is a flat SELECT with derived subqueries — self-contained for ODBC
- Trailing semicolons stripped uniformly at every boundary
- Standalone DROP TABLE #temp statements skipped entirely
"""

import logging
import re

from .sql_dependency_graph import SQLDependencyGraphBuilder

logger = logging.getLogger(__name__)

_CLAUSE_KEYWORDS = {
    "WHERE",
    "GROUP",
    "ORDER",
    "HAVING",
    "UNION",
    "EXCEPT",
    "INTERSECT",
    "JOIN",
    "LEFT",
    "RIGHT",
    "FULL",
    "INNER",
    "CROSS",
    "ON",
}
_CLAUSE_ALIAS_GUARD = "|".join(sorted(_CLAUSE_KEYWORDS, key=len, reverse=True))

# Matches full IF OBJECT_ID('tempdb..#x') IS NOT NULL DROP TABLE #x
# Handles N'...' prefix, optional ,'U' arg, optional trailing semicolon
_GUARD_RE = re.compile(
    r"IF\s+OBJECT_ID\s*\(\s*N?['\"]tempdb\.\.[^'\"]+['\"]"
    r"(?:\s*,\s*N?['\"][A-Za-z]['\"])?\s*\)\s+IS\s+NOT\s+NULL\s+"
    r"DROP\s+TABLE\s+##?\w+\s*;?",
    re.IGNORECASE | re.DOTALL,
)


def _strip_guard(sql: str) -> str:
    """Remove IF OBJECT_ID guard blocks and any orphan leading semicolons."""
    cleaned = _GUARD_RE.sub("", sql).strip()
    cleaned = re.sub(r"^\s*;+\s*", "", cleaned)
    return cleaned.strip()


class SQLRefactorer:
    def __init__(self, permanent_prefix: str = "perm_", runtime_cte_prefix: str = "perm_"):
        self.permanent_prefix = permanent_prefix
        self.runtime_cte_prefix = runtime_cte_prefix
        self.graph_builder = SQLDependencyGraphBuilder()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def refactor_to_permanent_tables(self, sql_text: str, dialect: str = None) -> dict:
        try:
            graph = self.graph_builder.build(sql_text, dialect=dialect)
            statements = graph["statements"]
            active_dialect = graph["dialect"] or "tsql"

            if not statements:
                return self._empty_result(sql_text)

            detection = self.detect_temp_tables(sql_text, dependency_graph=graph)
            token_map: dict[str, str] = {
                tok: self._to_perm_name(tok)
                for tok in detection["temp_table_tokens"]
            }

            final_select_idx = graph["final_select_index"]
            setup_parts: list[str] = []
            permanent_parts: list[str] = []
            final_select_sql: str = ""

            for stmt in statements:
                idx = stmt["index"]
                raw = stmt["sql"]
                stype = stmt["statement_type"]

                # Skip pure guards and bare DROP TABLE #temp
                if self._is_skippable(raw, stype):
                    continue

                rewritten = self._rewrite_statement(
                    raw, stmt.get("expression"), token_map, active_dialect
                )
                if not rewritten.strip():
                    continue

                rewritten_clean = rewritten.strip().rstrip(";")
                created_temp = stmt["created_temp_table"]

                if created_temp:
                    perm_name = token_map.get(created_temp, self._to_perm_name(created_temp))
                    drop_stmt = (
                        f"IF OBJECT_ID('{perm_name}', 'U') IS NOT NULL "
                        f"DROP TABLE {perm_name}"
                    )
                    permanent_parts.append(f"{drop_stmt};")
                    permanent_parts.append(f"{rewritten_clean};")
                    # setup_sql = only the DDL/DML to create permanent tables
                    setup_parts.append(f"{drop_stmt};")
                    setup_parts.append(f"{rewritten_clean};")

                elif idx == final_select_idx:
                    final_select_sql = rewritten_clean
                    permanent_parts.append(f"{rewritten_clean};")
                    # final SELECT intentionally NOT in setup_sql

                else:
                    # Non-temp-creating non-final statements (INSERT, UPDATE, etc.)
                    permanent_parts.append(f"{rewritten_clean};")
                    setup_parts.append(f"{rewritten_clean};")

            permanent_sql = "\n\n".join(permanent_parts)
            setup_sql = "\n\n".join(setup_parts)

            runtime_sql = self._build_runtime_sql(
                statements, final_select_idx, token_map, active_dialect
            )
            runtime_temp_definitions = self._build_runtime_temp_definitions(
                statements, token_map, active_dialect
            )

            analysis = {
                "temp_tables_detected": detection["temp_table_count"],
                "dependency_order": [
                    token_map.get(t, self._to_perm_name(t))
                    for t in graph["dependency_order"]
                ],
                "notes": "Temp tables converted to permanent tables.",
                "permanent_table_names": sorted(token_map.values()),
                "statement_count": len(statements),
                "statement_edges": graph["statement_edges"],
                "temp_dependencies": {
                    token_map.get(k, self._to_perm_name(k)): [
                        token_map.get(d, self._to_perm_name(d)) for d in deps
                    ]
                    for k, deps in graph["temp_dependencies"].items()
                },
                "complexity_reduction": {
                    "original_statements": len(statements),
                    "temp_tables_converted": detection["temp_table_count"],
                },
            }

            return {
                "analysis": analysis,
                "permanent_table_version": permanent_sql,
                "setup_sql": setup_sql,
                "validation_sql": final_select_sql or permanent_sql.rstrip(";"),
                "runtime_sql": runtime_sql or final_select_sql or "",
                "runtime_temp_definitions": runtime_temp_definitions,
                "runtime_cte_prefix": self.runtime_cte_prefix,
                "success": True,
            }

        except Exception as exc:
            logger.error("Refactorer error: %s", exc, exc_info=True)
            return {
                "analysis": {
                    "notes": f"Error: {exc}",
                    "temp_tables_detected": 0,
                    "dependency_order": [],
                },
                "error": str(exc),
                "success": False,
            }

    def detect_temp_tables(self, sql_text: str, dependency_graph: dict = None) -> dict:
        graph = dependency_graph or self.graph_builder.build(sql_text)
        created = set(graph.get("temp_tables", []))
        referenced: set[str] = set()
        for stmt in graph.get("statements", []):
            referenced.update(stmt.get("referenced_temp_tables", []))

        all_tokens = sorted(created | referenced)
        if not all_tokens:
            all_tokens = sorted(set(re.findall(r"##?\w+", sql_text, re.IGNORECASE)))

        return {
            "has_temp_tables": bool(all_tokens),
            "has_convertible_temp_tables": bool(created),
            "temp_table_tokens": all_tokens,
            "created_temp_table_tokens": sorted(created),
            "referenced_temp_table_tokens": sorted(referenced),
            "temp_table_names": [t.lstrip("#") for t in all_tokens],
            "temp_table_count": len(all_tokens),
            "permanent_table_names": [self._to_perm_name(t) for t in all_tokens],
        }

    def build_runtime_sql_from_definitions(self, sql_text: str, runtime_temp_definitions: dict[str, str]) -> str:
        temp_tokens = sorted(runtime_temp_definitions.keys(), key=len, reverse=True)
        token_map = {token: self._to_perm_name(token) for token in temp_tokens}
        final_sql = _strip_guard(sql_text).strip().rstrip(";")
        runtime_sql = self._build_runtime_sql_from_definitions_sqlglot(
            final_sql,
            runtime_temp_definitions,
            token_map,
            "tsql",
        )
        if runtime_sql:
            return runtime_sql

        producer_sql = self._normalise_runtime_definitions(runtime_temp_definitions)
        return self._inline_producers_regex(final_sql, producer_sql, token_map)

    # ------------------------------------------------------------------ #
    # Runtime SQL — self-contained SELECT for Tableau / ODBC              #
    # ------------------------------------------------------------------ #

    def _build_runtime_sql(
        self,
        statements: list[dict],
        final_select_idx: int | None,
        token_map: dict[str, str],
        dialect: str,
    ) -> str:
        if final_select_idx is None:
            return ""
        final_stmt = next((s for s in statements if s["index"] == final_select_idx), None)
        if final_stmt is None:
            return ""

        runtime = self._build_runtime_sqlglot(statements, final_stmt, token_map, dialect)
        if runtime:
            return runtime
        return self._build_runtime_regex(statements, final_stmt, token_map)

    def _build_runtime_temp_definitions(
        self, statements: list[dict], token_map: dict[str, str], dialect: str
    ) -> dict[str, str]:
        definitions = self._build_runtime_temp_definitions_sqlglot(statements, token_map, dialect)
        if definitions:
            return definitions
        return self._build_runtime_temp_definitions_regex(statements, token_map)

    def _build_runtime_sql_from_definitions_sqlglot(
        self,
        sql_text: str,
        runtime_temp_definitions: dict[str, str],
        token_map: dict[str, str],
        dialect: str,
    ) -> str:
        try:
            import sqlglot
        except ImportError:
            return ""

        producer_expr = self._build_runtime_definition_expressions(
            runtime_temp_definitions,
            token_map,
            dialect,
        )
        if not producer_expr:
            return ""

        try:
            expression = sqlglot.parse_one(sql_text, read=dialect or "tsql")
        except Exception:
            return ""

        result_expr = self._inline_producers_in_expr(
            expression,
            producer_expr,
            token_map,
            dialect,
        )

        try:
            return result_expr.sql(dialect=dialect or "tsql", pretty=True).strip().rstrip(";")
        except Exception:
            return ""

    def _build_runtime_sqlglot(
        self, statements: list[dict], final_stmt: dict, token_map: dict, dialect: str
    ) -> str:
        try:
            from sqlglot import exp as sg_exp
        except ImportError:
            return ""

        producer_expr: dict[str, object] = {}

        for stmt in statements:
            ct = stmt.get("created_temp_table")
            expr = stmt.get("expression")
            if not ct or stmt.get("statement_type") != "select_into_temp" or expr is None:
                continue
            body = self._strip_into_from_expr(expr)
            if body is None:
                continue
            inlined = self._inline_producers_in_expr(body, producer_expr, token_map, dialect)
            producer_expr[ct.lower()] = inlined

        final_expr = final_stmt.get("expression")
        if final_expr is None:
            return ""

        result_expr = self._inline_producers_in_expr(
            final_expr, producer_expr, token_map, dialect
        )
        try:
            sql = result_expr.sql(dialect=dialect or "tsql", pretty=True).strip().rstrip(";")
            return sql
        except Exception:
            return ""

    def _build_runtime_temp_definitions_sqlglot(
        self, statements: list[dict], token_map: dict[str, str], dialect: str
    ) -> dict[str, str]:
        definitions: dict[str, str] = {}
        try:
            from sqlglot import exp as sg_exp
        except ImportError:
            return definitions

        producer_expr: dict[str, object] = {}

        for stmt in statements:
            ct = stmt.get("created_temp_table")
            expr = stmt.get("expression")
            if not ct or stmt.get("statement_type") != "select_into_temp" or expr is None:
                continue

            body = self._strip_into_from_expr(expr)
            if body is None:
                continue

            inlined = self._inline_producers_in_expr(body, producer_expr, token_map, dialect)
            producer_expr[ct.lower()] = inlined
            try:
                definitions[ct] = inlined.sql(dialect=dialect or "tsql", pretty=True).strip().rstrip(";")
            except Exception:
                continue

        return definitions

    def _build_runtime_definition_expressions(
        self,
        runtime_temp_definitions: dict[str, str],
        token_map: dict[str, str],
        dialect: str,
    ) -> dict[str, object]:
        try:
            import sqlglot
        except ImportError:
            return {}

        raw_defs = self._normalise_runtime_definitions(runtime_temp_definitions)
        expressions: dict[str, object] = {}

        for token, sql_body in raw_defs.items():
            try:
                body_expr = sqlglot.parse_one(sql_body, read=dialect or "tsql")
            except Exception:
                continue

            inlined = self._inline_producers_in_expr(
                body_expr,
                expressions,
                token_map,
                dialect,
            )
            expressions[token] = inlined

        return expressions

    def _strip_into_from_expr(self, expression):
        try:
            from sqlglot import exp as sg_exp
            if isinstance(expression, sg_exp.Select):
                copy = expression.copy()
                copy.set("into", None)
                return copy
        except Exception:
            pass
        return None

    def _inline_producers_in_expr(
        self, expression, producer_expr: dict, token_map: dict, dialect: str
    ):
        try:
            from sqlglot import exp as sg_exp
        except ImportError:
            return expression

        def replacer(node):
            if not isinstance(node, sg_exp.Table):
                return node
            raw_name = node.sql(dialect=dialect or "tsql").strip()
            norm = raw_name.strip().strip("[]")
            prod = producer_expr.get(norm.lower())
            if prod is None:
                return node
            alias_node = node.args.get("alias")
            alias_name = (
                alias_node.this.name if alias_node else None
            ) or token_map.get(norm, self._to_perm_name(norm))
            return sg_exp.Subquery(
                this=prod.copy(),
                alias=sg_exp.TableAlias(this=sg_exp.to_identifier(alias_name)),
            )

        return expression.copy().transform(replacer)

    def _build_runtime_regex(
        self, statements: list[dict], final_stmt: dict, token_map: dict
    ) -> str:
        producer_sql: dict[str, str] = {}

        for stmt in statements:
            ct = stmt.get("created_temp_table")
            if not ct or stmt.get("statement_type") != "select_into_temp":
                continue
            body = _strip_guard(stmt["sql"])
            body = re.sub(
                rf"\bINTO\s+{re.escape(ct)}\b", "", body, flags=re.IGNORECASE
            ).strip().rstrip(";")
            body = self._inline_producers_regex(body, producer_sql, token_map)
            producer_sql[ct.lower()] = body

        final_sql = _strip_guard(final_stmt["sql"]).strip().rstrip(";")
        return self._inline_producers_regex(final_sql, producer_sql, token_map)

    def _build_runtime_temp_definitions_regex(
        self, statements: list[dict], token_map: dict[str, str]
    ) -> dict[str, str]:
        definitions: dict[str, str] = {}
        producer_sql: dict[str, str] = {}

        for stmt in statements:
            ct = stmt.get("created_temp_table")
            if not ct or stmt.get("statement_type") != "select_into_temp":
                continue
            body = _strip_guard(stmt["sql"])
            body = re.sub(
                rf"\bINTO\s+{re.escape(ct)}\b", "", body, flags=re.IGNORECASE
            ).strip().rstrip(";")
            body = self._inline_producers_regex(body, producer_sql, token_map)
            producer_sql[ct.lower()] = body
            definitions[ct] = body

        return definitions

    def _normalise_runtime_definitions(self, runtime_temp_definitions: dict[str, str]) -> dict[str, str]:
        return {
            token.lower(): (body or "").strip().rstrip(";")
            for token, body in runtime_temp_definitions.items()
            if (body or "").strip()
        }

    def _inline_producers_regex(
        self, sql: str, producer_sql: dict, token_map: dict
    ) -> str:
        if not sql:
            return ""

        pattern = re.compile(
            r"(?P<kw>\bFROM|\bJOIN)\s+(?P<tbl>##?\w+)"
            rf"(?:\s+(?:AS\s+)?(?P<alias>(?!(?:{_CLAUSE_ALIAS_GUARD})\b)[A-Za-z_]\w*))?",
            re.IGNORECASE,
        )

        def sub(m):
            tbl = m.group("tbl")
            kw = m.group("kw")
            alias = m.group("alias")
            body = producer_sql.get(tbl.lower())
            if body is None:
                perm = token_map.get(tbl, self._to_perm_name(tbl))
                return f"{kw} {perm}" + (f" AS {alias}" if alias else "")
            alias_name = alias or token_map.get(tbl, self._to_perm_name(tbl))
            return f"{kw} (\n{body}\n) AS {alias_name}"

        result = pattern.sub(sub, sql)
        result = self._replace_tokens(result, token_map)
        return result.strip().rstrip(";")

    # ------------------------------------------------------------------ #
    # Statement rewriting                                                  #
    # ------------------------------------------------------------------ #

    def _rewrite_statement(
        self, sql: str, expression, token_map: dict, dialect: str
    ) -> str:
        sql = _strip_guard(sql)

        if expression is not None:
            try:
                rewritten = self._rewrite_sqlglot(expression, token_map, dialect)
                if rewritten:
                    return rewritten.strip().rstrip(";")
            except Exception:
                pass

        return self._replace_tokens(sql, token_map).strip().rstrip(";")

    def _rewrite_sqlglot(self, expression, token_map: dict, dialect: str) -> str | None:
        try:
            from sqlglot import exp as sg_exp

            def replacer(node):
                if isinstance(node, sg_exp.Table):
                    raw = node.sql(dialect=dialect or "tsql").strip()
                    norm = raw.strip().strip("[]")
                    repl = self._find_token_replacement(norm, token_map)
                    if repl:
                        return sg_exp.to_table(repl)
                return node

            rewritten = expression.copy().transform(replacer)
            return rewritten.sql(dialect=dialect or "tsql", pretty=True)
        except Exception:
            return None

    def _find_token_replacement(self, name: str, token_map: dict) -> str | None:
        if name in token_map:
            return token_map[name]
        for k, v in token_map.items():
            if k.lower() == name.lower():
                return v
        return None

    def _replace_tokens(self, sql: str, token_map: dict) -> str:
        for token, perm in sorted(token_map.items(), key=lambda x: len(x[0]), reverse=True):
            sql = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])",
                perm,
                sql,
                flags=re.IGNORECASE,
            )
        return sql

    # ------------------------------------------------------------------ #
    # Guard / DROP detection                                               #
    # ------------------------------------------------------------------ #

    def _is_skippable(self, sql: str, stype: str) -> bool:
        if stype == "drop_guard":
            return True
        stripped = _strip_guard(sql).strip()
        if not stripped:
            return True
        if re.match(r"^\s*DROP\s+TABLE\s+##?\w+\s*;?\s*$", sql, re.IGNORECASE):
            return True
        return False

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _to_perm_name(self, temp_token: str) -> str:
        clean = re.sub(r"[^A-Za-z0-9_]", "", temp_token.lstrip("#"))
        return f"{self.permanent_prefix}{clean}"

    def _empty_result(self, sql_text: str) -> dict:
        return {
            "analysis": {
                "notes": "No valid SQL statements found.",
                "temp_tables_detected": 0,
                "dependency_order": [],
            },
            "permanent_table_version": sql_text,
            "setup_sql": sql_text,
            "validation_sql": sql_text,
            "runtime_sql": sql_text,
            "runtime_temp_definitions": {},
            "runtime_cte_prefix": self.runtime_cte_prefix,
            "success": True,
        }
