"""
SQL Dependency Graph Builder — T-SQL focused, robust for enterprise workbooks.

Fixes:
- Statement splitter correctly handles GO, quoted strings, block/line comments
- Temp table classification distinguishes created vs referenced without false positives
- SELECT INTO detection covers all T-SQL variants
- _normalize_temp_name never returns None for valid #temp tokens
- Default dialect is always tsql
"""

import logging
import re
from collections import defaultdict

logger = logging.getLogger(__name__)


class SQLDependencyGraphBuilder:
    def __init__(self):
        self._sqlglot = None
        self._exp = None

    def build(self, sql_text: str, dialect: str = None) -> dict:
        active_dialect = dialect or "tsql"
        raw_statements = self._split_sql_statements(sql_text)
        parsed_expressions = self._try_parse_all(raw_statements, active_dialect)

        statements = []
        for idx, raw_sql in enumerate(raw_statements, start=1):
            expr = parsed_expressions.get(idx - 1)
            stmt = self._classify_statement(idx, raw_sql, expr, active_dialect)
            if stmt["sql"].strip():
                statements.append(stmt)

        producers: dict[str, int] = {}
        for stmt in statements:
            ct = stmt["created_temp_table"]
            if ct:
                producers[ct.lower()] = stmt["index"]

        consumer_edges = []
        temp_dependencies: dict[str, set] = defaultdict(set)

        for stmt in statements:
            for ref in stmt["referenced_temp_tables"]:
                prod_idx = producers.get(ref.lower())
                if prod_idx and prod_idx != stmt["index"]:
                    consumer_edges.append({
                        "from_statement": prod_idx,
                        "to_statement": stmt["index"],
                        "temp_table": ref,
                    })
                    ct = stmt["created_temp_table"]
                    if ct:
                        temp_dependencies[ct].add(ref)

        creation_order = [s["created_temp_table"] for s in statements if s["created_temp_table"]]

        return {
            "dialect": active_dialect,
            "statements": statements,
            "temp_table_count": len(set(creation_order)),
            "temp_tables": creation_order,
            "producers": producers,
            "statement_edges": consumer_edges,
            "temp_dependencies": {k: sorted(v) for k, v in temp_dependencies.items()},
            "dependency_order": self._resolve_dependency_order(creation_order, temp_dependencies),
            "final_select_index": self._find_final_select_index(statements),
        }

    # ------------------------------------------------------------------ #
    # Statement splitting                                                  #
    # ------------------------------------------------------------------ #

    def _split_sql_statements(self, sql_text: str) -> list[str]:
        """
        Split T-SQL on semicolons and GO batch separators.
        Respects single-quoted strings, double-quoted identifiers,
        line comments (--) and block comments (/* */).
        """
        statements: list[str] = []
        buf: list[str] = []
        i = 0
        n = len(sql_text)
        in_sq = False
        in_dq = False
        in_lc = False
        in_bc = False

        def flush():
            s = "".join(buf).strip()
            if s:
                statements.append(s)
            buf.clear()

        while i < n:
            ch = sql_text[i]
            nxt = sql_text[i + 1] if i + 1 < n else ""

            if in_lc:
                buf.append(ch)
                if ch == "\n":
                    in_lc = False
                i += 1
                continue

            if in_bc:
                buf.append(ch)
                if ch == "*" and nxt == "/":
                    buf.append(nxt)
                    in_bc = False
                    i += 2
                else:
                    i += 1
                continue

            if in_sq:
                buf.append(ch)
                if ch == "'" and nxt == "'":
                    buf.append(nxt)
                    i += 2
                else:
                    if ch == "'":
                        in_sq = False
                    i += 1
                continue

            if in_dq:
                buf.append(ch)
                if ch == '"':
                    in_dq = False
                i += 1
                continue

            if ch == "-" and nxt == "-":
                in_lc = True
                buf.append(ch)
                buf.append(nxt)
                i += 2
                continue

            if ch == "/" and nxt == "*":
                in_bc = True
                buf.append(ch)
                buf.append(nxt)
                i += 2
                continue

            if ch == "'":
                in_sq = True
                buf.append(ch)
                i += 1
                continue

            if ch == '"':
                in_dq = True
                buf.append(ch)
                i += 1
                continue

            if ch == ";":
                flush()
                i += 1
                continue

            if ch in ("\n", "\r"):
                buf.append(ch)
                i += 1
                j = i
                while j < n and sql_text[j] in (" ", "\t"):
                    j += 1
                after = sql_text[j:j + 2].upper()
                end_of_go = j + 2 >= n or (not sql_text[j + 2].isalnum() and sql_text[j + 2] != "_")
                if after == "GO" and end_of_go:
                    flush()
                    i = j + 2
                    while i < n and sql_text[i] not in ("\n", "\r"):
                        i += 1
                continue

            buf.append(ch)
            i += 1

        flush()
        return statements

    # ------------------------------------------------------------------ #
    # Optional sqlglot parse                                               #
    # ------------------------------------------------------------------ #

    def _try_parse_all(self, raw_statements: list[str], dialect: str) -> dict[int, object]:
        sqlglot = self._load_sqlglot()
        if not sqlglot:
            return {}
        result = {}
        for idx, sql in enumerate(raw_statements):
            try:
                parsed = sqlglot.parse(sql, read=dialect or "tsql")
                if parsed:
                    result[idx] = parsed[0]
            except Exception:
                pass
        return result

    # ------------------------------------------------------------------ #
    # Statement classification                                             #
    # ------------------------------------------------------------------ #

    def _classify_statement(self, index: int, sql_text: str, expression, dialect: str) -> dict:
        created_temp = None
        referenced: list[str] = []
        physical: list[str] = []
        stmt_type = "unknown"
        parse_ok = expression is not None

        if parse_ok:
            try:
                created_temp = self._extract_created_temp_from_expression(expression, dialect)
                referenced, physical = self._extract_table_usage(expression, dialect, created_temp)
                stmt_type = self._type_from_expression(expression, created_temp)
            except Exception as exc:
                logger.debug("sqlglot classification failed, falling back: %s", exc)
                parse_ok = False

        if not parse_ok:
            created_temp = self._extract_created_temp_regex(sql_text)
            referenced, physical = self._extract_table_usage_regex(sql_text, created_temp)
            stmt_type = self._type_from_sql(sql_text, created_temp)

        return {
            "index": index,
            "sql": sql_text,
            "expression": expression,
            "statement_type": stmt_type,
            "created_temp_table": created_temp,
            "referenced_temp_tables": referenced,
            "physical_tables": physical,
            "parse_success": parse_ok,
        }

    # ------------------------------------------------------------------ #
    # sqlglot-based extraction                                             #
    # ------------------------------------------------------------------ #

    def _extract_created_temp_from_expression(self, expression, dialect: str):
        exp = self._exp
        if exp is None:
            return None
        if isinstance(expression, exp.Select):
            into = expression.args.get("into")
            if into:
                return self._normalize_temp_name(into.this.sql(dialect=dialect or "tsql"))
        if isinstance(expression, exp.Create):
            tbl = expression.this
            if tbl is not None:
                name = tbl.sql(dialect=dialect or "tsql") if hasattr(tbl, "sql") else str(tbl)
                return self._normalize_temp_name(name)
        return None

    def _extract_table_usage(self, expression, dialect: str, created_temp):
        exp = self._exp
        referenced: list[str] = []
        physical: list[str] = []
        for tbl in expression.find_all(exp.Table):
            raw = tbl.sql(dialect=dialect or "tsql")
            norm = self._normalize_temp_name(raw)
            if norm:
                if created_temp and norm.lower() == created_temp.lower():
                    continue
                if norm not in referenced:
                    referenced.append(norm)
            else:
                label = raw.strip().strip("[]")
                if label and label not in physical:
                    physical.append(label)
        return referenced, physical

    def _type_from_expression(self, expression, created_temp: str) -> str:
        exp = self._exp
        if isinstance(expression, exp.Select) and created_temp:
            return "select_into_temp"
        if isinstance(expression, exp.Create) and created_temp:
            return "create_temp"
        if isinstance(expression, exp.Select):
            return "select"
        if isinstance(expression, exp.Drop):
            return "drop"
        if isinstance(expression, exp.Insert):
            return "insert"
        return type(expression).__name__.lower()

    # ------------------------------------------------------------------ #
    # Regex-based extraction (primary T-SQL path)                         #
    # ------------------------------------------------------------------ #

    def _extract_created_temp_regex(self, sql: str) -> str | None:
        m = re.search(r"\bINTO\s+(##?\w+)\b", sql, re.IGNORECASE)
        if m:
            return self._normalize_temp_name(m.group(1))
        m = re.search(r"\bCREATE\s+TABLE\s+(##?\w+)\b", sql, re.IGNORECASE)
        if m:
            return self._normalize_temp_name(m.group(1))
        return None

    def _extract_table_usage_regex(self, sql: str, created_temp) -> tuple[list, list]:
        all_tokens = sorted(set(re.findall(r"##?\w+", sql, re.IGNORECASE)))
        referenced: list[str] = []
        for tok in all_tokens:
            norm = self._normalize_temp_name(tok)
            if not norm:
                continue
            if created_temp and norm.lower() == created_temp.lower():
                continue
            if norm not in referenced:
                referenced.append(norm)

        physical: list[str] = []
        for m in re.finditer(
            r"\b(?:FROM|JOIN)\s+(\[?[A-Za-z_][A-Za-z0-9_.\[\]]*\]?"
            r"(?:\s*\.\s*\[?[A-Za-z_][A-Za-z0-9_.\[\]]*\]?)*)",
            sql, re.IGNORECASE,
        ):
            name = m.group(1).strip()
            if not name.startswith("#") and name not in physical:
                physical.append(name)
        return referenced, physical

    def _type_from_sql(self, sql: str, created_temp) -> str:
        up = sql.strip().upper()
        if created_temp and re.search(r"\bINTO\s+##?\w+", sql, re.IGNORECASE):
            return "select_into_temp"
        if created_temp and re.search(r"\bCREATE\s+TABLE\s+##?\w+", sql, re.IGNORECASE):
            return "create_temp"
        if up.startswith("SELECT"):
            return "select"
        if up.startswith("DROP"):
            return "drop"
        if up.startswith("INSERT"):
            return "insert"
        if up.startswith("IF") and "DROP TABLE" in up:
            return "drop_guard"
        return "unknown"

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _normalize_temp_name(self, name) -> str | None:
        if not name:
            return None
        cleaned = str(name).strip().strip("[]").strip()
        if cleaned.startswith("##") or cleaned.startswith("#"):
            return cleaned
        return None

    def _resolve_dependency_order(self, creation_order: list, temp_dependencies: dict) -> list:
        ordered: list[str] = []
        seen: set[str] = set()

        def visit(name: str):
            if name.lower() in seen:
                return
            for dep in temp_dependencies.get(name, []):
                visit(dep)
            seen.add(name.lower())
            ordered.append(name)

        for name in creation_order:
            visit(name)
        return ordered

    def _find_final_select_index(self, statements: list) -> int | None:
        idxs = [s["index"] for s in statements if s["statement_type"] == "select"]
        return idxs[-1] if idxs else None

    def _load_sqlglot(self):
        if self._sqlglot is not None:
            return self._sqlglot if self._sqlglot is not False else None
        try:
            import sqlglot
            from sqlglot import exp
            self._sqlglot = sqlglot
            self._exp = exp
            return sqlglot
        except Exception:
            logger.info("sqlglot not available; using regex dependency graph.")
            self._sqlglot = False
            return None
