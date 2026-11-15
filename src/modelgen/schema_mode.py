"""
Schema-only generation for tables a project PRODUCES.

Raw and staging generation assume event ingestion: a loaded-at column per
table and a `sources:` block of landed events. A contract for a produced
table (CDP, FDP, marts) has neither. This mode reads only the schema
objects: it emits one dbt `models:` yml per contract, with enforced
column types, constraints and key tests, and no SQL.

Deterministic like the rest of the generator: no paths, no timestamps,
contract order throughout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modelgen.contract import (
    ContractError,
    Issue,
    _load_doc,
    _select_server,
    deprecation_notices,
    read_contract,
    safe_name,
    select_schema_objects,
)
from modelgen.dialect import get_dialect
from modelgen.generator import provenance_header
from modelgen.quality import ModelTests, map_object
from modelgen.yml_mode import custom_properties, emit, slug

COMPOUND_TEST = "dbt_expectations.expect_compound_columns_to_be_unique"


def schema_filename(contract_name: str) -> str:
    """`_<contract-name>__models.yml`, name lower-cased, non-alphanumerics
    collapsed to one underscore."""
    return f"_{slug(contract_name)}__models.yml"


def implied_tests(props: list[dict[str, Any]]) -> dict[str, list[str]]:
    """not_null for required/key properties, unique for a lone primaryKey."""
    keys = [p for p in props if p.get("primaryKey")]
    out: dict[str, list[str]] = {}
    for prop in props:
        is_key = bool(prop.get("primaryKey"))
        tests: list[str] = []
        if prop.get("required") or is_key:
            tests.append("not_null")
        # primary_key is not a constraint; uniqueness is a test.
        if is_key and len(keys) == 1:
            tests.append("unique")
        out[str(prop["name"])] = tests
    return out


def _column(
    prop: dict[str, Any], dialect: Any
) -> tuple[dict[str, Any], list[str], bool]:
    if not prop.get("physicalType"):
        raise ContractError(
            f"Column {prop.get('name')!r} has no physicalType;"
            " schema mode needs it for the enforced data_type."
        )
    col: dict[str, Any] = {
        "name": prop["name"],
        "description": prop.get("description", ""),
        "data_type": dialect.cast_type(str(prop["physicalType"])),
    }
    is_key = bool(prop.get("primaryKey"))
    required = bool(prop.get("required")) or is_key
    tests: list[str] = []
    if required:
        col["constraints"] = [{"type": "not_null"}]
        tests.append("not_null")
    return col, tests, is_key


def _model(
    obj: dict[str, Any], dialect: Any, header: str
) -> tuple[dict[str, Any], ModelTests]:
    props = obj.get("properties") or []
    keys = [p for p in props if p.get("primaryKey")]
    compound = len(keys) > 1
    columns = []
    found = ModelTests()
    for prop in props:
        col, tests, is_key = _column(prop, dialect)
        if is_key and not compound:
            tests.append("unique")
        found.column_tests[col["name"]] = tests
        columns.append(col)

    model: dict[str, Any] = {
        "name": obj["name"],
        "description": obj.get("description", ""),
        "config": {"contract": {"enforced": True}},
    }
    meta = custom_properties(obj)
    if meta:
        model["meta"] = meta
    if compound:
        ordered = sorted(keys, key=lambda p: p.get("primaryKeyPosition", 0))
        found.model_tests.append(
            {COMPOUND_TEST: {"column_list": [p["name"] for p in ordered]}}
        )
    map_object(obj, header, found)
    if found.model_tests:
        model["data_tests"] = found.model_tests
    for col in columns:
        if found.column_tests.get(col["name"]):
            col["data_tests"] = found.column_tests[col["name"]]
    model["columns"] = columns
    return model, found


def render_schema(
    contract: Path,
    *,
    server: str | None = None,
    tables: list[str] | None = None,
    unmapped: list[str] | None = None,
    notices: list[Issue] | None = None,
) -> dict[str, str]:
    """{relative path: content} for one contract. Nothing is written.

    Besides the models yml, singular tests from quality rules appear
    under `tests/generated/`. Quality rules that cannot be mapped are
    appended to `unmapped` as `<model>.<rule>`. Deprecation warnings are
    appended to `notices`.
    """
    doc = _load_doc(read_contract(contract), str(contract))
    if notices is not None:
        notices.extend(deprecation_notices(doc))
    schemas, _ = select_schema_objects(doc, tables)
    # The name becomes a test filename and a quoted ref() argument.
    for obj in schemas:
        safe_name("Schema object name", obj.get("name"))

    chosen = _select_server(doc.get("servers") or [], server)
    dialect = get_dialect(str(chosen["type"]))

    cname = str(doc.get("name") or "contract")
    stamp = provenance_header(cname, doc.get("version"))
    built = [_model(o, dialect, stamp) for o in schemas]
    return emit(
        stamp,
        schema_filename(cname),
        {"version": 2, "models": [m for m, _ in built]},
        built,
        unmapped,
    )
