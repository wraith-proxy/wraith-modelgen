"""
CLI integration tests.

Uses typer.testing.CliRunner — no live BigQuery, no filesystem side-effects
beyond tmp_path. FakeIntrospector is injected by monkeypatching BigQueryIntrospector.
"""
from pathlib import Path

import pytest
from typer.testing import CliRunner

from modelgen.cli import app
from modelgen.introspect import FakeIntrospector, IntrospectedColumn

runner = CliRunner()

CONTRACT_YAML = """
version: "1"
event:
  name: user_signed_up
  description: "Fired when a user signs up."
  unique_key: EVENT_ID
  loaded_at_field: RECEIVED_AT
  source:
    project: my-project
    dataset: landing
    table: user_signups_raw
  raw:
    dataset: raw
    incremental_strategy: merge
    dedup: true
  staging:
    dataset: staging
    incremental_strategy: merge
    columns:
      - source: EVENT_ID
        name: event_id
        type: STRING
        tests: [not_null, unique]
      - source: RECEIVED_AT
        name: received_at
        type: TIMESTAMP
        tests: [not_null]
"""

_FAKE_COLUMNS = [
    IntrospectedColumn("EVENT_ID", "STRING", "REQUIRED"),
    IntrospectedColumn("RECEIVED_AT", "STRING"),
    IntrospectedColumn("EXTRA_COL", "STRING"),
]


@pytest.fixture
def contract_file(tmp_path) -> Path:
    p = tmp_path / "contract.yml"
    p.write_text(CONTRACT_YAML)
    return p


@pytest.fixture(autouse=True)
def patch_introspector(monkeypatch):
    fake = FakeIntrospector({
        ("my-project", "landing", "user_signups_raw"): _FAKE_COLUMNS,
    })
    monkeypatch.setattr(
        "modelgen.introspect.BigQueryIntrospector",
        lambda: fake,
    )


# ---------------------------------------------------------------------------
# Help and version
# ---------------------------------------------------------------------------

