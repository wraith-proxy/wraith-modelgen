"""
Data contract schema and parsing.

Contracts are Open Data Contract Standard (ODCS) v3.0.2 documents. Source
teams maintain them as part of their release process. modelgen propagates
contract changes to dbt models; downstream FDPs see a stable interface
because staging absorbs the drift.

ODCS is a general standard; modelgen reads the subset it needs and carries
its BigQuery/dbt specifics in ODCS `customProperties`:

  schema[].name              -> event name (dbt model stem)
  schema[].physicalName      -> source table
  servers[bigquery]          -> source project + dataset
  schema[].properties[]      -> staging columns
    property.name            -> staging (consumer-facing) column name
    property.physicalName    -> source column name
    property.physicalType    -> BigQuery type to cast to
    property.primaryKey      -> contributes to unique_key (source names)
    property.required        -> not_null test
    property.unique          -> unique test
    property.logicalTypeOptions.validValues -> accepted_values test
    property.classification  -> pii/psi; drives encryption in staging
    property.customProperties -> modelgenExempt (column-scoped)
  schema[].customProperties  -> modelgenLoadedAtField, modelgenSubjectField,
                                modelgenRaw, modelgenStaging, modelgenExempt

Staging materialises as a datestamped daily snapshot by default: one full
copy of the dimension per `ds` partition. `materialization: incremental`
opts back into an incremental load keyed on the contract's partitionBy.

A classified property is encrypted on the way into staging, never
materialised in plaintext. modelgenSubjectField names the source column
identifying the data subject: it is encrypted deterministically so joins
survive a crypto-shred, and it is the keystore lookup for every other
classified column on the row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema
import yaml

ODCS_API_VERSION = "v3.0.2"
_SCHEMA_PATH = (
    Path(__file__).parent / "schema" / "odcs-json-schema-v3.0.2.json"
)


class ContractError(ValueError):
    """Raised when a contract is malformed or references invalid state."""


@lru_cache(maxsize=1)
def _odcs_validator() -> jsonschema.protocols.Validator:
    schema = json.loads(_SCHEMA_PATH.read_text())
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


@dataclass(frozen=True)
class ColumnTest:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Exemption:
    """A justified opt-out from one lint rule, contract- or column-scoped."""

    rule: str
    reason: str


@dataclass
class StagingColumn:
    """One column in staging: source ref + rename + cast + tests."""

    source: str  # column name in raw (== source column name)
    name: str = ""  # column name in staging (default: source.lower())
    type: str = ""  # BigQuery type to cast to (default: no cast)
    description: str = ""
    tests: list[ColumnTest] = field(default_factory=list)
    classification: str = ""  # "" | "pii" | "psi": drives encryption
    exemptions: list[Exemption] = field(default_factory=list)

    def __post_init__(self):
        if not self.source:
            raise ContractError("Staging column must declare a 'source'.")
        if not self.name:
            object.__setattr__(self, "name", self.source.lower())

    @property
    def needs_cast(self) -> bool:
        return bool(self.type)

    @property
    def needs_rename(self) -> bool:
        return self.source != self.name

    @classmethod
    def from_property(cls, prop: dict) -> StagingColumn:
        """Build from an ODCS schema property.

        physicalName is the source column; name is the consumer-facing
        name. A property with neither is invalid.
        """
        name = prop.get("name", "")
        source = prop.get("physicalName") or name
        if not source:
            raise ContractError(
                f"Schema property missing 'name'/'physicalName': {prop}"
            )
        tests: list[ColumnTest] = []
        if prop.get("required"):
            tests.append(ColumnTest("not_null"))
        if prop.get("unique"):
            tests.append(ColumnTest("unique"))
        for check in prop.get("quality") or []:
            if check.get("rule") == "validValues" and "validValues" in check:
                tests.append(
                    ColumnTest(
                        "accepted_values",
                        {"values": check["validValues"]},
                    )
                )
        return cls(
            source=source,
            name=name or source.lower(),
            type=prop.get("physicalType", ""),
            description=prop.get("description", ""),
            tests=tests,
            classification=(prop.get("classification") or "").strip().lower(),
            exemptions=_exemptions(prop),
        )


@dataclass(frozen=True)
class SourceRef:
    """Fully-qualified BigQuery table reference."""

    project: str
    dataset: str
    table: str

    def __post_init__(self):
        for label, value in [
            ("project", self.project),
            ("dataset", self.dataset),
            ("table", self.table),
        ]:
            if not value or not isinstance(value, str):
                raise ContractError(
                    f"source.{label} must be a non-empty string."
                )

    @property
    def fqn(self) -> str:
        return f"{self.project}.{self.dataset}.{self.table}"


@dataclass(frozen=True)
class PartitionConfig:
    """BigQuery partition spec. Maps directly to dbt's partition_by config."""

    field: str
    data_type: str = "timestamp"
    granularity: str = "day"

    @classmethod
    def from_raw(cls, raw: dict | None) -> PartitionConfig | None:
        if raw is None:
            return None
        if "field" not in raw:
            raise ContractError("partitionBy must specify a 'field'.")
        return cls(
            field=raw["field"],
            data_type=raw.get("dataType", "timestamp"),
            granularity=raw.get("granularity", "day"),
        )


