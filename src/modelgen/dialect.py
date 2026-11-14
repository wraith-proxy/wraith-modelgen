"""
Warehouse dialect seam.

Everything modelgen knows about one warehouse's SQL and type system sits
behind `Dialect`: which physical types a contract may cast to, how a cast
is spelled, how the dbt `config()` block is rendered, and whether the
dialect partitions on a column. A contract picks its dialect through the
`type` of its ODCS server entry; `get_dialect` resolves it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from modelgen.contract import EventContract


class UnknownDialectError(ValueError):
    """Raised when a server type has no registered dialect."""


class Dialect(Protocol):
    name: str

    def server_types(self) -> tuple[str, ...]:
        """ODCS server `type` values this dialect answers to."""
        ...

    def valid_physical_types(self) -> frozenset[str]:
        """Base type names a contract may declare as physicalType."""
        ...

    def is_valid_physical_type(self, raw: str) -> bool:
        """True when `raw` (parameterised or nested forms included) is a
        physicalType this dialect accepts."""
        ...

    def cast_type(self, physical_type: str) -> str:
        """The type spelling to emit in a cast for a contract physicalType."""
        ...

    def render_dbt_config(self, contract: EventContract, layer: str) -> str:
        """The full `{{ config(...) }}` block for layer 'raw' or 'staging'."""
        ...

    def has_partition_column(self) -> bool:
        """True when the dialect partitions on a table column."""
        ...


def staging_name_for(contract: EventContract, source_column: str) -> str:
    """Look up the staging column name for a given source column. Falls back
    to the source name if the column isn't in the staging contract."""
    for col in contract.staging.columns:
        if col.source == source_column:
            return col.name
    return source_column


def staging_unique_key_dbt(contract: EventContract) -> str:
    keys = [staging_name_for(contract, k) for k in contract.unique_key]
    if len(keys) == 1:
        return f"'{keys[0]}'"
    return "[" + ", ".join(f"'{k}'" for k in keys) + "]"


def render_cluster_by(cols: list[str]) -> str:
    """Render a cluster_by list as a dbt config literal."""
    if not cols:
        return ""
    if len(cols) == 1:
        return f"'{cols[0]}'"
    return "[" + ", ".join(f"'{c}'" for c in cols) + "]"


# Standard types plus the aliases BigQuery accepts (INT/DECIMAL family).
_BQ_TYPES = frozenset(
    {
        "STRING",
        "BYTES",
        "INT64",
        "INT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "TINYINT",
        "BYTEINT",
        "FLOAT64",
        "FLOAT",
        "NUMERIC",
        "DECIMAL",
        "BIGNUMERIC",
        "BIGDECIMAL",
        "BOOL",
        "BOOLEAN",
        "TIMESTAMP",
        "DATE",
        "TIME",
        "DATETIME",
        "GEOGRAPHY",
        "JSON",
        "INTERVAL",
    }
)


class BigQueryDialect:
    name = "bigquery"

    def server_types(self) -> tuple[str, ...]:
        return ("bigquery",)

    def valid_physical_types(self) -> frozenset[str]:
        return _BQ_TYPES

    def is_valid_physical_type(self, raw: str) -> bool:
        t = raw.strip().upper()
        if t.startswith(("ARRAY<", "STRUCT<")) and t.endswith(">"):
            return True
        base = re.split(r"[(<]", t, maxsplit=1)[0].strip()
        return base in self.valid_physical_types()

    def cast_type(self, physical_type: str) -> str:
        # BigQuery casts to the declared type verbatim.
        return physical_type

    def has_partition_column(self) -> bool:
        return True

    def render_dbt_config(self, contract: EventContract, layer: str) -> str:
        """Pre-built so the SQL template doesn't need to juggle conditional
        commas."""
        if layer == "raw":
            layer_cfg = contract.raw
            unique_key_dbt = contract.unique_key_dbt
        elif layer == "staging":
            layer_cfg = contract.staging
            unique_key_dbt = (
                ""
                if contract.staging.is_snapshot
                else staging_unique_key_dbt(contract)
            )
        else:
            raise ValueError(f"Unknown layer {layer!r}: use raw or staging.")
        partition_by = layer_cfg.partition_by
        lines = [
            "    materialized='incremental'",
            f"    incremental_strategy='{layer_cfg.incremental_strategy}'",
        ]
        # insert_overwrite replaces whole partitions and never reads
        # unique_key.
        if unique_key_dbt:
            lines.append(f"    unique_key={unique_key_dbt}")
        lines.append(f"    schema='{layer_cfg.dataset}'")
        if partition_by is not None:
            lines.append(
                f"    partition_by={{"
                f"'field': '{partition_by.field}', "
                f"'data_type': '{partition_by.data_type}', "
                f"'granularity': '{partition_by.granularity}'"
                f"}}"
            )
        if layer_cfg.cluster_by:
            lines.append(
                f"    cluster_by={render_cluster_by(layer_cfg.cluster_by)}"
            )
        body = ",\n".join(lines)
        return "{{ config(\n" + body + "\n) }}"


_DIALECTS: tuple[Dialect, ...] = (BigQueryDialect(),)


def get_dialect(server_type: str) -> Dialect:
    for d in _DIALECTS:
        if server_type in d.server_types():
            return d
    supported = ", ".join(
        sorted(t for d in _DIALECTS for t in d.server_types())
    )
    raise UnknownDialectError(
        f"Unsupported server type {server_type!r}."
        f" Supported dialects: {supported}."
    )
