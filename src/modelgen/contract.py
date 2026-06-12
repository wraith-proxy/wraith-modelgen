"""
Data contract schema and parsing.

The contract is the source of truth. Source teams maintain it as part of
their release process. modelgen propagates contract changes to dbt models;
downstream FDPs see a stable interface because staging absorbs the drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_CONTRACT_VERSIONS = {"1"}


class ContractError(ValueError):
    """Raised when a contract is malformed or references invalid state."""


@dataclass(frozen=True)
class ColumnTest:
    name: str
    args: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: Any) -> ColumnTest:
        if isinstance(raw, str):
            return cls(name=raw)
        if isinstance(raw, dict):
            if len(raw) != 1:
                raise ContractError(
                    "Test entry must have exactly one key, "
                    f"got: {list(raw.keys())}"
                )
            name = str(next(iter(raw)))
            args = raw[name] or {}
            if not isinstance(args, dict):
                raise ContractError(
                    f"Test args for {name!r} must be a mapping, "
                    f"got {type(args).__name__}"
                )
            return cls(name=name, args=args)
        raise ContractError(f"Unrecognised test format: {raw!r}")


@dataclass
class StagingColumn:
    """One column in staging: source ref + rename + cast + tests."""

    source: str  # column name in raw (== source column name)
    name: str = ""  # column name in staging (default: source.lower())
    type: str = ""  # BigQuery type to cast to (default: no cast)
    description: str = ""
    tests: list[ColumnTest] = field(default_factory=list)

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
    def from_raw(cls, raw: dict) -> StagingColumn:
        if "source" not in raw:
            raise ContractError(f"Staging column missing 'source': {raw}")
        return cls(
            source=raw["source"],
            name=raw.get("name", ""),
            type=raw.get("type", ""),
            description=raw.get("description", ""),
            tests=[ColumnTest.from_raw(t) for t in raw.get("tests", [])],
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

    @classmethod
    def from_raw(cls, raw: dict) -> SourceRef:
        if not raw:
            raise ContractError("Contract missing 'source' block.")
        return cls(
            project=raw["project"],
            dataset=raw["dataset"],
            table=raw["table"],
        )


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
            raise ContractError("partition_by must specify a 'field'.")
        return cls(
            field=raw["field"],
            data_type=raw.get("data_type", "timestamp"),
            granularity=raw.get("granularity", "day"),
        )


@dataclass
class RawConfig:
    dataset: str = "raw"
    incremental_strategy: str = "merge"
    dedup: bool = False
    partition_by: PartitionConfig | None = None
    cluster_by: list[str] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: dict | None) -> RawConfig:
        raw = raw or {}
        cluster_by = raw.get("cluster_by", [])
        if isinstance(cluster_by, str):
            cluster_by = [cluster_by]
        return cls(
            dataset=raw.get("dataset", "raw"),
            incremental_strategy=raw.get("incremental_strategy", "merge"),
            dedup=raw.get("dedup", False),
            partition_by=PartitionConfig.from_raw(raw.get("partition_by")),
            cluster_by=cluster_by,
        )


@dataclass
class StagingConfig:
    dataset: str = "staging"
    incremental_strategy: str = "merge"
    columns: list[StagingColumn] = field(default_factory=list)
    partition_by: PartitionConfig | None = None
    cluster_by: list[str] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: dict | None) -> StagingConfig:
        raw = raw or {}
        cluster_by = raw.get("cluster_by", [])
        if isinstance(cluster_by, str):
            cluster_by = [cluster_by]
        return cls(
            dataset=raw.get("dataset", "staging"),
            incremental_strategy=raw.get("incremental_strategy", "merge"),
            columns=[
                StagingColumn.from_raw(c) for c in raw.get("columns", [])
            ],
            partition_by=PartitionConfig.from_raw(raw.get("partition_by")),
            cluster_by=cluster_by,
        )


def _normalise_unique_key(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and all(isinstance(k, str) for k in raw):
        if not raw:
            raise ContractError("unique_key list cannot be empty.")
        return [str(k) for k in raw]
    raise ContractError(
        f"unique_key must be a string or list of strings, got {raw!r}"
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
            raw_yaml = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise ContractError(
                f"Failed to parse YAML at {source}: {e}"
            ) from e

        if not isinstance(raw_yaml, dict):
            raise ContractError(
                "Contract root must be a mapping, "
                f"got {type(raw_yaml).__name__}"
            )

        version = str(raw_yaml.get("version", ""))
        if version not in SUPPORTED_CONTRACT_VERSIONS:
            raise ContractError(
                f"Unsupported contract version {version!r}. "
                f"Supported: {sorted(SUPPORTED_CONTRACT_VERSIONS)}"
            )

        event = raw_yaml.get("event")
        if not isinstance(event, dict):
            raise ContractError("Contract missing 'event' block.")

        required = ["name", "unique_key", "loaded_at_field", "source"]
        missing = [k for k in required if k not in event]
        if missing:
            raise ContractError(f"event missing required fields: {missing}")

        return cls(
            name=event["name"],
            description=event.get("description", ""),
            unique_key=_normalise_unique_key(event["unique_key"]),
            loaded_at_field=event["loaded_at_field"],
            source=SourceRef.from_raw(event["source"]),
            raw=RawConfig.from_raw(event.get("raw")),
            staging=StagingConfig.from_raw(event.get("staging")),
        )