@dataclass(frozen=True)
class FreshnessThreshold:
    """One dbt source-freshness bound. Validation lives in lint."""

    count: Any
    period: str

    @classmethod
    def from_raw(cls, raw: dict | None) -> FreshnessThreshold | None:
        if raw is None:
            return None
        return cls(
            count=raw.get("count", 0),
            period=str(raw.get("period", "")),
        )


@dataclass(frozen=True)
class FreshnessConfig:
    warn_after: FreshnessThreshold | None = None
    error_after: FreshnessThreshold | None = None

    @classmethod
    def from_raw(cls, raw: dict | None) -> FreshnessConfig | None:
        if raw is None:
            return None
        warn_after = FreshnessThreshold.from_raw(raw.get("warnAfter"))
        error_after = FreshnessThreshold.from_raw(raw.get("errorAfter"))
        # A freshness block with neither bound would render an empty dbt
        # `freshness:` key, which dbt rejects. Treat it as undeclared so
        # lint reports it as missing rather than generating broken YAML.
        if warn_after is None and error_after is None:
            return None
        return cls(warn_after=warn_after, error_after=error_after)


SNAPSHOT_PARTITION_FIELD = "ds"
SNAPSHOT_STRATEGY = "insert_overwrite"
_SNAPSHOT_PARTITION = PartitionConfig(
    field=SNAPSHOT_PARTITION_FIELD, data_type="date", granularity="day"
)


def _cluster_by(raw: dict) -> list[str]:
    cluster_by = raw.get("clusterBy", [])
    if isinstance(cluster_by, str):
        return [cluster_by]
    return list(cluster_by)


@dataclass
class RawConfig:
    dataset: str = "raw"
    incremental_strategy: str = "merge"
    dedup: bool = False
    partition_by: PartitionConfig | None = None
    cluster_by: list[str] = field(default_factory=list)
    freshness: FreshnessConfig | None = None

    @classmethod
    def from_raw(cls, raw: dict | None) -> RawConfig:
        raw = raw or {}
        return cls(
            dataset=raw.get("dataset", "raw"),
            incremental_strategy=raw.get("incrementalStrategy", "merge"),
            dedup=raw.get("dedup", False),
            partition_by=PartitionConfig.from_raw(raw.get("partitionBy")),
            cluster_by=_cluster_by(raw),
            freshness=FreshnessConfig.from_raw(raw.get("freshness")),
        )


@dataclass
class StagingConfig:
    dataset: str = "staging"
    materialization: str = "snapshot"
    incremental_strategy: str = SNAPSHOT_STRATEGY
    columns: list[StagingColumn] = field(default_factory=list)
    partition_by: PartitionConfig | None = None
    cluster_by: list[str] = field(default_factory=list)

    @property
    def is_snapshot(self) -> bool:
        return self.materialization == "snapshot"

    @classmethod
    def from_raw(
        cls, raw: dict | None, columns: list[StagingColumn]
    ) -> StagingConfig:
        raw = raw or {}
        if "dedup" in raw:
            raise ContractError(
                "modelgenStaging does not support 'dedup'. Dedup is a"
                " raw-layer concern, and a snapshot is the full dimension."
            )
        # A contract can opt out of a staging model (raw-only) while still
        # declaring its schema/key via properties.
        if raw.get("generate") is False:
            columns = []
        materialization = raw.get("materialization", "snapshot")
        snapshot = materialization == "snapshot"
        # Snapshot mode implies partitioning on ds, so the contract does not
        # restate it. A declared partitionBy is kept as-is and lint rejects
        # it if it disagrees, since silently overriding it hides the mistake.
        declared_partition = PartitionConfig.from_raw(raw.get("partitionBy"))
        if snapshot and declared_partition is None:
            declared_partition = _SNAPSHOT_PARTITION
        return cls(
            dataset=raw.get("dataset", "staging"),
            materialization=materialization,
            incremental_strategy=raw.get(
                "incrementalStrategy",
                SNAPSHOT_STRATEGY if snapshot else "merge",
            ),
            columns=columns,
            partition_by=declared_partition,
            cluster_by=_cluster_by(raw),
        )


