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

import re
from pathlib import Path
from typing import Any

import yaml

from modelgen import __version__
from modelgen.contract import ContractError, _load_doc, _select_server
from modelgen.dialect import get_dialect
from modelgen.quality import ModelTests, map_object

COMPOUND_TEST = "dbt_expectations.expect_compound_columns_to_be_unique"


def schema_filename(contract_name: str) -> str:
    """`_<contract-name>__models.yml`, name lower-cased, non-alphanumerics
    collapsed to one underscore."""
    slug = re.sub(r"[^a-z0-9]+", "_", contract_name.lower()).strip("_")
    return f"_{slug}__models.yml"


def _meta(obj: dict[str, Any]) -> dict[str, Any]:
    """customProperties ({property, value} list) as a mapping.

    A property named more than once (ODCS allows it, and real contracts
    use it, e.g. several `consumes`) becomes a list of its values in
    declaration order, so nothing is silently dropped.
    """
    seen: dict[str, list[Any]] = {}
    for cp in obj.get("customProperties") or []:
        if "property" in cp:
            seen.setdefault(str(cp["property"]), []).append(cp.get("value"))
    return {k: v[0] if len(v) == 1 else v for k, v in seen.items()}


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
    # primary_key is not a constraint here; uniqueness is a test.
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
    meta = _meta(obj)
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
) -> dict[str, str]:
    """{relative path: content} for one contract. Nothing is written.

    Besides the models yml, singular tests from quality rules appear
    under `tests/generated/`. Quality rules that cannot be mapped are
    appended to `unmapped` as `<model>.<rule>`.
    """
    doc = _load_doc(contract.read_text(), str(contract))
    schemas = doc.get("schema") or []
    if not schemas:
        raise ContractError(
            "Contract declares no schema objects. Add at least one"
            " entry under schema:."
        )
    names = [str(o.get("name")) for o in schemas]
    if tables is not None:
        unknown = [t for t in tables if t not in names]
        if unknown:
            raise ContractError(
                f"Unknown table(s): {', '.join(unknown)}. Objects in"
                f" this contract: {', '.join(names)}."
            )
        schemas = [o for o in schemas if str(o["name"]) in tables]

    chosen = _select_server(doc.get("servers") or [], server)
    dialect = get_dialect(str(chosen["type"]))

    cname = str(doc.get("name") or "contract")
    stamp = (
        f"GENERATED by modelgen {__version__} from {cname}"
        f" {doc.get('version', '')}. Do not edit."
    )
    built = [_model(o, dialect, stamp) for o in schemas]
    body = yaml.safe_dump(
        {"version": 2, "models": [m for m, _ in built]},
        sort_keys=False,
        allow_unicode=True,
        width=79,
    )
    files = {schema_filename(cname): f"# {stamp}\n" + body}
    for _, found in built:
        files.update(found.singular)
        if unmapped is not None:
            unmapped.extend(found.unmapped)
    return files
