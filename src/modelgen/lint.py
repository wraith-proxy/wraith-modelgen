"""
Semantic contract linting.

Parsing (contract.py) catches structural problems: missing blocks, wrong
shapes. Linting catches the semantic ones that would otherwise surface as
broken SQL in dbt hours later: a typo'd incremental strategy, a cast to a
type BigQuery has never heard of, a staging unique_key pointing at a column
the staging model doesn't select.

All findings are collected in one pass, so everything can be fixed at
once rather than playing whack-a-mole one error per run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum

from modelgen.contract import (
    SNAPSHOT_PARTITION_FIELD,
    SNAPSHOT_STRATEGY,
    EventContract,
    StagingColumn,
    StagingConfig,
)
from modelgen.dialect import get_dialect

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_PROJECT = re.compile(r"^[a-z][a-z0-9-]*$")

_INCREMENTAL_STRATEGIES = {"merge", "insert_overwrite", "microbatch"}
_MATERIALIZATIONS = {"snapshot", "incremental"}
_CLASSIFICATIONS = {"pii", "psi"}
_PARTITION_DATA_TYPES = {"timestamp", "date", "datetime", "int64"}
_PARTITION_GRANULARITIES = {"hour", "day", "month", "year"}
# dbt's source-freshness vocabulary.
_FRESHNESS_PERIODS = {"minute", "hour", "day"}

# A description shorter than this is a placeholder, not documentation. The
# floor is calibrated for readers who did not write the model.
_MIN_DESCRIPTION_LENGTH = 50


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    code: str
    message: str
    column: str = ""  # staging column name; "" means contract-scoped
    exempt_reason: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity is Severity.ERROR

    @property
    def exempted(self) -> bool:
        return bool(self.exempt_reason)


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
            " ^[a-z][a-z0-9_]*$, because it becomes a dbt model name"
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
    findings.extend(_lint_staging_materialization(contract))
    findings.extend(_lint_staging_columns(contract))
    findings.extend(_lint_classification(contract))
    findings.extend(_lint_freshness(contract))

    findings = _apply_exemptions(contract, findings)
    # After the sweep, so an exemption can never exempt itself.
    findings.extend(_lint_exemptions(contract))

    return findings


def _appender(findings: list[Finding], severity: Severity):
    def add(code: str, message: str, column: str = "") -> None:
        findings.append(Finding(severity, code, message, column))

    return add


def _apply_exemptions(
    contract: EventContract, findings: list[Finding]
) -> list[Finding]:
    """Attach the justification to every finding an exemption covers.

    Matching is on (column, rule). A contract-scoped exemption keys on the
    empty column, so it can never reach into a column-scoped finding of the
    same code, or the reverse.
    """
    # ponytail: no registry of valid rule codes. A typo'd rule is inert and
    # its warning keeps firing, so the mistake is already visible. Add one if
    # typos become a recurring cost.
    reasons: dict[tuple[str, str], str] = {
        ("", e.rule): e.reason for e in contract.exemptions if e.reason.strip()
    }
    for col in contract.staging.columns:
        for e in col.exemptions:
            if e.reason.strip():
                reasons[(col.name, e.rule)] = e.reason

    return [
        replace(f, exempt_reason=reasons[(f.column, f.code)])
        if (f.column, f.code) in reasons
        else f
        for f in findings
    ]


def _lint_exemptions(contract: EventContract) -> list[Finding]:
    findings: list[Finding] = []
    err = _appender(findings, Severity.ERROR)

    scopes: list[tuple[str, list]] = [("", contract.exemptions)]
    scopes += [(c.name, c.exemptions) for c in contract.staging.columns]

    for column, exemptions in scopes:
        for e in exemptions:
            if not e.reason.strip():
                err(
                    "invalid-exemption-reason",
                    f"modelgenExempt entry for rule {e.rule!r} declares no"
                    " reason. Say why the rule does not apply here, so the"
                    " next person to read it does not have to guess.",
                    column=column,
                )

    return findings


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


def _lint_staging_materialization(
    contract: EventContract,
) -> list[Finding]:
    staging: StagingConfig = contract.staging
    findings: list[Finding] = []
    err = _appender(findings, Severity.ERROR)

    if staging.materialization not in _MATERIALIZATIONS:
        err(
            "invalid-staging-materialization",
            f"staging.materialization {staging.materialization!r} must be"
            f" one of {sorted(_MATERIALIZATIONS)}.",
        )
        return findings

    if not staging.is_snapshot:
        return findings

    if staging.incremental_strategy != SNAPSHOT_STRATEGY:
        err(
            "snapshot-strategy-conflict",
            f"staging.incremental_strategy"
            f" {staging.incremental_strategy!r} conflicts with"
            f" materialization: snapshot, which replaces whole"
            f" {SNAPSHOT_PARTITION_FIELD} partitions. Drop the key or use"
            f" {SNAPSHOT_STRATEGY!r}.",
        )

    pb = staging.partition_by
    if pb is None or pb.field != SNAPSHOT_PARTITION_FIELD:
        declared = "nothing" if pb is None else repr(pb.field)
        err(
            "snapshot-partition-not-ds",
            f"staging.partitionBy.field is {declared}; a snapshot is"
            f" partitioned on {SNAPSHOT_PARTITION_FIELD!r}. Drop"
            " partitionBy and let it derive, or switch to"
            " materialization: incremental.",
        )

    if any(c.name == SNAPSHOT_PARTITION_FIELD for c in staging.columns):
        err(
            "ds-column-reserved",
            f"a snapshot model generates its own"
            f" {SNAPSHOT_PARTITION_FIELD!r} column; a property may not"
            " also map to that name.",
        )

    return findings


def _weak_description(col: StagingColumn) -> str:
    """Why a non-empty description reads as a placeholder, "" if it's fine."""
    text = col.description.strip()
    if text.lower() in {col.name.lower(), col.source.lower()}:
        return "it only restates the column name"
    if len(text) < _MIN_DESCRIPTION_LENGTH:
        return (
            f"it is {len(text)} characters, under the"
            f" {_MIN_DESCRIPTION_LENGTH}-character floor"
        )
    return ""


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
                    column=col.name,
                )
        if col.source in seen_sources:
            err(
                "duplicate-column-source",
                f"staging declares source column {col.source!r} more"
                " than once.",
                column=col.name,
            )
        seen_sources.add(col.source)
        if col.name in seen_names:
            err(
                "duplicate-column-name",
                f"staging declares output column {col.name!r} more than once.",
                column=col.name,
            )
        seen_names.add(col.name)

        if col.type and not get_dialect(
            contract.source.server_type
        ).is_valid_physical_type(col.type):
            err(
                "invalid-column-type",
                f"staging column {col.name!r} casts to {col.type!r},"
                " which is not a BigQuery type.",
                column=col.name,
            )
        if not col.description:
            warn(
                "missing-column-description",
                f"staging column {col.name!r} has no description."
                " Downstream consumers read these.",
                column=col.name,
            )
        elif weakness := _weak_description(col):
            warn(
                "low-quality-column-description",
                f"staging column {col.name!r} has a description, but"
                f" {weakness}. Say what the value means and when it is"
                " null, not what the name already says. If the column"
                " genuinely needs no more, exempt"
                " low-quality-column-description with a reason.",
                column=col.name,
            )

    # The staging model only selects declared columns. If unique_key or
    # loaded_at_field aren't among them, the generated config and
    # incremental filter reference columns the model doesn't produce.
    staging_names = {c.name for c in staging.columns}
    if staging.is_snapshot:
        staging_names.add(SNAPSHOT_PARTITION_FIELD)
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
                column=col.name,
            )

    return findings


