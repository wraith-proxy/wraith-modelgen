"""
Semantic contract linting.

Parsing (contract.py) catches structural problems: missing blocks, wrong
shapes. Linting catches the semantic ones that would otherwise surface as
broken SQL in dbt hours later: a typo'd incremental strategy, a cast to a
type BigQuery has never heard of, a staging unique_key pointing at a column
the staging model doesn't select.

All findings are collected in one pass — fix everything at once rather
than playing whack-a-mole one error per run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from modelgen.contract import EventContract, StagingConfig

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_PROJECT = re.compile(r"^[a-z][a-z0-9-]*$")

_INCREMENTAL_STRATEGIES = {"merge", "insert_overwrite", "microbatch"}
_PARTITION_DATA_TYPES = {"timestamp", "date", "datetime", "int64"}
_PARTITION_GRANULARITIES = {"hour", "day", "month", "year"}

# Standard types plus the aliases BigQuery accepts (INT/DECIMAL family).
_BQ_TYPES = {
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


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    code: str
    message: str

    @property
    def is_error(self) -> bool:
        return self.severity is Severity.ERROR


def _valid_bq_type(raw: str) -> bool:
    t = raw.strip().upper()
    if t.startswith(("ARRAY<", "STRUCT<")) and t.endswith(">"):
        return True
    base = re.split(r"[(<]", t, maxsplit=1)[0].strip()
    return base in _BQ_TYPES


def lint_contract(contract: EventContract) -> list[Finding]:
    """Run every semantic check against a parsed contract.

    Returns all findings, errors and warnings together, ordered by the
    section of the contract they refer to.
    """
    findings: list[Finding] = []
    err = _appender(findings, Severity.ERROR)

    if not _EVENT_NAME.match(contract.name):
        err(
            "invalid-event-name",
            f"event.name {contract.name!r} must match"
            " ^[a-z][a-z0-9_]*$ — it becomes a dbt model name"
            " and a filename.",
        )

    if not _PROJECT.match(contract.source.project):
        err(
            "invalid-source-project",
            f"source.project {contract.source.project!r} is not a"
            " valid GCP project id.",
        )
    for label, value in [
        ("dataset", contract.source.dataset),
        ("table", contract.source.table),
    ]:
        if not _IDENTIFIER.match(value):
            err(
                f"invalid-source-{label}",
                f"source.{label} {value!r} is not a valid BigQuery"
                " identifier.",
            )

    for key in contract.unique_key:
        if not _IDENTIFIER.match(key):
            err(
                "invalid-unique-key",
                f"unique_key column {key!r} is not a valid identifier.",
            )
    if len(contract.unique_key) != len(set(contract.unique_key)):
        err(
            "duplicate-unique-key",
            f"unique_key contains duplicates: {contract.unique_key}.",
        )
    if not _IDENTIFIER.match(contract.loaded_at_field):
        err(
            "invalid-loaded-at-field",
            f"loaded_at_field {contract.loaded_at_field!r} is not a"
            " valid identifier.",
        )

    findings.extend(_lint_layer_config("raw", contract))
    findings.extend(_lint_layer_config("staging", contract))
    findings.extend(_lint_staging_columns(contract))

    return findings


def _appender(findings: list[Finding], severity: Severity):
    def add(code: str, message: str) -> None:
        findings.append(Finding(severity, code, message))

    return add


def _lint_layer_config(layer: str, contract: EventContract) -> list[Finding]:
    cfg = contract.raw if layer == "raw" else contract.staging
    findings: list[Finding] = []
    err = _appender(findings, Severity.ERROR)

    if not _IDENTIFIER.match(cfg.dataset):
        err(
            f"invalid-{layer}-dataset",
            f"{layer}.dataset {cfg.dataset!r} is not a valid BigQuery"
            " identifier.",
        )

    if cfg.incremental_strategy not in _INCREMENTAL_STRATEGIES:
        err(
            f"invalid-{layer}-strategy",
            f"{layer}.incremental_strategy"
            f" {cfg.incremental_strategy!r} is not a dbt-BigQuery"
            f" strategy. Use one of:"
            f" {sorted(_INCREMENTAL_STRATEGIES)}.",
        )

    if cfg.partition_by is not None:
        pb = cfg.partition_by
        if not _IDENTIFIER.match(pb.field):
            err(
                f"invalid-{layer}-partition-field",
                f"{layer}.partition_by.field {pb.field!r} is not a"
                " valid identifier.",
            )
        if pb.data_type.lower() not in _PARTITION_DATA_TYPES:
            err(
                f"invalid-{layer}-partition-type",
                f"{layer}.partition_by.data_type {pb.data_type!r}"
                f" must be one of {sorted(_PARTITION_DATA_TYPES)}.",
            )
        if pb.granularity.lower() not in _PARTITION_GRANULARITIES:
            err(
                f"invalid-{layer}-partition-granularity",
                f"{layer}.partition_by.granularity {pb.granularity!r}"
                f" must be one of {sorted(_PARTITION_GRANULARITIES)}.",
            )

    for col in cfg.cluster_by:
        if not _IDENTIFIER.match(col):
            err(
                f"invalid-{layer}-cluster-column",
                f"{layer}.cluster_by column {col!r} is not a valid"
                " identifier.",
            )

    return findings


def _lint_staging_columns(contract: EventContract) -> list[Finding]:
    staging: StagingConfig = contract.staging
    findings: list[Finding] = []
    if not staging.columns:
        return findings
    err = _appender(findings, Severity.ERROR)
    warn = _appender(findings, Severity.WARNING)

    seen_sources: set[str] = set()
    seen_names: set[str] = set()
    for col in staging.columns:
        for label, value in [("source", col.source), ("name", col.name)]:
            if not _IDENTIFIER.match(value):
                err(
                    f"invalid-column-{label}",
                    f"staging column {label} {value!r} is not a valid"
                    " identifier.",
                )
        if col.source in seen_sources:
            err(
                "duplicate-column-source",
                f"staging declares source column {col.source!r} more"
                " than once.",
            )
        seen_sources.add(col.source)
        if col.name in seen_names:
            err(
                "duplicate-column-name",
                f"staging declares output column {col.name!r} more than once.",
            )
        seen_names.add(col.name)

        if col.type and not _valid_bq_type(col.type):
            err(
                "invalid-column-type",
                f"staging column {col.name!r} casts to {col.type!r},"
                " which is not a BigQuery type.",
            )
        if not col.description:
            warn(
                "missing-column-description",
                f"staging column {col.name!r} has no description."
                " Downstream consumers read these.",
            )

    # The staging model only selects declared columns. If unique_key or
    # loaded_at_field aren't among them, the generated config and
    # incremental filter reference columns the model doesn't produce.
    staging_names = {c.name for c in staging.columns}
    for key in contract.unique_key:
        if key not in seen_sources:
            err(
                "unique-key-not-in-staging",
                f"unique_key column {key!r} is not declared in"
                " staging.columns. The staging model's unique_key"
                " would reference a column it never selects.",
            )
    if contract.loaded_at_field not in seen_sources:
        err(
            "loaded-at-not-in-staging",
            f"loaded_at_field {contract.loaded_at_field!r} is not"
            " declared in staging.columns. The incremental filter"
            " would reference a column the model never selects.",
        )

    if staging.partition_by is not None:
        if staging.partition_by.field not in staging_names:
            err(
                "partition-field-not-in-staging",
                f"staging.partition_by.field"
                f" {staging.partition_by.field!r} is not a declared"
                " staging column name. Remember: staging config uses"
                " post-rename names.",
            )
    for col in staging.cluster_by:
        if col not in staging_names:
            err(
                "cluster-column-not-in-staging",
                f"staging.cluster_by column {col!r} is not a declared"
                " staging column name.",
            )

    key_sources = set(contract.unique_key)
    for col in staging.columns:
        if col.source in key_sources and not col.tests:
            warn(
                "unique-key-untested",
                f"staging column {col.name!r} backs the unique_key"
                " but declares no tests. Consider [not_null,"
                " unique].",
            )

    return findings


def errors(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.is_error]


def warnings(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if not f.is_error]
