"""Server selection: BigQuery preferred by default; --server on sources."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from modelgen.cli import app
from modelgen.contract import ContractError, _select_server

runner = CliRunner()
SOURCES_CONTRACT = (
    Path(__file__).parent
    / "fixtures"
    / "real_contracts"
    / "bus-positions-extract.odcs.yaml"
)


def test_bigquery_after_local_still_selected():
    servers = [
        {"server": "dev", "type": "local"},
        {"server": "prod", "type": "bigquery"},
    ]
    assert _select_server(servers)["server"] == "prod"


def test_bigquery_after_duckdb_still_selected():
    servers = [
        {"server": "dev", "type": "duckdb"},
        {"server": "prod", "type": "bigquery"},
    ]
    assert _select_server(servers)["server"] == "prod"


def test_no_bigquery_falls_back_to_first_supported():
    servers = [
        {"server": "x", "type": "postgres"},
        {"server": "dev", "type": "local"},
        {"server": "db", "type": "duckdb"},
    ]
    assert _select_server(servers)["server"] == "dev"


def test_explicit_server_unchanged():
    servers = [
        {"server": "dev", "type": "local"},
        {"server": "prod", "type": "bigquery"},
    ]
    assert _select_server(servers, "dev")["server"] == "dev"


def test_no_usable_server_still_errors():
    with pytest.raises(ContractError, match="no BigQuery server"):
        _select_server([{"server": "x", "type": "postgres"}])


def test_gen_layer_sources_rejects_server(tmp_path):
    result = runner.invoke(
        app,
        [
            "gen", str(SOURCES_CONTRACT), "--layer", "sources",
            "--server", "prod", "-o", str(tmp_path), "--dry-run",
        ],
    )  # fmt: skip
    assert result.exit_code == 2
    assert "--server" in result.output
