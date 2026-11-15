from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("wraith-modelgen")
except PackageNotFoundError:
    # Used when not installed (running from source): reports 0.0.0.
    __version__ = "0.0.0"

# After __version__: modelgen.generator imports it from this module.
from modelgen.config import ConfigError, ModelgenConfig  # noqa: E402
from modelgen.contract import ContractError, EventContract  # noqa: E402
from modelgen.errors import ModelgenError  # noqa: E402
from modelgen.generator import GenerationError, Layer, generate  # noqa: E402
from modelgen.introspect import IntrospectionError, Introspector  # noqa: E402
from modelgen.lint import Finding, Severity, lint_contract  # noqa: E402

__all__ = [
    "__version__",
    "ModelgenError",
    "ContractError",
    "ConfigError",
    "GenerationError",
    "IntrospectionError",
    "EventContract",
    "Finding",
    "Severity",
    "lint_contract",
    "Layer",
    "generate",
    "ModelgenConfig",
    "Introspector",
]
