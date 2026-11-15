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


EXAMPLE = Path(__file__).parent.parent / "contracts" / "example_contract.yml"


def _project(root: Path, contracts: dict[str, str]) -> modelgen.ModelgenConfig:
    (root / "contracts").mkdir()
    for name, text in contracts.items():
        (root / "contracts" / name).write_text(text)
    config = root / ".modelgen.yml"
    config.write_text(
        "contracts: contracts/*.yml\n"
        "raw_output: models/raw\n"
        "staging_output: models/staging\n"
    )
    return modelgen.ModelgenConfig.from_file(config)


def _run(cfg: modelgen.ModelgenConfig, **kwargs):
    return modelgen.run_project(
        cfg,
        server=None,
        from_contract=True,
        dry_run=kwargs.get("dry_run", False),
        notify=lambda _msg: None,
    )


def _check(cfg: modelgen.ModelgenConfig):
    return modelgen.check_project(
        cfg, server=None, from_contract=True, notify=lambda _msg: None
    )


def test_run_project_lists_each_written_file_as_written(tmp_path):
    cfg = _project(tmp_path, {"example.yml": EXAMPLE.read_text()})
    result = _run(cfg)
    assert len(result.files) > 0
    assert {f.status for f in result.files} == {"written"}
    assert all(f.path.is_file() for f in result.files)


def test_run_project_dry_run_marks_dry_run_and_writes_nothing(tmp_path):
    cfg = _project(tmp_path, {"example.yml": EXAMPLE.read_text()})
    result = _run(cfg, dry_run=True)
    assert len(result.files) > 0
    assert {f.status for f in result.files} == {"dry_run"}
    assert not (tmp_path / "models").exists()


def test_check_project_straight_after_run_is_in_sync(tmp_path):
    cfg = _project(tmp_path, {"example.yml": EXAMPLE.read_text()})
    _run(cfg)
    result = _check(cfg)
    assert len(result.files) > 0
    assert {f.status for f in result.files} == {"in_sync"}
    assert result.exit_code == 0


def test_check_project_reports_drift_with_on_disk_text(tmp_path):
    cfg = _project(tmp_path, {"example.yml": EXAMPLE.read_text()})
    _run(cfg)
    target = next((tmp_path / "models" / "raw").glob("*.sql"))
    target.write_text("-- hand edit\n")
    result = _check(cfg)
    drifted = [f for f in result.files if f.status == "drift"]
    assert [f.path for f in drifted] == [target]
    assert drifted[0].on_disk == "-- hand edit\n"
    assert result.exit_code == 4


def test_check_project_reports_a_deleted_file_as_missing(tmp_path):
    cfg = _project(tmp_path, {"example.yml": EXAMPLE.read_text()})
    _run(cfg)
    target = next((tmp_path / "models" / "raw").glob("*.sql"))
    target.unlink()
    result = _check(cfg)
    missing = [f for f in result.files if f.status == "missing"]
    assert [f.path for f in missing] == [target]
    assert result.exit_code == 4


def test_lint_failing_contract_is_a_problem_while_others_render(tmp_path):
    text = EXAMPLE.read_text()
    bad = text.replace(
        "incrementalStrategy: merge", "incrementalStrategy: mrege"
    )
    cfg = _project(tmp_path, {"a_bad.yml": bad, "b_good.yml": text})
    result = _run(cfg)
    assert len(result.problems) >= 1
    assert result.exit_code == 3
    assert len(result.files) > 0
    assert {f.contract.name for f in result.files} == {"b_good.yml"}
