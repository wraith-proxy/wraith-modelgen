"""
Project-level configuration for modelgen.

Reads .modelgen.yml from the repo root (or walks up from cwd to find it).
All paths in the config file are relative to the .modelgen.yml location.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class ConfigError(ValueError):
    """Raised when .modelgen.yml is malformed."""


_DEFAULT_CONTRACTS = "contracts/*.yml"
_DEFAULT_RAW_OUTPUT = "models/raw"
_DEFAULT_STAGING_OUTPUT = "models/staging"


@dataclass
class ModelgenConfig:
    root: Path
    contracts_glob: str
    raw_output: Path
    staging_output: Path

    def contract_files(self) -> list[Path]:
        return sorted(self.root.glob(self.contracts_glob))

    @classmethod
    def _build(cls, root: Path, raw: dict) -> "ModelgenConfig":
        return cls(
            root=root,
            contracts_glob=raw.get("contracts", _DEFAULT_CONTRACTS),
            raw_output=root / raw.get("raw_output", _DEFAULT_RAW_OUTPUT),
            staging_output=root / raw.get("staging_output", _DEFAULT_STAGING_OUTPUT),
        )

    @classmethod
    def from_file(cls, path: Path) -> "ModelgenConfig":
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"Failed to parse {path}: {e}") from e
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must be a YAML mapping.")
        return cls._build(path.parent, raw)

    @classmethod
    def discover(cls, start: Path) -> "ModelgenConfig":
        """Walk from start upward looking for .modelgen.yml; use defaults if not found."""
        for directory in [start, *start.parents]:
            candidate = directory / ".modelgen.yml"
            if candidate.exists():
                return cls.from_file(candidate)
        return cls._build(start, {})
