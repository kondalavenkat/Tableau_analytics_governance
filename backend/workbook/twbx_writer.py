"""
TWBx Write-back

Deterministic write-back rules:
- Patch the exact XML targets recorded during scan.
- Patch all duplicate active targets recorded for the same logical query.
- Patch the exact XML file inside the package where the SQL was found.
- Never inject new nodes.
"""

import logging
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from lxml import etree
from backend.conversion.sql_refactorer import SQLRefactorer

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


class TWBxWriter:
    def __init__(self):
        self.refactorer = SQLRefactorer()

    def write_back(self, source_path: str, conversions: list[dict], output_path: str = None) -> str:
        output_path = output_path or source_path
        ext = Path(source_path).suffix.lower()

        if ext == ".twbx":
            return self._write_twbx(source_path, conversions, output_path)
        if ext == ".twb":
            return self._write_twb(source_path, conversions, output_path)
        raise ValueError(f"Unsupported file type: {ext}")

    def _write_twbx(self, source_path: str, conversions: list[dict], output_path: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(source_path, "r") as zf:
                zf.extractall(tmp)
                all_names = zf.namelist()

            self._patch_bundle(tmp, conversions)

            tmp_out = output_path + ".tmp"
            with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as zf_out:
                for name in all_names:
                    zf_out.write(os.path.join(tmp, name), arcname=name)

            shutil.move(tmp_out, output_path)

        logger.info("Write-back complete: %s", output_path)
        return output_path

    def _write_twb(self, source_path: str, conversions: list[dict], output_path: str) -> str:
        if os.path.abspath(source_path) != os.path.abspath(output_path):
            shutil.copy2(source_path, output_path)
        base_root = os.path.dirname(output_path)
        self._patch_bundle(base_root, conversions)
        logger.info("Write-back complete: %s", output_path)
        return output_path

    def _patch_bundle(self, base_root: str, conversions: list[dict]):
        xml_cache: dict[tuple, dict] = {}
        nested_archives: dict[str, dict] = {}

        try:
            for conversion in conversions:
                if conversion.get("status") != "converted":
                    continue
                if conversion.get("type") == "Initial SQL":
                    self._patch_initial_sql(base_root, conversion, xml_cache, nested_archives)
                else:
                    self._patch_custom_sql(base_root, conversion, xml_cache, nested_archives)

            for cache_entry in xml_cache.values():
                if cache_entry["dirty"]:
                    cache_entry["tree"].write(
                        cache_entry["path"],
                        pretty_print=True,
                        xml_declaration=True,
                        encoding="utf-8",
                    )

            for archive in nested_archives.values():
                if not archive["dirty"]:
                    archive["tempdir"].cleanup()
                    continue

                tmp_out = archive["outer_path"] + ".tmp"
                with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as zf_out:
                    for name in archive["all_names"]:
                        zf_out.write(os.path.join(archive["tempdir"].name, name), arcname=name)
                shutil.move(tmp_out, archive["outer_path"])
                archive["tempdir"].cleanup()
        finally:
            for archive in nested_archives.values():
                try:
                    archive["tempdir"].cleanup()
                except Exception:
                    pass

    def _patch_initial_sql(self, base_root: str, conversion: dict, xml_cache: dict, nested_archives: dict):
        setup_sql = (conversion.get("setup_sql") or conversion.get("converted_permanent_query") or "").strip().rstrip(";")
        runtime_defs = conversion.get("runtime_temp_definitions") or {}
        token_map = self._build_token_map(conversion)

        for target in conversion.get("targets", []):
            if target.get("target_kind") not in {"initial_sql_attr", "initial_sql_element"}:
                continue
            entry = self._load_xml_target(base_root, target, xml_cache, nested_archives)
            element = self._resolve_xpath(entry["tree"], target.get("xpath"))
            if element is None:
                logger.warning("Initial SQL target not found: %s", target.get("xpath"))
                continue

            if target.get("target_kind") == "initial_sql_attr":
                attr_name = target.get("target_attr_name")
                if attr_name and attr_name in element.attrib:
                    element.attrib[attr_name] = setup_sql
                    self._mark_dirty(entry, nested_archives)
            else:
                element.text = setup_sql
                self._mark_dirty(entry, nested_archives)

        for target in conversion.get("related_runtime_targets", []):
            self._rewrite_runtime_target(base_root, target, runtime_defs, token_map, xml_cache, nested_archives)

    def _patch_custom_sql(self, base_root: str, conversion: dict, xml_cache: dict, nested_archives: dict):
        runtime_sql = (
            conversion.get("runtime_sql")
            or conversion.get("converted_permanent_query")
            or ""
        ).strip().rstrip(";")
        runtime_defs = conversion.get("runtime_temp_definitions") or {}
        token_map = self._build_token_map(conversion)

        for target in conversion.get("targets", []):
            if target.get("target_kind") != "custom_sql_relation":
                continue

            entry = self._load_xml_target(base_root, target, xml_cache, nested_archives)
            relation = self._resolve_xpath(entry["tree"], target.get("xpath"))
            if relation is None:
                logger.warning("Custom SQL target not found: %s", target.get("xpath"))
                continue

            current_sql = (relation.text or "").strip()
            replacement_sql = runtime_sql or self._inline_runtime_sql(current_sql, runtime_defs, token_map)
            if replacement_sql:
                relation.text = replacement_sql.strip().rstrip(";")
                self._mark_dirty(entry, nested_archives)

    def _rewrite_runtime_target(self, base_root: str, target: dict, runtime_defs: dict, token_map: dict, xml_cache: dict, nested_archives: dict):
        if target.get("target_kind") != "custom_sql_relation":
            return

        entry = self._load_xml_target(base_root, target, xml_cache, nested_archives)
        relation = self._resolve_xpath(entry["tree"], target.get("xpath"))
        if relation is None:
            logger.warning("Runtime target not found: %s", target.get("xpath"))
            return

        current_sql = (relation.text or "").strip()
        replacement_sql = self._inline_runtime_sql(current_sql, runtime_defs, token_map)
        if not replacement_sql:
            replacement_sql = self._build_select_runtime(token_map)

        relation.text = replacement_sql.strip().rstrip(";")
        self._mark_dirty(entry, nested_archives)

    def _load_xml_target(self, base_root: str, target: dict, xml_cache: dict, nested_archives: dict):
        cache_key = (
            target.get("source_kind"),
            target.get("source_relpath"),
            target.get("nested_inner_relpath"),
        )
        if cache_key in xml_cache:
            return xml_cache[cache_key]

        source_kind = target.get("source_kind")
        source_relpath = target.get("source_relpath")
        nested_inner_relpath = target.get("nested_inner_relpath")

        if source_kind == "nested_tdsx":
            outer_path = os.path.join(base_root, source_relpath)
            archive = nested_archives.get(source_relpath)
            if archive is None:
                tempdir = tempfile.TemporaryDirectory()
                with zipfile.ZipFile(outer_path, "r") as zf:
                    zf.extractall(tempdir.name)
                    all_names = zf.namelist()
                archive = {
                    "tempdir": tempdir,
                    "outer_path": outer_path,
                    "all_names": all_names,
                    "dirty": False,
                }
                nested_archives[source_relpath] = archive

            xml_path = os.path.join(archive["tempdir"].name, nested_inner_relpath)
            tree = etree.parse(xml_path, etree.XMLParser(remove_blank_text=False))
            xml_cache[cache_key] = {
                "tree": tree,
                "path": xml_path,
                "dirty": False,
                "archive_key": source_relpath,
            }
            return xml_cache[cache_key]

        xml_path = os.path.join(base_root, source_relpath)
        tree = etree.parse(xml_path, etree.XMLParser(remove_blank_text=False))
        xml_cache[cache_key] = {"tree": tree, "path": xml_path, "dirty": False, "archive_key": None}
        return xml_cache[cache_key]

    def _resolve_xpath(self, tree, xpath: str):
        try:
            results = tree.xpath(xpath)
            return results[0] if results else None
        except Exception:
            return None

    def _inline_runtime_sql(self, sql_text: str, runtime_defs: dict[str, str], token_map: dict[str, str]) -> str:
        if not sql_text:
            return ""

        parser_first = self.refactorer.build_runtime_sql_from_definitions(sql_text, runtime_defs)
        if parser_first:
            return parser_first.strip().rstrip(";")

        producer_sql = {token.lower(): body for token, body in runtime_defs.items()}
        pattern = re.compile(
            rf"(?P<kw>\bFROM|\bJOIN)\s+(?P<tbl>##?\w+)(?:\s+(?:AS\s+)?(?P<alias>(?!(?:{_CLAUSE_ALIAS_GUARD})\b)[A-Za-z_]\w*))?",
            re.IGNORECASE,
        )

        def replace_match(match):
            temp_token = match.group("tbl")
            keyword = match.group("kw")
            alias = match.group("alias")
            body = producer_sql.get(temp_token.lower())

            if body is None:
                perm_name = token_map.get(temp_token, self._build_perm_name(temp_token))
                return f"{keyword} {perm_name}" + (f" AS {alias}" if alias else "")

            alias_name = alias or token_map.get(temp_token, self._build_perm_name(temp_token))
            return f"{keyword} (\n{body}\n) AS {alias_name}"

        updated = pattern.sub(replace_match, sql_text)
        updated = self._replace_temp_tokens(updated, token_map)
        return updated.strip().rstrip(";")

    def _build_token_map(self, conversion: dict) -> dict[str, str]:
        tokens = set(conversion.get("temp_table_tokens") or [])
        tokens.update((conversion.get("runtime_temp_definitions") or {}).keys())

        if not tokens:
            for sql_text in [conversion.get("original_temp_query", ""), conversion.get("raw_initial_sql", "")]:
                tokens.update(re.findall(r"##?\w+", sql_text or "", re.IGNORECASE))

        return {token: self._build_perm_name(token) for token in sorted(tokens)}

    def _replace_temp_tokens(self, sql_text: str, token_map: dict[str, str]) -> str:
        updated = sql_text
        for token, perm_name in sorted(token_map.items(), key=lambda item: len(item[0]), reverse=True):
            updated = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])",
                perm_name,
                updated,
                flags=re.IGNORECASE,
            )
        return updated

    def _build_perm_name(self, temp_token: str) -> str:
        return f"perm_{re.sub(r'[^A-Za-z0-9_]', '', temp_token.lstrip('#'))}"

    def _build_select_runtime(self, token_map: dict[str, str]) -> str:
        first_name = next(iter(token_map.values()))
        return f"SELECT * FROM {first_name}"

    def _mark_dirty(self, entry: dict, nested_archives: dict):
        entry["dirty"] = True
        archive_key = entry.get("archive_key")
        if archive_key and archive_key in nested_archives:
            nested_archives[archive_key]["dirty"] = True
