"""
CLI integration tests.

Uses typer.testing.CliRunner — no live BigQuery, no filesystem side-effects
beyond tmp_path. FakeIntrospector is injected by monkeypatching
BigQueryIntrospector.
"""

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from modelgen.cli import app
from modelgen.introspect import FakeIntrospector, IntrospectedColumn

runner = CliRunner()


def strip_ansi(text: str) -> str:
    """Strip ANSI escape codes from output.

    Rich renders pretty panels in CI with full ANSI even when nobody asked.
    This is the price we pay for having a CLI that looks nice in a terminal
    and also needs to be testable by machines that don't have feelings.
    """
    return re.sub(r"\x1b\[[0-9;]*[mGKHF]", "", text)


CONTRACT_YAML = """
version: "1.0.0"
kind: DataContract
apiVersion: v3.0.2
id: 11111111-1111-1111-1111-111111111111
status: active
name: user_signed_up
description:
  purpose: "Fired when a user signs up."
servers:
  - server: landing
    type: bigquery
    project: my-project
    dataset: landing
schema:
  - name: user_signed_up
    physicalName: user_signups_raw
    physicalType: table
    description: "Fired when a user signs up."
    customProperties:
      - property: modelgenLoadedAtField
        value: RECEIVED_AT
      - property: modelgenRaw
        value:
          dataset: raw
          incrementalStrategy: merge
          dedup: true
      - property: modelgenStaging
        value:
          dataset: staging
          incrementalStrategy: merge
    properties:
      - name: event_id
        physicalName: EVENT_ID
        logicalType: string
        physicalType: STRING
        primaryKey: true
        primaryKeyPosition: 1
        required: true
        unique: true
      - name: received_at
        physicalName: RECEIVED_AT
        logicalType: date
        physicalType: TIMESTAMP
        required: true
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
    fake = FakeIntrospector(
        {
            ("my-project", "landing", "user_signups_raw"): _FAKE_COLUMNS,
        }
    )
    monkeypatch.setattr(
        "modelgen.introspect.BigQueryIntrospector",
        lambda: fake,
    )
    monkeypatch.setattr(
        "modelgen.introspect.has_default_credentials",
        lambda: True,
    )


# ---------------------------------------------------------------------------
# Help and version
# ---------------------------------------------------------------------------


class TestHelp:
    def test_no_args_shows_help(self):
        result = runner.invoke(app, [])
        assert result.exit_code in (0, 2)  # Click raises NoArgsIsHelpError → 2
        output = strip_ansi(result.output)
        assert "gen" in output
        assert "validate" in output
        assert "run" in output

    def test_version_flag(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "modelgen" in strip_ansi(result.output)

    def test_gen_help(self):
        result = runner.invoke(app, ["gen", "--help"])
        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "--raw" in output
        assert "--staging" in output
        assert "--dry-run" in output

    def test_validate_help(self):
        result = runner.invoke(app, ["validate", "--help"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# gen command
# ---------------------------------------------------------------------------


class TestGenCommand:
    def test_raw_layer_exits_zero(self, contract_file, tmp_path):
        result = runner.invoke(
            app, ["gen", str(contract_file), "--raw", "-o", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output

    def test_staging_layer_exits_zero(self, contract_file, tmp_path):
        result = runner.invoke(
            app, ["gen", str(contract_file), "--staging", "-o", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output

    def test_raw_writes_files(self, contract_file, tmp_path):
        runner.invoke(
            app, ["gen", str(contract_file), "--raw", "-o", str(tmp_path)]
        )
        assert (tmp_path / "raw__user_signed_up.sql").exists()
        assert (tmp_path / "raw__user_signed_up.yml").exists()

    def test_staging_writes_files(self, contract_file, tmp_path):
        runner.invoke(
            app, ["gen", str(contract_file), "--staging", "-o", str(tmp_path)]
        )
        assert (tmp_path / "stg__user_signed_up.sql").exists()
        assert (tmp_path / "stg__user_signed_up.yml").exists()

    def test_dry_run_writes_nothing(self, contract_file, tmp_path):
        result = runner.invoke(
            app,
            [
                "gen",
                str(contract_file),
                "--raw",
                "--dry-run",
                "-o",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0
        assert not (tmp_path / "raw__user_signed_up.sql").exists()

    def test_dry_run_label_in_output(self, contract_file, tmp_path):
        result = runner.invoke(
            app,
            [
                "gen",
                str(contract_file),
                "--raw",
                "--dry-run",
                "-o",
                str(tmp_path),
            ],
        )
        assert "dry-run" in strip_ansi(result.output)

    def test_schema_evolution_panel_shown(self, contract_file, tmp_path):
        result = runner.invoke(
            app, ["gen", str(contract_file), "--staging", "-o", str(tmp_path)]
        )
        assert "EXTRA_COL" in strip_ansi(result.output)

    def test_neither_flag_exits_2(self, contract_file, tmp_path):
        result = runner.invoke(
            app, ["gen", str(contract_file), "-o", str(tmp_path)]
        )
        assert result.exit_code == 2

    def test_both_flags_exits_2(self, contract_file, tmp_path):
        result = runner.invoke(
            app,
            [
                "gen",
                str(contract_file),
                "--raw",
                "--staging",
                "-o",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 2

    def test_missing_contract_file_exits_nonzero(self, tmp_path):
        result = runner.invoke(
            app, ["gen", str(tmp_path / "nope.yml"), "--raw"]
        )
        assert result.exit_code != 0

    def test_from_contract_flag_raw_exits_zero(self, contract_file, tmp_path):
        result = runner.invoke(
            app,
            [
                "gen",
                str(contract_file),
                "--raw",
                "--from-contract",
                "-o",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

    def test_from_contract_flag_writes_files(self, contract_file, tmp_path):
        runner.invoke(
            app,
            [
                "gen",
                str(contract_file),
                "--raw",
                "--from-contract",
                "-o",
                str(tmp_path),
            ],
        )
        assert (tmp_path / "raw__user_signed_up.sql").exists()
        assert (tmp_path / "raw__user_signed_up.yml").exists()

    def test_from_contract_flag_staging_exits_zero(
        self, contract_file, tmp_path
    ):
        result = runner.invoke(
            app,
            [
                "gen",
                str(contract_file),
                "--staging",
                "--from-contract",
                "-o",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

    def test_auto_fallback_when_no_credentials(
        self, contract_file, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "modelgen.introspect.has_default_credentials", lambda: False
        )
        result = runner.invoke(
            app,
            ["gen", str(contract_file), "--raw", "-o", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        output = strip_ansi(result.output)
        assert "No GCP credentials" in output
        assert (tmp_path / "raw__user_signed_up.sql").exists()


# ---------------------------------------------------------------------------
# validate command
# ---------------------------------------------------------------------------


class TestValidateCommand:
    def test_valid_contract_exits_zero(self, contract_file):
        result = runner.invoke(app, ["validate", str(contract_file)])
        assert result.exit_code == 0
        assert "OK" in strip_ansi(result.output)

    def test_valid_contract_shows_summary(self, contract_file):
        result = runner.invoke(app, ["validate", str(contract_file)])
        assert "user_signed_up" in strip_ansi(result.output)

    def test_invalid_contract_exits_1(self, tmp_path):
        p = tmp_path / "bad.yml"
        p.write_text(
            'version: "1"\nevent:\n  name: x\n'
            "  unique_key: id\n  loaded_at_field: t\n"
        )
        result = runner.invoke(app, ["validate", str(p)])
        assert result.exit_code == 1
        assert "Invalid contract" in strip_ansi(result.output)

    def test_malformed_yaml_exits_1(self, tmp_path):
        p = tmp_path / "broken.yml"
        p.write_text("not: : valid: yaml: :")
        result = runner.invoke(app, ["validate", str(p)])
        assert result.exit_code == 1

    def test_semantic_error_exits_1(self, tmp_path):
        p = tmp_path / "bad_strategy.yml"
        p.write_text(
            CONTRACT_YAML.replace(
                "incrementalStrategy: merge",
                "incrementalStrategy: mrege",
            )
        )
        result = runner.invoke(app, ["validate", str(p)])
        assert result.exit_code == 1
        output = strip_ansi(result.output)
        assert "invalid-raw-strategy" in output
        assert "FAIL" in output

    def test_warnings_pass_without_strict(self, tmp_path):
        p = tmp_path / "warns.yml"
        # Contract columns carry no descriptions → warnings only
        p.write_text(CONTRACT_YAML)
        result = runner.invoke(app, ["validate", str(p)])
        assert result.exit_code == 0
        assert "OK" in strip_ansi(result.output)

    def test_strict_turns_warnings_into_failure(self, tmp_path):
        p = tmp_path / "warns.yml"
        p.write_text(CONTRACT_YAML)
        result = runner.invoke(app, ["validate", str(p), "--strict"])
        assert result.exit_code == 1
        assert "FAIL" in strip_ansi(result.output)

    def test_all_errors_reported_in_one_pass(self, tmp_path):
        p = tmp_path / "multi.yml"
        p.write_text(
            CONTRACT_YAML.replace(
                "incrementalStrategy: merge",
                "incrementalStrategy: mrege",
            ).replace("physicalType: STRING", "physicalType: VARCHAR")
        )
        result = runner.invoke(app, ["validate", str(p)])
        output = strip_ansi(result.output)
        assert "invalid-raw-strategy" in output
        assert "invalid-column-type" in output


class TestGenLintGate:
    def test_gen_refuses_contract_with_lint_errors(
        self, contract_file, tmp_path
    ):
        contract_file.write_text(
            contract_file.read_text().replace(
                "incrementalStrategy: merge",
                "incrementalStrategy: mrege",
            )
        )
        result = runner.invoke(
            app, ["gen", str(contract_file), "--raw", "-o", str(tmp_path)]
        )
        assert result.exit_code == 1
        assert "failed lint" in strip_ansi(result.output)
        assert not (tmp_path / "raw__user_signed_up.sql").exists()


# ---------------------------------------------------------------------------
# run command
# ---------------------------------------------------------------------------

_RAW_ONLY_CONTRACT = """\
version: "1.0.0"
kind: DataContract
apiVersion: v3.0.2
id: 22222222-2222-2222-2222-222222222222
status: active
name: raw_only_event
servers:
  - server: landing
    type: bigquery
    project: my-project
    dataset: landing
