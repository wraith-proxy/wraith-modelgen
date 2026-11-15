"""
Sources generation for tables a project READS but does not own.

An extract contract describes files another process lands (Parquet on
local disk). This mode emits one dbt `sources:` yml per contract: the
external location, freshness on the loaded-at field, a column-set test
over every declared column, and tests mapped from the contract's quality
rules (singular ones select from `{{ source(...) }}`).

Customisation comes from the contract's customProperties:

- `dbtSourceName`: source name (default: the contract name)
- `dbtColumnSetTest`: name of the column-set test macro (default:
  `dbt_expectations.expect_table_columns_to_match_set`); same
  `column_list` argument
- `externalLocation`: written to each table's `meta.external_location`
- `loadedAtField`: source `loaded_at_field`
- `freshnessWarnAfter` / `freshnessErrorAfter`: e.g. `2 days`

Deterministic like the rest of the generator: no paths, no timestamps,
contract order throughout.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from modelgen.contract import (
    ContractError,
    Issue,
    _load_doc,
    deprecation_notices,
    read_contract,
    safe_name,
    select_schema_objects,
)
from modelgen.generator import provenance_header
from modelgen.quality import ModelTests, map_object
from modelgen.schema_mode import implied_tests
from modelgen.yml_mode import custom_properties, emit, slug

COLUMNS_TEST = "dbt_expectations.expect_table_columns_to_match_set"
_PERIODS = {
    "minute": "minute",
    "minutes": "minute",
    "hour": "hour",
    "hours": "hour",
    "day": "day",
    "days": "day",
}


def sources_filename(contract_name: str) -> str:
    """`_<contract-name>__sources.yml`, slugged like the models yml."""
    return f"_{slug(contract_name)}__sources.yml"


def _threshold(prop: str, value: Any) -> dict[str, Any]:
    m = re.fullmatch(r"\s*(\d+)\s+([A-Za-z]+)\s*", str(value))
    if not m or m.group(2).lower() not in _PERIODS:
        raise ContractError(
            f"customProperty {prop} must look like '2 days' (minutes,"
            f" hours or days); got {value!r}."
        )
    if int(m.group(1)) <= 0:
        raise ContractError(
            f"customProperty {prop} must be greater than zero; got {value!r}."
        )
    return {"count": int(m.group(1)), "period": _PERIODS[m.group(2).lower()]}


def _scalar(prop: str, value: Any) -> str | None:
    """A customProperty that must be one string, not a repeated list."""
    if value is None or value == "":
        return None
    if isinstance(value, (list, dict)):
        raise ContractError(
            f"customProperty {prop} must be a single value; got {value!r}."
        )
    return str(value)


def _location(
    obj: dict[str, Any], doc_meta: dict[str, Any], count: int
) -> Any:
    """Per-object externalLocation, else the contract's (one object only).

    `count` is the UNFILTERED object count so `--table` cannot change
    whether a shared location is accepted."""
    own = custom_properties(obj).get("externalLocation")
    if own:
        return own
    shared = doc_meta.get("externalLocation")
    if shared and count > 1:
        raise ContractError(
            f"externalLocation is set at contract level but the contract"
            f" has {count} objects; set it on each object's"
            f" customProperties (table {obj.get('name')!r} has none)."
        )
    return shared


def _table(
    obj: dict[str, Any],
    source: str,
    header: str,
    location: Any,
    columns_test: str = COLUMNS_TEST,
) -> tuple[dict[str, Any], ModelTests]:
    props = obj.get("properties") or []
    found = ModelTests()
    table: dict[str, Any] = {
        "name": obj["name"],
        "description": obj.get("description", ""),
    }
    if location:
        table["meta"] = {"external_location": location}
    found.model_tests.append(
        {columns_test: {"column_list": [str(p["name"]) for p in props]}}
    )
    found.column_tests.update(implied_tests(props))
    map_object(obj, header, found, source=source)
    table["data_tests"] = found.model_tests
    columns = []
    for prop in props:
        col: dict[str, Any] = {
            "name": prop["name"],
            "description": prop.get("description", ""),
        }
        if found.column_tests.get(prop["name"]):
            col["data_tests"] = found.column_tests[prop["name"]]
        columns.append(col)
    if columns:
        table["columns"] = columns
    return table, found


def render_sources(
    contract: Path,
    *,
    tables: list[str] | None = None,
    unmapped: list[str] | None = None,
    notices: list[Issue] | None = None,
) -> dict[str, str]:
    """{relative path: content} for one contract. Nothing is written.

    Quality rules that cannot be mapped are appended to `unmapped` as
    `<table>.<rule>`. Deprecation warnings are appended to `notices`.
    """
    doc = _load_doc(read_contract(contract), str(contract))
    if notices is not None:
        notices.extend(deprecation_notices(doc))
    schemas, object_count = select_schema_objects(doc, tables)
    # Both names become quoted source() arguments in generated tests.
    for obj in schemas:
        safe_name("Schema object name", obj.get("name"))

    cname = str(doc.get("name") or "contract")
    meta = custom_properties(doc)
    source_name = safe_name(
        "dbtSourceName (default: the contract name)",
        _scalar("dbtSourceName", meta.get("dbtSourceName")) or cname,
    )
    columns_test = (
        _scalar("dbtColumnSetTest", meta.get("dbtColumnSetTest"))
        or COLUMNS_TEST
    )
    stamp = provenance_header(cname, doc.get("version"))
    built = [
        _table(
            o,
            source_name,
            stamp,
            _location(o, meta, object_count),
            columns_test,
        )
        for o in schemas
    ]
    source: dict[str, Any] = {
        "name": source_name,
        "description": _description(doc),
    }
    loaded_at = _scalar("loadedAtField", meta.get("loadedAtField"))
    if loaded_at:
        source["loaded_at_field"] = loaded_at
    freshness = {
        key: _threshold(prop, meta[prop])
        for key, prop in (
            ("warn_after", "freshnessWarnAfter"),
            ("error_after", "freshnessErrorAfter"),
        )
        if meta.get(prop)
    }
    if freshness:
        source["freshness"] = freshness
    source["tables"] = [t for t, _ in built]
    return emit(
        stamp,
        sources_filename(cname),
        {"version": 2, "sources": [source]},
        built,
        unmapped,
    )


def _description(doc: dict[str, Any]) -> str:
    d = doc.get("description")
    if isinstance(d, dict):
        return str(d.get("purpose") or "")
    return str(d or "")
