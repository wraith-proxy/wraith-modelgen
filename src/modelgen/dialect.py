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
    display_name: str  # spelling used in user-facing messages

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
        """True when the dialect partitions on a table column. When False
        no `ds` snapshot column is generated and partitionBy is rejected."""
        ...

    def source_location(self, server: dict) -> tuple[str, str]:
        """(project, dataset) for the SourceRef, read off an ODCS server."""
        ...

    def validate_project(self, project: str) -> bool:
        """True when `project` is a plausible first-part source name."""
        ...

    def staging_materializations(self) -> frozenset[str]:
        """Values accepted for modelgenStaging.materialization."""
        ...

    def materialization(self, contract: EventContract, layer: str) -> str:
        """dbt materialization the layer is built with."""
        ...

    def render_yml_config(self, contract: EventContract, layer: str) -> str:
        """Indented body of the schema.yml `config:` block, no trailing
        newline."""
        ...

    def render_source_yml(self, contract: EventContract) -> tuple[str, str]:
        """Extra lines for the sources entry: (after the source name, after
        the table name). Either may be empty. Lines are fully indented."""
        ...

    def supports_encryption(self) -> bool:
        """True when classified columns can be encrypted in staging."""
        ...

    def string_type(self) -> str:
        """The dialect's text type name."""
        ...

    def is_string_type(self, physical_type: str) -> bool:
        """True when `physical_type` is already the dialect's text type."""
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


_BQ_PROJECT = re.compile(r"^[a-z][a-z0-9-]*$")


def _unique_key_yaml(keys: list[str]) -> str:
    """Render a unique_key as a YAML scalar or flow list for schema.yml."""
    if len(keys) == 1:
        return keys[0]
    return "[" + ", ".join(keys) + "]"


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
    display_name = "BigQuery"

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

    def source_location(self, server: dict) -> tuple[str, str]:
        return server.get("project", ""), server.get("dataset", "")

    def validate_project(self, project: str) -> bool:
        return bool(_BQ_PROJECT.match(project))

    def staging_materializations(self) -> frozenset[str]:
        return frozenset({"snapshot", "incremental"})

    def materialization(self, contract: EventContract, layer: str) -> str:
        return "incremental"

    def render_yml_config(self, contract: EventContract, layer: str) -> str:
        if layer == "raw":
            lines = [
                "      materialized: incremental",
                "      incremental_strategy:"
                f" {contract.raw.incremental_strategy}",
                f"      unique_key: {_unique_key_yaml(contract.unique_key)}",
                f"      schema: {contract.raw.dataset}",
            ]
        elif layer == "staging":
            lines = [
                "      materialized: incremental",
                "      incremental_strategy:"
                f" {contract.staging.incremental_strategy}",
                f"      schema: {contract.staging.dataset}",
            ]
        else:
            raise ValueError(f"Unknown layer {layer!r}: use raw or staging.")
        return "\n".join(lines)

    def render_source_yml(self, contract: EventContract) -> tuple[str, str]:
        return f"    project: {contract.source.project}", ""

    def supports_encryption(self) -> bool:
        return True

    def string_type(self) -> str:
        return "STRING"

    def is_string_type(self, physical_type: str) -> bool:
        return physical_type.strip().upper() == "STRING"

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


# DuckDB base type names (aliases included). Parameterised and nested forms
# are accepted by is_valid_physical_type on top of these.
_DUCKDB_TYPES = frozenset(
    {
        "VARCHAR",
        "TEXT",
        "BLOB",
        "BYTEA",
        "BIGINT",
        "INT8",
        "LONG",
        "INTEGER",
        "INT",
        "INT4",
        "SMALLINT",
        "INT2",
        "TINYINT",
        "INT1",
        "HUGEINT",
        "UBIGINT",
        "UINTEGER",
        "USMALLINT",
        "UTINYINT",
        "UHUGEINT",
        "DOUBLE",
        "FLOAT8",
        "FLOAT",
        "FLOAT4",
        "REAL",
        "DECIMAL",
        "NUMERIC",
        "BOOLEAN",
        "BOOL",
        "DATE",
        "TIME",
        "TIMESTAMP",
        "DATETIME",
        "TIMESTAMPTZ",
        "TIMESTAMP_S",
        "TIMESTAMP_MS",
        "TIMESTAMP_NS",
        "INTERVAL",
        "UUID",
        "JSON",
        "BIT",
        "BITSTRING",
    }
)
_DUCKDB_NESTED = re.compile(r"^(LIST|STRUCT|MAP|UNION|ENUM)\s*\(.*\)$", re.S)
_DUCKDB_ARRAY_SUFFIX = re.compile(r"^(?P<inner>.+?)\s*\[\s*\d*\s*\]$", re.S)


