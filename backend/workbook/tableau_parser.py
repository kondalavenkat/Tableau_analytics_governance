"""
Tableau Parser

Deterministic extraction rules:
- Record the exact XML targets where Initial SQL and Custom SQL were found.
- Group duplicate active nodes into one logical entry with many targets.
- Preserve source-file information so write-back can patch the exact XML file
  inside a .twb / .twbx package instead of guessing by SQL text.
"""

import hashlib
import logging
import os
import re
import tempfile
import zipfile

from lxml import etree

logger = logging.getLogger(__name__)

_INITIAL_SQL_ATTR_KEYWORDS = [
    "initial-sql",
    "initial_sql",
    "one-time-sql",
    "onetime-sql",
]


class TableauParser:
    def __init__(self, file_path: str, datasource_paths: list[str] = None, package_root: str = None):
        self.file_path = file_path
        self.datasource_paths = datasource_paths or []
        self.package_root = package_root or os.path.dirname(file_path)
        self.tree = None
        self.root = None
        self._temp_dirs: list[tempfile.TemporaryDirectory] = []

    def parse(self):
        try:
            self.tree = etree.parse(self.file_path)
            self.root = self.tree.getroot()
            logger.info("Parsed XML: %s", self.file_path)
        except Exception as exc:
            logger.error("Failed to parse XML: %s", exc)
            raise

    def cleanup(self):
        for td in self._temp_dirs:
            try:
                td.cleanup()
            except Exception:
                pass
        self._temp_dirs.clear()

    # ------------------------------------------------------------------ #
    # Custom SQL                                                         #
    # ------------------------------------------------------------------ #

    def extract_custom_sql(self) -> list[dict]:
        entries_by_key: dict[tuple, dict] = {}

        for source in self._iter_xml_sources(include_primary=True):
            tree = etree.parse(source["xml_path"])
            root = tree.getroot()

            for relation in root.xpath('//*[local-name()="relation" and @type="text"]'):
                sql_text = relation.text
                if not sql_text or not sql_text.strip():
                    continue

                datasource_name = self._find_datasource_name(relation)
                normalized = self._normalise_sql(sql_text)
                sql_hash = self._hash_sql(normalized)
                key = ("Custom SQL", datasource_name, sql_hash)

                target = self._build_target(
                    tree=tree,
                    source=source,
                    element=relation,
                    target_kind="custom_sql_relation",
                    datasource_name=datasource_name,
                    relation_name=relation.get("name") or relation.get("table") or "Unnamed",
                    connection_ref=relation.get("connection") or self._find_connection_ref(relation),
                )

                if key not in entries_by_key:
                    entries_by_key[key] = {
                        "datasource": datasource_name,
                        "name": relation.get("name") or relation.get("table") or "Unnamed",
                        "query": normalized,
                        "query_hash": sql_hash,
                        "type": "Custom SQL",
                        "targets": [],
                        "source_files": [],
                    }

                self._append_target(entries_by_key[key], target)
                self._append_source_file(entries_by_key[key], source)

        results = list(entries_by_key.values())
        logger.info("Total Custom SQL entries: %d", len(results))
        return results

    # ------------------------------------------------------------------ #
    # Initial SQL                                                        #
    # ------------------------------------------------------------------ #

    def extract_initial_sql(self) -> list[dict]:
        entries_by_key: dict[tuple, dict] = {}

        for source in self._iter_xml_sources(include_primary=True):
            tree = etree.parse(source["xml_path"])
            root = tree.getroot()

            for element in root.iter():
                for attr_name, attr_val in element.attrib.items():
                    if not any(keyword in attr_name.lower() for keyword in _INITIAL_SQL_ATTR_KEYWORDS):
                        continue
                    if not attr_val or not attr_val.strip():
                        continue

                    datasource_name = self._find_datasource_name(element)
                    normalized = self._normalise_sql(attr_val)
                    sql_hash = self._hash_sql(normalized)
                    key = ("Initial SQL", datasource_name, sql_hash)

                    target = self._build_target(
                        tree=tree,
                        source=source,
                        element=element,
                        target_kind="initial_sql_attr",
                        datasource_name=datasource_name,
                        target_attr_name=attr_name,
                        connection_ref=self._find_connection_ref(element),
                    )

                    if key not in entries_by_key:
                        entries_by_key[key] = {
                            "datasource": datasource_name,
                            "name": f"Initial SQL ({os.path.basename(source['display_name'])})",
                            "query": normalized,
                            "query_hash": sql_hash,
                            "type": "Initial SQL",
                            "targets": [],
                            "source_files": [],
                        }

                    self._append_target(entries_by_key[key], target)
                    self._append_source_file(entries_by_key[key], source)

            for element in root.xpath('//*[local-name()="initial-sql" or local-name()="initial_sql"]'):
                text = element.text or ""
                if not text.strip():
                    continue

                datasource_name = self._find_datasource_name(element)
                normalized = self._normalise_sql(text)
                sql_hash = self._hash_sql(normalized)
                key = ("Initial SQL", datasource_name, sql_hash)

                target = self._build_target(
                    tree=tree,
                    source=source,
                    element=element,
                    target_kind="initial_sql_element",
                    datasource_name=datasource_name,
                    connection_ref=self._find_connection_ref(element),
                )

                if key not in entries_by_key:
                    entries_by_key[key] = {
                        "datasource": datasource_name,
                        "name": f"Initial SQL ({os.path.basename(source['display_name'])})",
                        "query": normalized,
                        "query_hash": sql_hash,
                        "type": "Initial SQL",
                        "targets": [],
                        "source_files": [],
                    }

                self._append_target(entries_by_key[key], target)
                self._append_source_file(entries_by_key[key], source)

        results = list(entries_by_key.values())
        logger.info("Total Initial SQL entries: %d", len(results))
        return results

    # ------------------------------------------------------------------ #
    # Compatibility extractors                                           #
    # ------------------------------------------------------------------ #

    def extract_datasources(self) -> list[dict]:
        datasources = []
        for match in self.root.xpath("//datasource"):
            ds_name = match.get("name") or match.get("caption") or "Unnamed Datasource"
            connections = []
            for conn in match.xpath(".//connection"):
                conn_type = conn.get("class")
                if not conn_type:
                    continue
                connections.append({
                    "type": conn_type,
                    "dbname": conn.get("dbname"),
                    "server": conn.get("server"),
                    "user": conn.get("username"),
                })
            datasources.append({
                "name": ds_name,
                "has_connection": match.get("hasconnection"),
                "connections": connections,
            })
        return datasources

    def extract_tables(self) -> list[dict]:
        tables = []
        for relation in self.root.xpath('//relation[@type="table"]'):
            tables.append({
                "datasource": self._find_datasource_name(relation),
                "table_name": relation.get("table"),
            })
        return tables

    def extract_calculations(self) -> list[dict]:
        calcs = []
        for column in self.root.xpath("//column"):
            calc_child = column.find("calculation")
            if calc_child is not None:
                formula = calc_child.get("formula")
                if formula:
                    calcs.append({
                        "name": column.get("name") or column.get("caption"),
                        "formula": formula,
                    })
        return calcs

    def extract_worksheets(self) -> list[str]:
        return [sheet.get("name") for sheet in self.root.xpath("//worksheet")]

    # ------------------------------------------------------------------ #
    # Source discovery                                                   #
    # ------------------------------------------------------------------ #

    def _iter_xml_sources(self, include_primary: bool):
        if include_primary:
            yield self._build_xml_source(self.file_path, display_name=self.file_path)

        for ds in self.datasource_paths:
            if ds.lower().endswith(".tds"):
                yield self._build_xml_source(ds, display_name=ds)
            elif ds.lower().endswith(".tdsx"):
                td = tempfile.TemporaryDirectory()
                self._temp_dirs.append(td)
                try:
                    with zipfile.ZipFile(ds, "r") as zf:
                        zf.extractall(td.name)
                    for root_dir, _, files in os.walk(td.name):
                        for filename in files:
                            if filename.lower().endswith(".tds"):
                                xml_path = os.path.join(root_dir, filename)
                                yield self._build_nested_tdsx_source(ds, td.name, xml_path)
                except Exception as exc:
                    logger.warning("Failed to unzip nested .tdsx %s: %s", ds, exc)

    def _build_xml_source(self, xml_path: str, display_name: str):
        return {
            "source_kind": "xml",
            "display_name": display_name,
            "xml_path": xml_path,
            "source_relpath": self._relative_to_package_root(xml_path),
            "nested_inner_relpath": None,
        }

    def _build_nested_tdsx_source(self, outer_tdsx_path: str, extracted_root: str, xml_path: str):
        return {
            "source_kind": "nested_tdsx",
            "display_name": f"{outer_tdsx_path}::{xml_path}",
            "xml_path": xml_path,
            "source_relpath": self._relative_to_package_root(outer_tdsx_path),
            "nested_inner_relpath": os.path.relpath(xml_path, extracted_root),
        }

    # ------------------------------------------------------------------ #
    # Target helpers                                                     #
    # ------------------------------------------------------------------ #

    def _build_target(
        self,
        tree,
        source: dict,
        element,
        target_kind: str,
        datasource_name: str,
        target_attr_name: str = None,
        relation_name: str = None,
        connection_ref: str = None,
    ):
        return {
            "target_kind": target_kind,
            "source_kind": source["source_kind"],
            "source_relpath": source["source_relpath"],
            "nested_inner_relpath": source.get("nested_inner_relpath"),
            "xpath": tree.getpath(element),
            "target_attr_name": target_attr_name,
            "datasource": datasource_name,
            "relation_name": relation_name,
            "connection_ref": connection_ref or "",
        }

    def _append_target(self, entry: dict, target: dict):
        targets = entry.setdefault("targets", [])
        target_signature = (
            target["source_kind"],
            target["source_relpath"],
            target.get("nested_inner_relpath"),
            target["target_kind"],
            target["xpath"],
            target.get("target_attr_name"),
        )
        if any(
            (
                existing["source_kind"],
                existing["source_relpath"],
                existing.get("nested_inner_relpath"),
                existing["target_kind"],
                existing["xpath"],
                existing.get("target_attr_name"),
            ) == target_signature
            for existing in targets
        ):
            return
        targets.append(target)

    def _append_source_file(self, entry: dict, source: dict):
        source_files = entry.setdefault("source_files", [])
        display_name = source["display_name"]
        if display_name not in source_files:
            source_files.append(display_name)

    # ------------------------------------------------------------------ #
    # XML ancestry helpers                                               #
    # ------------------------------------------------------------------ #

    def _find_datasource_name(self, element) -> str:
        ancestor = element
        while ancestor is not None:
            if ancestor.tag.endswith("datasource"):
                return ancestor.get("name") or ancestor.get("caption") or "Unknown"
            ancestor = ancestor.getparent()
        return "Unknown"

    def _find_connection_ref(self, element) -> str:
        if element is None:
            return ""
        if element.get("connection"):
            return element.get("connection")
        ancestor = element
        while ancestor is not None:
            if ancestor.tag.endswith("connection"):
                return ancestor.get("name") or ancestor.get("class") or ""
            ancestor = ancestor.getparent()
        return ""

    # ------------------------------------------------------------------ #
    # Generic helpers                                                    #
    # ------------------------------------------------------------------ #

    def _relative_to_package_root(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.package_root)
        except Exception:
            return os.path.basename(path)

    def _normalise_sql(self, sql_text: str) -> str:
        cleaned = self._strip_sql_comments(sql_text or "")
        return re.sub(r"\s+", " ", cleaned.strip())

    def _hash_sql(self, sql_text: str) -> str:
        return hashlib.sha256(sql_text.encode("utf-8")).hexdigest()

    def _strip_sql_comments(self, sql_text: str) -> str:
        result = []
        i = 0
        in_single_quote = False
        in_double_quote = False
        length = len(sql_text)

        while i < length:
            char = sql_text[i]
            nxt = sql_text[i + 1] if i + 1 < length else ""

            if not in_single_quote and not in_double_quote:
                if char == "-" and nxt == "-":
                    i += 2
                    while i < length and sql_text[i] not in "\r\n":
                        i += 1
                    continue
                if char == "/" and nxt == "*":
                    i += 2
                    while i + 1 < length and not (sql_text[i] == "*" and sql_text[i + 1] == "/"):
                        i += 1
                    i += 2
                    continue

            if char == "'" and not in_double_quote:
                result.append(char)
                if in_single_quote and nxt == "'":
                    result.append(nxt)
                    i += 2
                    continue
                in_single_quote = not in_single_quote
                i += 1
                continue

            if char == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
                result.append(char)
                i += 1
                continue

            result.append(char)
            i += 1

        return "".join(result)
