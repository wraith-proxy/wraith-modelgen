"""
Contract and project scaffolding.

`draft_contract` reverses the usual flow: instead of a contract driving
introspection, introspection drives a first-draft contract. The draft is
deliberately conservative — every guess is marked with a TODO so a human
confirms the keys before the contract becomes load-bearing.
"""

from __future__ import annotations

import re
from pathlib import Path

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


class ScaffoldError(ValueError):
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


def _quote(text: str) -> str:
    return '"' + text.replace("\n", " ").replace('"', '\\"') + '"'


def _column_block(col: IntrospectedColumn, unique_key: str) -> list[str]:
    lines = [
        f"      - source: {col.name}",
        f"        name: {col.name.lower()}",
    ]
    if col.field_type != "RECORD" and col.mode != "REPEATED":
        lines.append(f"        type: {col.field_type}")
    if col.description:
        lines.append(f"        description: {_quote(col.description)}")
    if col.name == unique_key:
        lines.append("        tests: [not_null, unique]")
    return lines


def draft_contract(
    project: str,
    dataset: str,
    table: str,
    columns: list[IntrospectedColumn],
    name: str | None = None,
) -> str:
    """Render a first-draft contract YAML from an introspected schema."""
    if not columns:
        raise ScaffoldError(
            f"Source table {project}.{dataset}.{table} has no columns."
        )
    event_name = name or derive_event_name(table)
    unique_key = guess_unique_key(columns)
    loaded_at = guess_loaded_at(columns)
    loaded_at_col = next(c for c in columns if c.name == loaded_at)

    lines = [
        'version: "1"',
        "",
        "event:",
        f"  name: {event_name}",
        f"  description: {_quote(f'TODO: what one {event_name} row means.')}",
        f"  unique_key: {unique_key}  # TODO: confirm guess",
        f"  loaded_at_field: {loaded_at}  # TODO: confirm guess",
        "",
        "  source:",
        f"    project: {project}",
        f"    dataset: {dataset}",
        f"    table: {table}",
        "",
        "  raw:",
        "    dataset: raw",
        "    incremental_strategy: merge",
        "    dedup: true",
    ]

    partition_type = _PARTITION_TYPE.get(loaded_at_col.field_type)
    if partition_type:
        lines += [
            "    partition_by:",
            f"      field: {loaded_at}",
            f"      data_type: {partition_type}",
            "      granularity: day",
        ]

    lines += [
        "",
        "  staging:",
        "    dataset: staging",
        "    incremental_strategy: merge",
    ]
    if partition_type:
        lines += [
            "    partition_by:",
            f"      field: {loaded_at.lower()}",
            f"      data_type: {partition_type}",
            "      granularity: day",
        ]
    lines.append("    columns:")
    for col in columns:
        lines += _column_block(col, unique_key)

    return "\n".join(lines) + "\n"


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
            f"{config} already exists — this project is initialised."
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