schema:
  - name: raw_only_event
    physicalName: user_signups_raw
    physicalType: table
    customProperties:
      - property: modelgenLoadedAtField
        value: RECEIVED_AT
      - property: modelgenRaw
        value:
          dataset: raw
      - property: modelgenStaging
        value:
          generate: false
    properties:
      - name: event_id
        physicalName: EVENT_ID
        logicalType: string
        physicalType: STRING
        primaryKey: true
        primaryKeyPosition: 1
        required: true
        unique: true
      - name: received_at
        physicalName: RECEIVED_AT
        logicalType: date
        physicalType: TIMESTAMP
        required: true
"""


class TestRunCommand:
    @pytest.fixture
    def repo(self, tmp_path) -> Path:
        """Minimal repo layout: contracts/ dir with one full contract."""
        (tmp_path / "contracts").mkdir()
        (tmp_path / "contracts" / "user_signed_up.yml").write_text(
            CONTRACT_YAML
        )
        (tmp_path / ".modelgen.yml").write_text(
            "contracts: contracts/*.yml\n"
            "raw_output: models/raw\n"
            "staging_output: models/staging\n"
        )
        return tmp_path

    def test_run_exits_zero(self, repo):
        result = runner.invoke(
            app, ["run", "--config", str(repo / ".modelgen.yml")]
        )
        assert result.exit_code == 0, result.output

    def test_run_shows_help(self):
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "--config" in output
        assert "--dry-run" in output

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
        assert "dry-run" in strip_ansi(result.output)

    def test_run_no_contracts_exits_zero(self, tmp_path):
        cfg = tmp_path / ".modelgen.yml"
        cfg.write_text("contracts: contracts/*.yml\n")
        result = runner.invoke(app, ["run", "--config", str(cfg)])
        assert result.exit_code == 0
        assert "No contracts found" in strip_ansi(result.output)

    def test_run_multiple_contracts(self, repo):
        second = CONTRACT_YAML.replace(
            "name: user_signed_up", "name: page_viewed"
        ).replace("table: user_signups_raw", "table: user_signups_raw")
        (repo / "contracts" / "page_viewed.yml").write_text(second)
        result = runner.invoke(
            app, ["run", "--config", str(repo / ".modelgen.yml")]
        )
        assert result.exit_code == 0, result.output
        assert "raw__user_signed_up.sql" in strip_ansi(result.output)
        assert "raw__page_viewed.sql" in strip_ansi(result.output)

    def test_run_lint_error_exits_1(self, tmp_path):
        (tmp_path / "contracts").mkdir()
        (tmp_path / "contracts" / "bad.yml").write_text(
            CONTRACT_YAML.replace(
                "incrementalStrategy: merge",
                "incrementalStrategy: mrege",
            )
        )
        cfg = tmp_path / ".modelgen.yml"
        cfg.write_text("contracts: contracts/*.yml\n")
        result = runner.invoke(app, ["run", "--config", str(cfg)])
        assert result.exit_code == 1
        assert "lint" in strip_ansi(result.output)

    def test_run_invalid_contract_exits_1(self, tmp_path):
        (tmp_path / "contracts").mkdir()
        (tmp_path / "contracts" / "bad.yml").write_text(
            "not: : valid: yaml: :"
        )
        cfg = tmp_path / ".modelgen.yml"
        cfg.write_text("contracts: contracts/*.yml\n")
        result = runner.invoke(app, ["run", "--config", str(cfg)])
        assert result.exit_code == 1

    def test_run_skips_staging_when_no_columns(self, tmp_path):
        (tmp_path / "contracts").mkdir()
        (tmp_path / "contracts" / "raw_only.yml").write_text(
            _RAW_ONLY_CONTRACT
        )
        cfg = tmp_path / ".modelgen.yml"
        cfg.write_text("contracts: contracts/*.yml\n")
        result = runner.invoke(app, ["run", "--config", str(cfg)])
        assert result.exit_code == 0, result.output
        assert not (tmp_path / "models" / "staging").exists()
        assert (tmp_path / "models" / "raw").exists()


class TestCheckCommand:
    @pytest.fixture
    def repo(self, tmp_path) -> Path:
        (tmp_path / "contracts").mkdir()
        (tmp_path / "contracts" / "user_signed_up.yml").write_text(
            CONTRACT_YAML
        )
        (tmp_path / ".modelgen.yml").write_text(
            "contracts: contracts/*.yml\n"
            "raw_output: models/raw\n"
            "staging_output: models/staging\n"
        )
        return tmp_path

    def _run_then_check(self, repo, *check_args):
        cfg = str(repo / ".modelgen.yml")
        gen = runner.invoke(app, ["run", "--config", cfg])
        assert gen.exit_code == 0, gen.output
        return runner.invoke(app, ["check", "--config", cfg, *check_args])

    def test_clean_repo_exits_zero(self, repo):
        result = self._run_then_check(repo)
        assert result.exit_code == 0, result.output
        assert "in sync" in strip_ansi(result.output)

    def test_check_writes_nothing(self, repo):
        cfg = str(repo / ".modelgen.yml")
        result = runner.invoke(app, ["check", "--config", cfg])
        assert result.exit_code == 1
        assert not (repo / "models").exists()

    def test_missing_files_exit_1(self, repo):
        cfg = str(repo / ".modelgen.yml")
        result = runner.invoke(app, ["check", "--config", cfg])
        assert result.exit_code == 1
        assert "missing" in strip_ansi(result.output)

    def test_drifted_file_exits_1(self, repo):
        result = self._run_then_check(repo)
        assert result.exit_code == 0
        sql = repo / "models" / "raw" / "raw__user_signed_up.sql"
        sql.write_text(sql.read_text() + "\n-- hand edit\n")
        check = runner.invoke(
            app, ["check", "--config", str(repo / ".modelgen.yml")]
        )
        assert check.exit_code == 1
        assert "drift" in strip_ansi(check.output)

    def test_diff_flag_shows_unified_diff(self, repo):
        self._run_then_check(repo)
        sql = repo / "models" / "raw" / "raw__user_signed_up.sql"
        sql.write_text(sql.read_text() + "\n-- hand edit\n")
        check = runner.invoke(
            app,
            ["check", "--config", str(repo / ".modelgen.yml"), "--diff"],
        )
        assert check.exit_code == 1
        output = strip_ansi(check.output)
        assert "-- hand edit" in output
        assert "expected" in output

    def test_check_reports_lint_errors(self, tmp_path):
        (tmp_path / "contracts").mkdir()
        (tmp_path / "contracts" / "bad.yml").write_text(
            CONTRACT_YAML.replace(
                "incrementalStrategy: merge",
                "incrementalStrategy: mrege",
            )
        )
        cfg = tmp_path / ".modelgen.yml"
        cfg.write_text("contracts: contracts/*.yml\n")
        result = runner.invoke(app, ["check", "--config", str(cfg)])
        assert result.exit_code == 1
        assert "lint" in strip_ansi(result.output)

    def test_check_no_contracts_exits_zero(self, tmp_path):
        cfg = tmp_path / ".modelgen.yml"
        cfg.write_text("contracts: contracts/*.yml\n")
        result = runner.invoke(app, ["check", "--config", str(cfg)])
        assert result.exit_code == 0
        assert "No contracts found" in strip_ansi(result.output)

    def test_check_skips_staging_when_no_columns(self, tmp_path):
        (tmp_path / "contracts").mkdir()
        (tmp_path / "contracts" / "raw_only.yml").write_text(
            _RAW_ONLY_CONTRACT
        )
        cfg = tmp_path / ".modelgen.yml"
        cfg.write_text("contracts: contracts/*.yml\n")
        runner.invoke(app, ["run", "--config", str(cfg)])
        result = runner.invoke(app, ["check", "--config", str(cfg)])
        assert result.exit_code == 0, result.output
        assert "staging" not in strip_ansi(result.output).lower().replace(
            "models/staging", ""
        )


class TestRunCommandOutputDirs:
    def test_run_uses_custom_output_dirs(self, tmp_path):
        (tmp_path / "contracts").mkdir()
        (tmp_path / "contracts" / "user_signed_up.yml").write_text(
            CONTRACT_YAML
        )
        cfg = tmp_path / ".modelgen.yml"
        cfg.write_text(
            "contracts: contracts/*.yml\n"
            "raw_output: out/raw\n"
            "staging_output: out/staging\n"
        )
        runner.invoke(app, ["run", "--config", str(cfg)])
        assert (tmp_path / "out" / "raw" / "raw__user_signed_up.sql").exists()
        assert (
            tmp_path / "out" / "staging" / "stg__user_signed_up.sql"
        ).exists()
