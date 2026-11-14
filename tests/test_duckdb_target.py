"""DuckDB dialect: selected by servers[].type, no BigQuery constructs."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from modelgen.contract import ContractError, EventContract
from modelgen.dialect import (
    DuckDBDialect,
    UnknownDialectError,
    get_dialect,
)
from modelgen.generator import Layer, generate
from modelgen.introspect import ContractIntrospector
from modelgen.lint import lint_contract

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "duckdb" / "single_table.yaml"
GOLDEN = HERE / "golden" / "duckdb"
_RUNNER = "from modelgen.cli import main; main()"


def _render(contract: EventContract, out: Path) -> None:
    introspector = ContractIntrospector(contract)
    for layer in (Layer.RAW, Layer.STAGING):
        generate(contract, introspector, out, layer)


def test_duckdb_contract_generates_models(tmp_path):
    contract = EventContract.from_file(FIXTURE)
    assert not [f for f in lint_contract(contract) if f.is_error]
    _render(contract, tmp_path)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == sorted(p.name for p in GOLDEN.iterdir())
    for name in names:
        assert (tmp_path / name).read_text() == (GOLDEN / name).read_text()


def test_no_bigquery_constructs_in_duckdb_output(tmp_path):
    _render(EventContract.from_file(FIXTURE), tmp_path)
    pattern = re.compile(
        r"insert_overwrite|partition_by|cluster_by|FLOAT64|INT64|STRING|ds "
    )
    for p in tmp_path.iterdir():
        assert not pattern.search(p.read_text()), p.name


def test_local_parquet_server_selects_duckdb(tmp_path):
    text = FIXTURE.read_text().replace(
        "    type: duckdb\n    database: warehouse.duckdb\n    schema: main\n",
        "    type: local\n    format: parquet\n"
        "    path: data/bus_ping_summary.parquet\n",
    )
    contract = EventContract.from_text(text)
    assert isinstance(get_dialect(contract.source.server_type), DuckDBDialect)
    _render(contract, tmp_path)
    raw_yml = (tmp_path / "raw__bus_ping_summary.yml").read_text()
    assert "external_location: data/bus_ping_summary.parquet" in raw_yml
    assert "incremental" not in raw_yml


def test_unknown_server_type_still_errors_listing_dialects():
    with pytest.raises(UnknownDialectError) as exc:
        get_dialect("oracle")
    for t in ("bigquery", "duckdb", "local"):
        assert t in str(exc.value)
    text = FIXTURE.read_text().replace("type: duckdb", "type: custom")
    with pytest.raises(ContractError) as exc2:
        EventContract.from_text(text)
    assert "duckdb" in str(exc2.value)


def test_duckdb_physical_types():
    d = get_dialect("duckdb")
    for ok in (
        "VARCHAR", "DOUBLE", "FLOAT", "REAL", "BIGINT", "INTEGER",
        "SMALLINT", "TINYINT", "BOOLEAN", "DATE", "TIMESTAMP",
        "TIMESTAMPTZ", "TIME", "DECIMAL(18,3)", "HUGEINT", "UUID", "BLOB",
        "VARCHAR[]", "INTEGER[3]", "LIST(INTEGER)",
        "STRUCT(a INTEGER, b VARCHAR)", "decimal(10, 2)",
    ):  # fmt: skip
        assert d.is_valid_physical_type(ok), ok
    for bad in ("STRING", "INT64", "FLOAT64", "ARRAY<INT64>", "WIBBLE"):
        assert not d.is_valid_physical_type(bad), bad
    assert d.cast_type("DECIMAL(18,3)") == "DECIMAL(18,3)"
    assert d.has_partition_column() is False


def test_duckdb_lint_names_dialect_in_type_message():
    text = FIXTURE.read_text().replace(
        "physicalType: DOUBLE", "physicalType: FLOAT64"
    )
    msgs = [
        f.message
        for f in lint_contract(EventContract.from_text(text))
        if f.code == "invalid-column-type"
    ]
    assert msgs and "not a DuckDB type" in msgs[0]


def test_duckdb_rejects_partition_config():
    text = FIXTURE.read_text().replace(
        "      - property: modelgenStaging\n",
        "      - property: modelgenRaw\n"
        "        value:\n"
        "          partitionBy:\n"
        "            field: bucket_end\n"
        "      - property: modelgenStaging\n",
    )
    codes = {f.code for f in lint_contract(EventContract.from_text(text))}
    assert "dialect-unsupported-config" in codes


def test_duckdb_view_default_and_table_when_declared(tmp_path):
    contract = EventContract.from_file(FIXTURE)
    d = get_dialect("duckdb")
    assert "materialized='view'" in d.render_dbt_config(contract, "raw")
    assert "materialized='table'" in d.render_dbt_config(contract, "staging")


def _gen(contract: Path, out: Path, hashseed: str) -> None:
    env = {**os.environ, "PYTHONHASHSEED": hashseed, "LC_ALL": "C"}
    for layer in ("raw", "staging"):
        subprocess.run(
            [
                sys.executable, "-c", _RUNNER, "gen", str(contract),
                f"--{layer}", "--from-contract", "-o", str(out),
            ],
            cwd=contract.parent,
            env=env,
            check=True,
            capture_output=True,
        )  # fmt: skip


def test_duckdb_output_deterministic(tmp_path):
    _gen(FIXTURE, tmp_path / "a", "0")
    _gen(FIXTURE, tmp_path / "b", "1")
    r = subprocess.run(
        ["diff", "-r", str(tmp_path / "a"), str(tmp_path / "b")],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout
    assert any((tmp_path / "a").glob("*.sql"))