class TestHelp:
    def test_no_args_shows_help(self):
        result = runner.invoke(app, [])
        assert result.exit_code in (0, 2)  # Click raises NoArgsIsHelpError → 2
        assert "gen" in result.output
        assert "validate" in result.output
        assert "run" in result.output

    def test_version_flag(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "modelgen" in result.output

    def test_gen_help(self):
        result = runner.invoke(app, ["gen", "--help"])
        assert result.exit_code == 0
        assert "--raw" in result.output
        assert "--staging" in result.output
        assert "--dry-run" in result.output

    def test_validate_help(self):
        result = runner.invoke(app, ["validate", "--help"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# gen command
# ---------------------------------------------------------------------------

class TestGenCommand:
    def test_raw_layer_exits_zero(self, contract_file, tmp_path):
        result = runner.invoke(app, ["gen", str(contract_file), "--raw", "-o", str(tmp_path)])
        assert result.exit_code == 0, result.output

    def test_staging_layer_exits_zero(self, contract_file, tmp_path):
        result = runner.invoke(app, ["gen", str(contract_file), "--staging", "-o", str(tmp_path)])
        assert result.exit_code == 0, result.output

    def test_raw_writes_files(self, contract_file, tmp_path):
        runner.invoke(app, ["gen", str(contract_file), "--raw", "-o", str(tmp_path)])
        assert (tmp_path / "raw__user_signed_up.sql").exists()
        assert (tmp_path / "raw__user_signed_up.yml").exists()

    def test_staging_writes_files(self, contract_file, tmp_path):
        runner.invoke(app, ["gen", str(contract_file), "--staging", "-o", str(tmp_path)])
        assert (tmp_path / "stg__user_signed_up.sql").exists()
        assert (tmp_path / "stg__user_signed_up.yml").exists()

    def test_dry_run_writes_nothing(self, contract_file, tmp_path):
        result = runner.invoke(
            app, ["gen", str(contract_file), "--raw", "--dry-run", "-o", str(tmp_path)]
        )
        assert result.exit_code == 0
        assert not (tmp_path / "raw__user_signed_up.sql").exists()

    def test_dry_run_label_in_output(self, contract_file, tmp_path):
        result = runner.invoke(
            app, ["gen", str(contract_file), "--raw", "--dry-run", "-o", str(tmp_path)]
        )
        assert "dry-run" in result.output

    def test_schema_evolution_panel_shown(self, contract_file, tmp_path):
        result = runner.invoke(
            app, ["gen", str(contract_file), "--staging", "-o", str(tmp_path)]
        )
        assert "EXTRA_COL" in result.output

    def test_neither_flag_exits_2(self, contract_file, tmp_path):
        result = runner.invoke(app, ["gen", str(contract_file), "-o", str(tmp_path)])
        assert result.exit_code == 2

    def test_both_flags_exits_2(self, contract_file, tmp_path):
        result = runner.invoke(
            app, ["gen", str(contract_file), "--raw", "--staging", "-o", str(tmp_path)]
        )
        assert result.exit_code == 2

    def test_missing_contract_file_exits_nonzero(self, tmp_path):
        result = runner.invoke(
            app, ["gen", str(tmp_path / "nope.yml"), "--raw"]
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# validate command
# ---------------------------------------------------------------------------

class TestValidateCommand:
    def test_valid_contract_exits_zero(self, contract_file):
        result = runner.invoke(app, ["validate", str(contract_file)])
        assert result.exit_code == 0
        assert "OK" in result.output

    def test_valid_contract_shows_summary(self, contract_file):
        result = runner.invoke(app, ["validate", str(contract_file)])
        assert "user_signed_up" in result.output

    def test_invalid_contract_exits_1(self, tmp_path):
        p = tmp_path / "bad.yml"
        p.write_text('version: "1"\nevent:\n  name: x\n  unique_key: id\n  loaded_at_field: t\n')
        result = runner.invoke(app, ["validate", str(p)])
        assert result.exit_code == 1
        assert "Invalid contract" in result.output

    def test_malformed_yaml_exits_1(self, tmp_path):
        p = tmp_path / "broken.yml"
        p.write_text("not: : valid: yaml: :")
        result = runner.invoke(app, ["validate", str(p)])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# run command
# ---------------------------------------------------------------------------

_RAW_ONLY_CONTRACT = """\
version: "1"
event:
  name: raw_only_event
  unique_key: EVENT_ID
  loaded_at_field: RECEIVED_AT
  source:
    project: my-project
    dataset: landing
    table: user_signups_raw
  raw:
    dataset: raw
  staging:
    dataset: staging
    columns: []
"""


class TestRunCommand:
    @pytest.fixture
    def repo(self, tmp_path) -> Path:
        """Minimal repo layout: contracts/ dir with one full contract."""
        (tmp_path / "contracts").mkdir()
        (tmp_path / "contracts" / "user_signed_up.yml").write_text(CONTRACT_YAML)
        (tmp_path / ".modelgen.yml").write_text(
            "contracts: contracts/*.yml\n"
            "raw_output: models/raw\n"
            "staging_output: models/staging\n"
        )
        return tmp_path

    def test_run_exits_zero(self, repo):
        result = runner.invoke(app, ["run", "--config", str(repo / ".modelgen.yml")])
        assert result.exit_code == 0, result.output

    def test_run_shows_help(self):
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "--config" in result.output
        assert "--dry-run" in result.output

    def test_run_writes_raw_files(self, repo):
        runner.invoke(app, ["run", "--config", str(repo / ".modelgen.yml")])
        raw_dir = repo / "models" / "raw"
        assert (raw_dir / "raw__user_signed_up.sql").exists()
        assert (raw_dir / "raw__user_signed_up.yml").exists()

    def test_run_writes_staging_files(self, repo):
        runner.invoke(app, ["run", "--config", str(repo / ".modelgen.yml")])
        stg_dir = repo / "models" / "staging"
        assert (stg_dir / "stg__user_signed_up.sql").exists()
        assert (stg_dir / "stg__user_signed_up.yml").exists()

    def test_run_dry_run_writes_nothing(self, repo):
        result = runner.invoke(
            app, ["run", "--config", str(repo / ".modelgen.yml"), "--dry-run"]
        )
        assert result.exit_code == 0
        assert not (repo / "models").exists()

    def test_run_dry_run_label_in_output(self, repo):
        result = runner.invoke(
            app, ["run", "--config", str(repo / ".modelgen.yml"), "--dry-run"]
        )
        assert "dry-run" in result.output

    def test_run_no_contracts_exits_zero(self, tmp_path):
        cfg = tmp_path / ".modelgen.yml"
        cfg.write_text("contracts: contracts/*.yml\n")
        result = runner.invoke(app, ["run", "--config", str(cfg)])
        assert result.exit_code == 0
        assert "No contracts found" in result.output

    def test_run_multiple_contracts(self, repo):
        second = CONTRACT_YAML.replace(
            "name: user_signed_up", "name: page_viewed"
        ).replace("table: user_signups_raw", "table: user_signups_raw")
        (repo / "contracts" / "page_viewed.yml").write_text(second)
        result = runner.invoke(app, ["run", "--config", str(repo / ".modelgen.yml")])
        assert result.exit_code == 0, result.output
        assert "raw__user_signed_up.sql" in result.output
        assert "raw__page_viewed.sql" in result.output

    def test_run_invalid_contract_exits_1(self, tmp_path):
        (tmp_path / "contracts").mkdir()
        (tmp_path / "contracts" / "bad.yml").write_text("not: : valid: yaml: :")
        cfg = tmp_path / ".modelgen.yml"
        cfg.write_text("contracts: contracts/*.yml\n")
        result = runner.invoke(app, ["run", "--config", str(cfg)])
        assert result.exit_code == 1

    def test_run_skips_staging_when_no_columns(self, tmp_path):
        (tmp_path / "contracts").mkdir()
        (tmp_path / "contracts" / "raw_only.yml").write_text(_RAW_ONLY_CONTRACT)
        cfg = tmp_path / ".modelgen.yml"
        cfg.write_text("contracts: contracts/*.yml\n")
        result = runner.invoke(app, ["run", "--config", str(cfg)])
        assert result.exit_code == 0, result.output
        assert not (tmp_path / "models" / "staging").exists()
        assert (tmp_path / "models" / "raw").exists()

    def test_run_uses_custom_output_dirs(self, tmp_path):
        (tmp_path / "contracts").mkdir()
        (tmp_path / "contracts" / "user_signed_up.yml").write_text(CONTRACT_YAML)
        cfg = tmp_path / ".modelgen.yml"
        cfg.write_text(
            "contracts: contracts/*.yml\n"
            "raw_output: out/raw\n"
            "staging_output: out/staging\n"
        )
        runner.invoke(app, ["run", "--config", str(cfg)])
        assert (tmp_path / "out" / "raw" / "raw__user_signed_up.sql").exists()
        assert (tmp_path / "out" / "staging" / "stg__user_signed_up.sql").exists()
