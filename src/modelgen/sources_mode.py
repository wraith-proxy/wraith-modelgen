"""
Sources generation for tables a project READS but does not own.

An extract contract describes files another process lands (Parquet on
local disk). This mode emits one dbt `sources:` yml per contract: the
external location, freshness on the loaded-at field, a column-set test
over every declared column, and tests mapped from the contract's quality
rules (singular ones select from `{{ source(...) }}`).

Customisation comes from the contract's customProperties:

- `dbtSourceName`: source name (default: the contract name)
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

import yaml

from modelgen.contract import ContractError, _load_doc
from modelgen.generator import provenance_header
from modelgen.quality import ModelTests, map_object
from modelgen.schema_mode import _meta, implied_tests

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
    slug = re.sub(r"[^a-z0-9]+", "_", contract_name.lower()).strip("_")
    return f"_{slug}__sources.yml"


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
    own = _meta(obj).get("externalLocation")
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
    obj: dict[str, Any], source: str, header: str, location: Any
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
        {COLUMNS_TEST: {"column_list": [str(p["name"]) for p in props]}}
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
) -> dict[str, str]:
    """{relative path: content} for one contract. Nothing is written.

    Quality rules that cannot be mapped are appended to `unmapped` as
    `<table>.<rule>`.
    """
    doc = _load_doc(contract.read_text(), str(contract))
    schemas = doc.get("schema") or []
    if not schemas:
        raise ContractError(
            "Contract declares no schema objects. Add at least one"
            " entry under schema:."
        )
    names = [str(o.get("name")) for o in schemas]
    object_count = len(schemas)
    if tables is not None:
        unknown = [t for t in tables if t not in names]
        if unknown:
            raise ContractError(
                f"Unknown table(s): {', '.join(unknown)}. Objects in"
                f" this contract: {', '.join(names)}."
            )
        schemas = [o for o in schemas if str(o["name"]) in tables]

    cname = str(doc.get("name") or "contract")
    meta = _meta(doc)
    source_name = _scalar("dbtSourceName", meta.get("dbtSourceName")) or cname
    stamp = provenance_header(cname, doc.get("version"))
    built = [
        _table(o, source_name, stamp, _location(o, meta, object_count))
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
    body = yaml.safe_dump(
        {"version": 2, "sources": [source]},
        sort_keys=False,
        allow_unicode=True,
        width=79,
    )
    files = {sources_filename(cname): f"# {stamp}\n" + body}
    for _, found in built:
        files.update(found.singular)
        if unmapped is not None:
            unmapped.extend(found.unmapped)
    return files


def _description(doc: dict[str, Any]) -> str:
    d = doc.get("description")
    if isinstance(d, dict):
        return str(d.get("purpose") or "")
    return str(d or "")