def _lint_freshness(contract: EventContract) -> list[Finding]:
    """A source nobody checks for staleness fails silently."""
    findings: list[Finding] = []
    err = _appender(findings, Severity.ERROR)
    warn = _appender(findings, Severity.WARNING)

    freshness = contract.raw.freshness
    if freshness is None:
        warn(
            "missing-freshness",
            "modelgenRaw declares no freshness block, so"
            " `dbt source freshness` has nothing to check and a stalled"
            " pipeline stays quiet until someone notices the numbers are"
            " wrong. Add warnAfter/errorAfter, or exempt"
            " missing-freshness with a reason (a static reference table"
            " refreshed by hand is the usual one).",
        )
        return findings

    for label, threshold in [
        ("warnAfter", freshness.warn_after),
        ("errorAfter", freshness.error_after),
    ]:
        if threshold is None:
            continue
        if threshold.period.lower() not in _FRESHNESS_PERIODS:
            err(
                "invalid-freshness-period",
                f"freshness.{label}.period {threshold.period!r} is not a"
                f" dbt freshness period. Use one of"
                f" {sorted(_FRESHNESS_PERIODS)}.",
            )
        if not isinstance(threshold.count, int) or threshold.count <= 0:
            err(
                "invalid-freshness-count",
                f"freshness.{label}.count {threshold.count!r} must be a"
                " positive integer.",
            )

    return findings