def _unique_key_from_properties(props: list[dict]) -> list[str]:
    """Source column names of primary-key properties, ordered by
    primaryKeyPosition (then declaration order)."""
    keyed = [p for p in props if p.get("primaryKey")]
    keyed.sort(key=lambda p: p.get("primaryKeyPosition", 0))
    keys = [p.get("physicalName") or p.get("name", "") for p in keyed]
    if not keys:
        raise ContractError(
            "No primaryKey property found. Mark the key column(s) with"
            " primaryKey: true. They become the dbt unique_key."
        )
    return keys


def _custom_props(obj: dict) -> dict[str, Any]:
    """Flatten an ODCS customProperties list into a dict."""
    out: dict[str, Any] = {}
    for cp in obj.get("customProperties") or []:
        if "property" in cp:
            out[cp["property"]] = cp.get("value")
    return out


def _exemptions(obj: dict) -> list[Exemption]:
    """Read modelgenExempt off any object carrying customProperties."""
    raw = _custom_props(obj).get("modelgenExempt") or []
    if not isinstance(raw, list) or not all(isinstance(e, dict) for e in raw):
        raise ContractError(
            "modelgenExempt must be a list of {rule, reason} mappings."
        )
    return [
        Exemption(
            rule=str(e.get("rule", "")),
            reason=str(e.get("reason", "")),
        )
        for e in raw
    ]


def _bigquery_server(servers: list[dict]) -> dict:
    bq = [s for s in servers if s.get("type") == "bigquery"]
    if not bq:
        raise ContractError(
            "Contract has no BigQuery server. Add a servers entry with"
            " type: bigquery, project, and dataset."
        )
    return bq[0]


@dataclass
class EventContract:
    name: str
    description: str
    unique_key: list[str]
    loaded_at_field: str
    source: SourceRef
    raw: RawConfig
    staging: StagingConfig
    subject_field: str = ""  # source column identifying the data subject
    exemptions: list[Exemption] = field(default_factory=list)

    @property
    def classified_columns(self) -> list[StagingColumn]:
        return [c for c in self.staging.columns if c.classification]

    @property
    def encrypts(self) -> bool:
        return bool(self.classified_columns)

    @property
    def raw_model_name(self) -> str:
        return f"raw__{self.name}"

    @property
    def staging_model_name(self) -> str:
        return f"stg__{self.name}"

    @property
    def unique_key_dbt(self) -> str:
        """Render unique_key in the form dbt expects: a string for single key,
        a list literal for composite."""
        if len(self.unique_key) == 1:
            return f"'{self.unique_key[0]}'"
        return "[" + ", ".join(f"'{k}'" for k in self.unique_key) + "]"

    @classmethod
    def from_file(cls, path: Path) -> EventContract:
        return cls.from_text(path.read_text(), source=str(path))

    @classmethod
    def from_text(cls, text: str, source: str = "<string>") -> EventContract:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise ContractError(
                f"Failed to parse YAML at {source}: {e}"
            ) from e

        if not isinstance(doc, dict):
            raise ContractError(
                f"Contract root must be a mapping, got {type(doc).__name__}"
            )

        errors = sorted(
            _odcs_validator().iter_errors(doc), key=lambda e: e.path
        )
        if errors:
            first = errors[0]
            loc = "/".join(str(p) for p in first.path) or "<root>"
            raise ContractError(
                f"Contract at {source} is not valid ODCS"
                f" ({len(errors)} error(s)). First at {loc}:"
                f" {first.message}"
            )

        if doc.get("kind") != "DataContract":
            raise ContractError(
                f"kind must be 'DataContract', got {doc.get('kind')!r}."
            )

        schemas = doc.get("schema") or []
        if len(schemas) != 1:
            raise ContractError(
                "modelgen contracts describe exactly one table:"
                f" expected one schema object, got {len(schemas)}."
            )
        obj = schemas[0]
        props = obj.get("properties") or []

        bq = _bigquery_server(doc.get("servers") or [])
        table = obj.get("physicalName") or obj.get("name")
        if not table:
            raise ContractError(
                "Schema object needs a physicalName (the source table)."
            )

        cfg = _custom_props(obj)
        loaded_at = cfg.get("modelgenLoadedAtField")
        if not loaded_at:
            raise ContractError(
                "Schema customProperties must set modelgenLoadedAtField"
                " to the source column used for incremental loads."
            )

        return cls(
            name=obj["name"],
            description=obj.get("description", ""),
            unique_key=_unique_key_from_properties(props),
            loaded_at_field=loaded_at,
            source=SourceRef(
                project=bq.get("project", ""),
                dataset=bq.get("dataset", ""),
                table=table,
            ),
            raw=RawConfig.from_raw(cfg.get("modelgenRaw")),
            staging=StagingConfig.from_raw(
                cfg.get("modelgenStaging"),
                [StagingColumn.from_property(p) for p in props],
            ),
            subject_field=cfg.get("modelgenSubjectField") or "",
            exemptions=_exemptions(obj),
        )
