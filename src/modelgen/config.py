"""
Project-level configuration for modelgen.

Reads .modelgen.yml from the repo root (or walks up from cwd to find it).
All paths in the config file are relative to the .modelgen.yml location.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from modelgen.contract import Issue
from modelgen.errors import ModelgenError


class ConfigError(ModelgenError, ValueError):
    """Raised when .modelgen.yml is malformed."""


_DEFAULT_CONTRACTS = "contracts/*.yml"
_DEFAULT_RAW_OUTPUT = "models/raw"
_DEFAULT_STAGING_OUTPUT = "models/staging"
_KEYS = (
    "contracts",
    "raw_output",
    "staging_output",
    "schema_contracts",
    "schema_output",
    "sources_contracts",
    "sources_output",
    "tests_output",
)


def _key_notices(raw: dict) -> list[Issue]:
    notices = []
    for key in raw:
        if key in _KEYS:
            continue
        near = difflib.get_close_matches(str(key), _KEYS, n=1)
        hint = f"; did you mean {near[0]!r}?" if near else ""
        notices.append(
            Issue(
                "config",
                f"unknown key {key!r} is ignored{hint}",
                severity="warning",
            )
        )
    for kind in ("schema", "sources"):
        pair = (f"{kind}_contracts", f"{kind}_output")
        set_, unset = pair if raw.get(pair[0]) else pair[::-1]
        if raw.get(set_) and not raw.get(unset):
            notices.append(
                Issue(
                    "config",
                    f"{set_!r} is set without {unset!r}; run and check"
                    " skip it. Set both or neither",
                    severity="warning",
                )
            )
    return notices


@dataclass
class ModelgenConfig:
    root: Path
    contracts_glob: str
    raw_output: Path
    staging_output: Path
    schema_contracts: str | None = None
    schema_output: Path | None = None
    sources_contracts: str | None = None
    sources_output: Path | None = None
    tests_output: Path | None = None
    notices: list[tuple[Path, Issue]] = field(default_factory=list)

    def contract_files(self) -> list[Path]:
        return sorted(self.root.glob(self.contracts_glob))

    def schema_contract_files(self) -> list[Path]:
        """Contracts for tables the project produces (schema mode)."""
        if not self.schema_contracts:
            return []
        return sorted(self.root.glob(self.schema_contracts))

    def sources_contract_files(self) -> list[Path]:
        """Contracts for tables the project reads (sources mode)."""
        if not self.sources_contracts:
            return []
        return sorted(self.root.glob(self.sources_contracts))

    @classmethod
    def _build(cls, root: Path, raw: dict) -> ModelgenConfig:
        return cls(
            root=root,
            contracts_glob=raw.get("contracts", _DEFAULT_CONTRACTS),
            raw_output=root / raw.get("raw_output", _DEFAULT_RAW_OUTPUT),
            staging_output=root
            / raw.get("staging_output", _DEFAULT_STAGING_OUTPUT),
            schema_contracts=raw.get("schema_contracts"),
            schema_output=(
                root / raw["schema_output"]
                if raw.get("schema_output")
                else None
            ),
            sources_contracts=raw.get("sources_contracts"),
            sources_output=(
                root / raw["sources_output"]
                if raw.get("sources_output")
                else None
            ),
            tests_output=(
                root / raw["tests_output"] if raw.get("tests_output") else None
            ),
        )

    @classmethod
    def from_file(cls, path: Path) -> ModelgenConfig:
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except (yaml.YAMLError, RecursionError) as e:
            raise ConfigError(f"Failed to parse {path}: {e}") from e
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must be a YAML mapping.")
        cfg = cls._build(path.parent, raw)
        cfg.notices = [(path, n) for n in _key_notices(raw)]
        return cfg

    @classmethod
    def discover(cls, start: Path) -> ModelgenConfig:
        """Find .modelgen.yml walking up from start; use defaults if absent."""
        for directory in [start, *start.parents]:
            candidate = directory / ".modelgen.yml"
            if candidate.exists():
                return cls.from_file(candidate)
        return cls._build(start, {})
