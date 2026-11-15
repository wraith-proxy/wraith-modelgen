from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("wraith-modelgen")
except PackageNotFoundError:
    # Used when not installed (running from source): reports 0.0.0.
    __version__ = "0.0.0"

# After __version__: modelgen.generator imports it from this module.
from modelgen.config import ConfigError, ModelgenConfig  # noqa: E402
from modelgen.contract import (  # noqa: E402
    ContractError,
    EventContract,
    Issue,
)
from modelgen.errors import ModelgenError  # noqa: E402
from modelgen.exit_codes import ExitCode  # noqa: E402
from modelgen.generator import GenerationError, Layer, generate  # noqa: E402
from modelgen.introspect import IntrospectionError, Introspector  # noqa: E402
from modelgen.lint import Finding, Severity, lint_contract  # noqa: E402
from modelgen.project import (  # noqa: E402
    ProjectResult,
    check_project,
    run_project,
)

__all__ = [
    "__version__",
    "ModelgenError",
    "ContractError",
    "ConfigError",
    "GenerationError",
    "IntrospectionError",
    "EventContract",
    "Issue",
    "Finding",
    "Severity",
    "lint_contract",
    "Layer",
    "generate",
    "ModelgenConfig",
    "Introspector",
    "run_project",
    "check_project",
    "ProjectResult",
    "ExitCode",
]
