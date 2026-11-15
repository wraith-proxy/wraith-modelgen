"""The declared Python API: what `import modelgen` promises."""

import subprocess
import sys
from pathlib import Path

import pytest

import modelgen

API_DOCS = Path(__file__).parent.parent / "docs" / "api.md"

PUBLIC_NAMES = [
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


def test_all_is_the_declared_surface():
    assert modelgen.__all__ == PUBLIC_NAMES


def test_every_public_name_resolves():
    missing = [n for n in modelgen.__all__ if not hasattr(modelgen, n)]
    assert missing == []


def test_api_docs_headings_match_all():
    text = API_DOCS.read_text()
    headings = [
        line.removeprefix("### ").strip()
        for line in text.splitlines()
        if line.startswith("### ")
    ]
    assert headings == [n for n in modelgen.__all__ if n != "__version__"]


def test_invalid_contract_error_is_a_modelgen_error_and_value_error():
    for caught in (modelgen.ModelgenError, ValueError):
        with pytest.raises(caught):
            modelgen.EventContract.from_text("not: a contract\n")


def test_import_modelgen_leaves_warehouse_clients_unloaded():
    probe = (
        "import sys, modelgen; "
        "print([m for m in ('google.cloud.bigquery', 'duckdb') "
        "if m in sys.modules])"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip() == "[]"


def test_test_fake_is_not_shipped_in_the_package():
    with pytest.raises(ImportError):
        from modelgen.introspect import (
            FakeIntrospector,  # noqa: F401  # ty: ignore[unresolved-import]
        )
