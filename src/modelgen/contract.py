"""
Data contract schema and parsing.

Contracts are Open Data Contract Standard (ODCS) v3.0.2 documents. Source
teams maintain them as part of their release process. modelgen propagates
contract changes to dbt models; downstream FDPs see a stable interface
because staging absorbs the drift.

ODCS is a general standard; modelgen reads the subset it needs and carries
its BigQuery/dbt specifics in ODCS `customProperties`:

  schema[]                   -> one raw + one staging model per object, in
                                contract order (`from_file_all`)
  schema[].name              -> event name (dbt model stem)
  schema[].physicalName      -> source table
  servers[bigquery]          -> source project + dataset
  servers[duckdb | local]    -> DuckDB dialect: database (or path) + schema;
                                `local` with `format: parquet` is a Parquet
                                landing file read via external_location
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

from modelgen.dialect import (
    UnknownDialectError,
    get_dialect,
    supported_server_types,
)

ODCS_API_VERSION = "v3.0.2"
SUPPORTED_ODCS_VERSIONS = ("v3.0.2", "v3.1.0", "v3.2.0")
_SCHEMA_DIR = Path(__file__).parent / "schema"


class ContractError(ValueError):
    """Raised when a contract is malformed or references invalid state."""


@lru_cache(maxsize=None)
def _odcs_validator(
    version: str = ODCS_API_VERSION,
) -> jsonschema.protocols.Validator:
    path = _SCHEMA_DIR / f"odcs-json-schema-{version}.json"
    schema = json.loads(path.read_text())
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


def odcs_schema_errors(doc: dict[str, Any]) -> list[Any]:
    """Validate ``doc`` against the bundled schema for its apiVersion.

    A missing or non-string apiVersion is validated against the default
    (v3.0.2) schema, which reports it. An unknown string version raises
    ContractError listing the supported versions.
    """
    version = doc.get("apiVersion")
    if not isinstance(version, str):
        version = ODCS_API_VERSION
    elif version not in SUPPORTED_ODCS_VERSIONS:
        # v2.x and v3.0.0/3.0.1 stay accepted via the v3.0.2 schema enum.
        if version in _LEGACY_ODCS_VERSIONS:
            version = ODCS_API_VERSION
        else:
            raise ContractError(
                f"Contract is not valid ODCS: unsupported apiVersion"
                f" {version!r}. Supported versions:"
                f" {', '.join(SUPPORTED_ODCS_VERSIONS)}."
            )
    return sorted(
        _odcs_validator(version).iter_errors(doc), key=lambda e: e.path
    )


_LEGACY_ODCS_VERSIONS = ("v3.0.1", "v3.0.0", "v2.2.2", "v2.2.1", "v2.2.0")


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
    server_type: str = "bigquery"  # ODCS servers[].type; picks the Dialect
    server_format: str = ""  # ODCS servers[].format (local: parquet)

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
    # "" lets the dialect choose. Only the DuckDB dialect reads it.
    materialization: str = ""

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
            materialization=raw.get("materialization", ""),
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


def _select_server(servers: list[dict], name: str | None = None) -> dict:
    """Pick the server to generate against.

    With `name`, the server whose `server:` id matches; it must be of a
    supported type. Without, the first `type: bigquery` server (as in
    0.14.x), and only if there is none, the first server whose `type` a
    registered dialect answers to (contract order).

    dialect.py imports contract.py only for type checking, so this module
    can use `get_dialect` directly.
    """
    if name is not None:
        ids = [str(s.get("server")) for s in servers]
        for s in servers:
            if str(s.get("server")) != name:
                continue
            try:
                get_dialect(str(s.get("type", "")))
            except UnknownDialectError as e:
                raise ContractError(
                    f"Server {name!r} has type {s.get('type')!r}, which no"
                    f" dialect supports. Supported dialects:"
                    f" {', '.join(supported_server_types())}."
                ) from e
            return s
        raise ContractError(
            f"No server named {name!r}. Servers in this contract:"
            f" {', '.join(ids) or 'none'}."
        )
    for s in servers:
        if s.get("type") == "bigquery":
            return s
    for s in servers:
        try:
            get_dialect(str(s.get("type", "")))
        except UnknownDialectError:
            continue
        return s
    found = ", ".join(sorted({str(s.get("type")) for s in servers})) or "none"
    raise ContractError(
        "Contract has no BigQuery server or other supported server (found: "
        f"{found}). Supported dialects: "
        f"{', '.join(supported_server_types())}. Add a servers entry with"
        " one of those types."
    )


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
    contract_name: str = ""  # ODCS top-level name (falls back to name)
    contract_version: str = ""  # ODCS top-level version

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
    def from_file_all(
        cls,
        path: Path,
        *,
        server: str | None = None,
        tables: list[str] | None = None,
    ) -> list[EventContract]:
        return cls.from_text_all(
            path.read_text(), source=str(path), server=server, tables=tables
        )

    @classmethod
    def from_text(cls, text: str, source: str = "<string>") -> EventContract:
        """Parse a contract that describes exactly one table."""
        doc = _load_doc(text, source)
        count = len(doc.get("schema") or [])
        if count != 1:
            raise ContractError(
                "Expected exactly one schema object, got"
                f" {count}. Use from_text_all for multi-table contracts."
            )
        return cls._from_doc(doc)[0]

    @classmethod
    def from_text_all(
        cls,
        text: str,
        source: str = "<string>",
        *,
        server: str | None = None,
        tables: list[str] | None = None,
    ) -> list[EventContract]:
        """One EventContract per schema object, in contract order.

        `server` picks a server by its `server:` id (default: the first
        server of a supported type). `tables` limits the result to the
        named objects; the order stays the contract's, not the caller's.
        """
        return cls._from_doc(_load_doc(text, source), server, tables)

    @classmethod
    def _from_doc(
        cls,
        doc: dict[str, Any],
        server_name: str | None = None,
        tables: list[str] | None = None,
    ) -> list[EventContract]:
        schemas, _ = select_schema_objects(doc, tables, unique=True)

        server = _select_server(doc.get("servers") or [], server_name)
        project, dataset = get_dialect(server["type"]).source_location(server)
        return [
            cls._from_object(doc, obj, server, project, dataset)
            for obj in schemas
        ]

    @classmethod
    def _from_object(
        cls,
        doc: dict[str, Any],
        obj: dict[str, Any],
        server: dict[str, Any],
        project: str,
        dataset: str,
    ) -> EventContract:
        props = obj.get("properties") or []
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
                f" Object: {obj.get('name')}."
            )

        return cls(
            name=obj["name"],
            description=obj.get("description", ""),
            unique_key=_unique_key_from_properties(props),
            loaded_at_field=loaded_at,
            source=SourceRef(
                project=project,
                dataset=dataset,
                table=table,
                server_type=server["type"],
                server_format=str(server.get("format") or ""),
            ),
            raw=RawConfig.from_raw(cfg.get("modelgenRaw")),
            staging=StagingConfig.from_raw(
                cfg.get("modelgenStaging"),
                [StagingColumn.from_property(p) for p in props],
            ),
            subject_field=cfg.get("modelgenSubjectField") or "",
            exemptions=_exemptions(obj),
            contract_name=str(doc.get("name") or obj["name"]),
            contract_version=str(doc.get("version") or ""),
        )


def select_schema_objects(
    doc: dict[str, Any],
    tables: list[str] | None = None,
    *,
    unique: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """(chosen schema objects, total object count) for a parsed contract.

    Raises ContractError for an empty `schema:` and for unknown `tables`.
    With `unique`, duplicate object names are rejected too, before the
    unknown-table check. The total is the count before `tables` narrows.
    """
    schemas = doc.get("schema") or []
    if not schemas:
        raise ContractError(
            "Contract declares no schema objects. Add at least one"
            " entry under schema:."
        )
    names = [str(o.get("name")) for o in schemas]
    if unique:
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ContractError(
                f"Duplicate schema object name(s): {', '.join(dupes)}."
                " Each object becomes a model, so names must be unique."
            )
    if tables is None:
        return schemas, len(schemas)
    unknown = [t for t in tables if t not in names]
    if unknown:
        raise ContractError(
            f"Unknown table(s): {', '.join(unknown)}. Objects in"
            f" this contract: {', '.join(names)}."
        )
    return [o for o in schemas if str(o.get("name")) in tables], len(schemas)


def _load_doc(text: str, source: str) -> dict[str, Any]:
    """Parse YAML and check it is a valid ODCS DataContract."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ContractError(f"Failed to parse YAML at {source}: {e}") from e

    if not isinstance(doc, dict):
        raise ContractError(
            f"Contract root must be a mapping, got {type(doc).__name__}"
        )

    errors = odcs_schema_errors(doc)
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
    return doc
