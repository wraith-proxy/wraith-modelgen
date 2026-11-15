"""
Contract and project scaffolding.

`draft_contract` reverses the usual flow: instead of a contract driving
introspection, introspection drives a first-draft contract. The draft is
deliberately conservative: every guess is marked with a TODO so a human
confirms the keys before the contract becomes load-bearing.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import yaml

from modelgen.contract import ODCS_API_VERSION
from modelgen.errors import ModelgenError
from modelgen.introspect import IntrospectedColumn

_KEY_HINT = re.compile(r"(^|_)(id|key|guid|uuid)$", re.IGNORECASE)
_TIME_HINT = re.compile(
    r"received|loaded|created|ingested|inserted|updated|_at$|_ts$|_time$",
    re.IGNORECASE,
)
_TIME_TYPES = {"TIMESTAMP", "DATETIME", "DATE"}
_PARTITION_TYPE = {
    "TIMESTAMP": "timestamp",
    "DATETIME": "datetime",
    "DATE": "date",
}

# Best-effort BigQuery -> ODCS logicalType. ODCS logicalType is one of
# string/date/number/integer/object/array/boolean.
_LOGICAL_TYPE = {
    "STRING": "string",
    "BYTES": "string",
    "INT64": "integer",
    "INTEGER": "integer",
    "NUMERIC": "number",
    "BIGNUMERIC": "number",
    "FLOAT64": "number",
    "BOOL": "boolean",
    "BOOLEAN": "boolean",
    "TIMESTAMP": "date",
    "DATETIME": "date",
    "DATE": "date",
    "TIME": "date",
    "RECORD": "object",
    "JSON": "object",
}


def _logical_type(col: IntrospectedColumn) -> str:
    if col.mode == "REPEATED":
        return "array"
    return _LOGICAL_TYPE.get(col.field_type, "string")


class ScaffoldError(ModelgenError, ValueError):
    """Raised when scaffolding cannot proceed."""


def guess_unique_key(columns: list[IntrospectedColumn]) -> str:
    for col in columns:
        if _KEY_HINT.search(col.name):
            return col.name
    return columns[0].name


def guess_loaded_at(columns: list[IntrospectedColumn]) -> str:
    time_cols = [c for c in columns if c.field_type in _TIME_TYPES]
    for col in time_cols:
        if _TIME_HINT.search(col.name):
            return col.name
    if time_cols:
        return time_cols[0].name
    return columns[0].name


def derive_event_name(table: str) -> str:
    name = re.sub(r"[^a-z0-9_]", "_", table.lower())
    if not re.match(r"^[a-z]", name):
        name = f"e_{name}"
    return name


def _property(col: IntrospectedColumn, unique_key: str) -> dict:
    prop: dict = {
        "name": col.name.lower(),
        "physicalName": col.name,
        "logicalType": _logical_type(col),
    }
    if col.field_type != "RECORD" and col.mode != "REPEATED":
        prop["physicalType"] = col.field_type
    if col.description:
        prop["description"] = col.description
    if col.name == unique_key:
        prop["primaryKey"] = True
        prop["primaryKeyPosition"] = 1
        prop["required"] = True
        prop["unique"] = True
    return prop


def draft_contract(
    project: str,
    dataset: str,
    table: str,
    columns: list[IntrospectedColumn],
    name: str | None = None,
) -> str:
    """Render a first-draft ODCS contract YAML from an introspected schema."""
    if not columns:
        raise ScaffoldError(
            f"Source table {project}.{dataset}.{table} has no columns."
        )
    event_name = name or derive_event_name(table)
    unique_key = guess_unique_key(columns)
    loaded_at = guess_loaded_at(columns)
    loaded_at_col = next(c for c in columns if c.name == loaded_at)

    raw_cfg: dict = {
        "dataset": "raw",
        "incrementalStrategy": "merge",
        "dedup": True,
    }
    # Staging is a ds snapshot, which derives its own partitioning.
    staging_cfg: dict = {
        "dataset": "staging",
        "materialization": "snapshot",
    }
    partition_type = _PARTITION_TYPE.get(loaded_at_col.field_type)
    if partition_type:
        raw_cfg["partitionBy"] = {
            "field": loaded_at,
            "dataType": partition_type,
            "granularity": "day",
        }

    doc = {
        "version": "1.0.0",
        "kind": "DataContract",
        "apiVersion": ODCS_API_VERSION,
        "id": str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{project}.{dataset}.{table}")
        ),
        "status": "draft",
        "name": event_name,
        "servers": [
            {
                "server": dataset,
                "type": "bigquery",
                "project": project,
                "dataset": dataset,
            }
        ],
        "schema": [
            {
                "name": event_name,
                "physicalName": table,
                "physicalType": "table",
                "description": f"TODO: what one {event_name} row means.",
                "customProperties": [
                    {
                        "property": "modelgenLoadedAtField",
                        "value": loaded_at,
                    },
                    {"property": "modelgenRaw", "value": raw_cfg},
                    {"property": "modelgenStaging", "value": staging_cfg},
                ],
                "properties": [_property(col, unique_key) for col in columns],
            }
        ],
    }

    header = (
        f"# Draft ODCS contract for {project}.{dataset}.{table}.\n"
        "# TODO: confirm the guessed primaryKey and modelgenLoadedAtField"
        " before generating.\n"
        "# TODO: classify personal columns (classification: pii | psi) and"
        " set modelgenSubjectField\n"
        "#       to the column identifying the data subject. Classified"
        " columns are encrypted\n"
        "#       into staging and erasable by crypto-shred; unclassified"
        " ones are not.\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False, width=88)


_MODELGEN_YML = """\
# modelgen project configuration. All paths relative to this file.
# Every key is optional; the values below are the defaults.

contracts: contracts/*.yml
raw_output: models/raw
staging_output: models/staging
"""


def init_project(root: Path) -> list[Path]:
    """Create the standard modelgen layout. Returns created paths."""
    config = root / ".modelgen.yml"
    if config.exists():
        raise ScaffoldError(
            f"{config} already exists. This project is initialised."
        )
    created: list[Path] = []
    root.mkdir(parents=True, exist_ok=True)
    config.write_text(_MODELGEN_YML)
    created.append(config)
    for sub in ["contracts", "models/raw", "models/staging"]:
        d = root / sub
        if not d.exists():
            d.mkdir(parents=True)
            created.append(d)
    return created