def _lint_classification(contract: EventContract) -> list[Finding]:
    """Encryption is only as good as the contract wiring it up.

    Every check here catches a way the generated model would leak a
    classified value in plaintext, or produce SQL that cannot run.
    """
    staging = contract.staging
    findings: list[Finding] = []
    err = _appender(findings, Severity.ERROR)
    warn = _appender(findings, Severity.WARNING)

    for col in staging.columns:
        if col.classification and col.classification not in _CLASSIFICATIONS:
            err(
                "invalid-classification",
                f"property {col.name!r} declares classification"
                f" {col.classification!r}; modelgen understands"
                f" {sorted(_CLASSIFICATIONS)}.",
                column=col.name,
            )
        # Encryption returns BYTES, so accepted_values could never match.
        # An enumerated column is also a category rather than a personal
        # value, and categories leak to frequency analysis.
        if col.classification and any(
            t.name == "accepted_values" for t in col.tests
        ):
            err(
                "classified-column-enumerated",
                f"classified property {col.name!r} declares validValues."
                " An enumerated column is a category, not a personal"
                " value: its ciphertext falls to frequency analysis and"
                " the accepted_values test cannot match BYTES.",
                column=col.name,
            )

    # Encryption emits BYTES, and BigQuery clusters on none of it and
    # partitions on none of it. Catch it here rather than in dbt.
    classified = {c.name for c in contract.classified_columns}
    for col in staging.cluster_by:
        if col in classified:
            err(
                "cluster-column-classified",
                f"clusterBy names {col!r}, which is classified and"
                " therefore emitted as BYTES. BigQuery cannot cluster on"
                " BYTES. Cluster on an unclassified column, or wrap the"
                " ciphertext in to_hex() yourself if the lookup path"
                " truly needs it.",
                column=col,
            )
    if (
        staging.partition_by is not None
        and staging.partition_by.field in classified
    ):
        err(
            "partition-field-classified",
            f"partitionBy.field {staging.partition_by.field!r} is"
            " classified and therefore emitted as BYTES. BigQuery"
            " partitions on date, timestamp, datetime or int64 only.",
            column=staging.partition_by.field,
        )

    if not contract.encrypts:
        return findings

    if not contract.subject_field:
        err(
            "subject-field-not-declared",
            "properties are classified but modelgenSubjectField is not"
            " set. modelgen cannot tell which column identifies the data"
            " subject, so it cannot resolve a keyset.",
        )
        return findings

    subject = next(
        (c for c in staging.columns if c.source == contract.subject_field),
        None,
    )
    if subject is None:
        err(
            "subject-field-not-in-staging",
            f"modelgenSubjectField {contract.subject_field!r} is not a"
            " declared staging property. The keystore join would"
            " reference a column the model never selects.",
        )
        return findings

    if not subject.classification:
        err(
            "subject-field-unclassified",
            f"modelgenSubjectField {contract.subject_field!r} identifies"
            " the data subject but carries no classification, so it"
            " would be materialised in plaintext, and it is the one"
            " column that must not be.",
            column=subject.name,
        )

    # The incremental filter reads `where <upstream> > (select max(<this>))`.
    # If the staging side is ciphertext that comparison is nonsense. A
    # snapshot has no filter, so this only bites in incremental mode.
    if not staging.is_snapshot:
        loaded_at = next(
            (
                c
                for c in staging.columns
                if c.source == contract.loaded_at_field
            ),
            None,
        )
        if loaded_at is not None and loaded_at.classification:
            err(
                "loaded-at-classified",
                f"modelgenLoadedAtField {contract.loaded_at_field!r} is"
                " classified, so the incremental filter would compare a"
                " timestamp against ciphertext. A load timestamp is"
                " pipeline metadata. Drop the classification, or"
                " switch to materialization: snapshot.",
                column=loaded_at.name,
            )

    if not any(t.name == "not_null" for t in subject.tests):
        warn(
            "subject-field-untested",
            f"subject column {subject.name!r} has no not_null test. A"
            " row whose subject has no keyset encrypts to NULL, and"
            " nothing would surface it. Set required: true.",
            column=subject.name,
        )

    return findings


def errors(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.is_error and not f.exempted]


def warnings(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if not f.is_error and not f.exempted]