class DuckDBDialect:
    """DuckDB, as driven by dbt-duckdb.

    A contract picks it with `servers[].type` of `duckdb`, or of `local`:
    a `local` server with `format: parquet` is the Parquet landing file
    that dbt-duckdb reads through the source's `external_location` meta.
    Models are views unless the contract says `table`. DuckDB has no
    partitions or clustering, so none are emitted and no `ds` column is
    added.
    """

    name = "duckdb"
    display_name = "DuckDB"

    def server_types(self) -> tuple[str, ...]:
        return ("duckdb", "local")

    def valid_physical_types(self) -> frozenset[str]:
        return _DUCKDB_TYPES

    def is_valid_physical_type(self, raw: str) -> bool:
        t = raw.strip().upper()
        if _DUCKDB_NESTED.match(t):
            return True
        m = _DUCKDB_ARRAY_SUFFIX.match(t)
        if m:
            return self.is_valid_physical_type(m.group("inner"))
        base = re.split(r"[(]", t, maxsplit=1)[0].strip()
        return base in self.valid_physical_types()

    def cast_type(self, physical_type: str) -> str:
        return physical_type

    def has_partition_column(self) -> bool:
        return False

    def source_location(self, server: dict) -> tuple[str, str]:
        location = (
            server.get("database")
            or server.get("path")
            or server.get("location")
            or server.get("server")
            or ""
        )
        return str(location), server.get("schema") or "main"

    def validate_project(self, project: str) -> bool:
        return bool(project.strip())

    def staging_materializations(self) -> frozenset[str]:
        return frozenset({"snapshot", "incremental", "view", "table"})

    def materialization(self, contract: EventContract, layer: str) -> str:
        if layer == "raw":
            declared = contract.raw.materialization
        elif layer == "staging":
            declared = contract.staging.materialization
        else:
            raise ValueError(f"Unknown layer {layer!r}: use raw or staging.")
        return "table" if declared == "table" else "view"

    def render_yml_config(self, contract: EventContract, layer: str) -> str:
        cfg = contract.raw if layer == "raw" else contract.staging
        return (
            f"      materialized: {self.materialization(contract, layer)}\n"
            f"      schema: {cfg.dataset}"
        )

    def render_source_yml(self, contract: EventContract) -> tuple[str, str]:
        src = contract.source
        if src.server_format == "parquet" and src.server_type == "local":
            return "", (
                f"        meta:\n          external_location: {src.project}"
            )
        return f"    database: {src.project}", ""

    def supports_encryption(self) -> bool:
        return False

    def string_type(self) -> str:
        return "VARCHAR"

    def is_string_type(self, physical_type: str) -> bool:
        return physical_type.strip().upper() in ("VARCHAR", "TEXT")

    def render_dbt_config(self, contract: EventContract, layer: str) -> str:
        if layer not in ("raw", "staging"):
            raise ValueError(f"Unknown layer {layer!r}: use raw or staging.")
        cfg = contract.raw if layer == "raw" else contract.staging
        return (
            "{{ config(\n"
            f"    materialized='{self.materialization(contract, layer)}',\n"
            f"    schema='{cfg.dataset}'\n"
            ") }}"
        )


_DIALECTS: tuple[Dialect, ...] = (BigQueryDialect(), DuckDBDialect())


def supported_server_types() -> tuple[str, ...]:
    return tuple(sorted(t for d in _DIALECTS for t in d.server_types()))


def get_dialect(server_type: str) -> Dialect:
    for d in _DIALECTS:
        if server_type in d.server_types():
            return d
    supported = ", ".join(supported_server_types())
    raise UnknownDialectError(
        f"Unsupported server type {server_type!r}."
        f" Supported dialects: {supported}."
    )
