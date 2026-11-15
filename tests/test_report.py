"""validate --format json prints one document that matches the checked-in
report schema, and text mode keeps diagnostics off stdout."""

import hashlib
import json
from pathlib import Path

import jsonschema
from typer.testing import CliRunner

from modelgen.cli import app

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "contracts" / "example_contract.yml"
SCHEMA = ROOT / "src" / "modelgen" / "schema" / "modelgen-report-v1.json"

runner = CliRunner()


def _report(stdout: str) -> dict:
    report = json.loads(stdout)
    schema = json.loads(SCHEMA.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(report)
    return report


def _contract(tmp_path: Path, text: str | None = None) -> Path:
    path = tmp_path / "contract.yml"
    path.write_text(EXAMPLE.read_text() if text is None else text)
    return path


def test_validate_json_on_a_valid_contract(tmp_path):
    contract = _contract(tmp_path)

    result = runner.invoke(
        app, ["validate", str(contract), "--format", "json"]
    )

    report = _report(result.stdout)
    assert result.exit_code == 0
    assert report["exit_code"] == 0
    assert report["ok"] is True
    assert report["command"] == "validate"
    assert report["inputs"] == [
        {
            "path": str(contract),
            "sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
        }
    ]


def test_validate_json_lists_every_schema_error_with_its_line(tmp_path):
    lines = EXAMPLE.read_text().splitlines(keepends=True)
    for number, old, new in (
        (42, "kind: DataContract", "kind: Bogus"),
        (53, "    type: bigquery", "    type: nosuchdb"),
        (124, "        logicalType: string", "        logicalType: bogus"),
    ):
        assert lines[number - 1] == old + "\n"
        lines[number - 1] = new + "\n"
    contract = _contract(tmp_path, "".join(lines))

    result = runner.invoke(
        app, ["validate", str(contract), "--format", "json"]
    )

    report = _report(result.stdout)
    assert result.exit_code == 1
    assert report["exit_code"] == 1
    assert report["ok"] is False
    assert len(report["issues"]) == 3
    assert all(i["pointer"] is not None for i in report["issues"])
    assert all(i["line"] is not None for i in report["issues"])
    located = {(i["pointer"], i["line"]) for i in report["issues"]}
    assert located == {
        ("/kind", 42),
        ("/servers/0/type", 53),
        ("/schema/0/properties/2/logicalType", 124),
    }


def test_validate_json_reports_lint_findings_with_pointers(tmp_path):
    text = EXAMPLE.read_text()
    assert "incrementalStrategy: merge" in text
    contract = _contract(
        tmp_path,
        text.replace(
            "incrementalStrategy: merge", "incrementalStrategy: mrege"
        ),
    )

    result = runner.invoke(
        app, ["validate", str(contract), "--format", "json"]
    )

    report = _report(result.stdout)
    assert result.exit_code == 3
    assert report["exit_code"] == 3
    assert all(i["pointer"] is not None for i in report["issues"])
    [strategy] = [
        i for i in report["issues"] if i["code"] == "invalid-raw-strategy"
    ]
    assert strategy["severity"] == "error"
    assert strategy["object"] == "user_signed_up"
    assert strategy["pointer"] == "/schema/0"
    assert strategy["line"] == 58
    assert strategy["exempt_reason"] is None


def test_text_mode_keeps_diagnostics_on_stderr_and_results_on_stdout(
    tmp_path,
):
    invalid = _contract(tmp_path, "name: [unclosed\n")
    valid = tmp_path / "valid.yml"
    valid.write_text(EXAMPLE.read_text())

    failed = runner.invoke(app, ["validate", str(invalid)])
    passed = runner.invoke(app, ["validate", str(valid)])

    assert failed.exit_code == 1
    assert "Invalid contract" in failed.stderr
    assert "Invalid contract" not in failed.stdout
    assert passed.exit_code == 0
    assert "OK user_signed_up" in passed.stdout
    assert "Source:   my-gcp-project.landing.user_signups_raw" in (
        passed.stdout
    )


def _project(tmp_path: Path) -> Path:
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "example.yml").write_text(EXAMPLE.read_text())
    config = tmp_path / ".modelgen.yml"
    config.write_text(
        "contracts: contracts/*.yml\n"
        "raw_output: models/raw\n"
        "staging_output: models/staging\n"
    )
    return config


def test_check_text_mode_puts_drift_notice_on_stderr_and_table_on_stdout(
    tmp_path,
):
    config = _project(tmp_path)
    runner.invoke(app, ["run", "--config", str(config)])
    generated = next((tmp_path / "models" / "raw").glob("*.sql"))
    generated.write_text(generated.read_text() + "\n-- hand edit\n")

    result = runner.invoke(app, ["check", "--config", str(config)])

    assert result.exit_code == 4
    assert "Drift detected" in result.stderr
    assert "Drift detected" not in result.stdout
    assert "drift" in result.stdout
    assert generated.name in result.stdout


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_run_json_lists_every_written_file_with_its_hash(tmp_path):
    config = _project(tmp_path)

    result = runner.invoke(
        app, ["run", "--config", str(config), "--format", "json"]
    )

    report = _report(result.stdout)
    assert result.exit_code == 0
    assert report["command"] == "run"
    assert report["inputs"] == [
        {
            "path": str(tmp_path / "contracts" / "example.yml"),
            "sha256": _sha(tmp_path / "contracts" / "example.yml"),
        }
    ]
    assert {f["layer"] for f in report["files"]} == {"raw", "staging"}
    on_disk = {str(p) for p in (tmp_path / "models").rglob("*") if p.is_file()}
    assert {f["path"] for f in report["files"]} == on_disk
    for f in report["files"]:
        assert f["status"] == "written"
        assert f["on_disk_sha256"] is None
        assert f["sha256"] == _sha(Path(f["path"]))


def test_check_json_in_sync_hashes_match_files_on_disk(tmp_path):
    config = _project(tmp_path)
    runner.invoke(app, ["run", "--config", str(config)])

    result = runner.invoke(
        app, ["check", "--config", str(config), "--format", "json"]
    )

    report = _report(result.stdout)
    assert result.exit_code == 0
    assert report["command"] == "check"
    assert report["files"]
    for f in report["files"]:
        assert f["status"] == "in_sync"
        assert f["on_disk_sha256"] is None
        assert f["sha256"] == _sha(Path(f["path"]))


def test_check_json_reports_a_changed_file_as_drift(tmp_path):
    config = _project(tmp_path)
    runner.invoke(app, ["run", "--config", str(config)])
    generated = next((tmp_path / "models" / "raw").glob("*.sql"))
    generated.write_text(generated.read_text() + "\n-- hand edit\n")

    result = runner.invoke(
        app,
        ["check", "--config", str(config), "--format", "json", "--diff"],
    )

    report = _report(result.stdout)
    assert result.exit_code == 4
    assert report["exit_code"] == 4
    assert report["ok"] is False
    [drifted] = [f for f in report["files"] if f["status"] == "drift"]
    assert drifted["path"] == str(generated)
    assert drifted["on_disk_sha256"] == _sha(generated)
    assert drifted["sha256"] != drifted["on_disk_sha256"]


REAL = ROOT / "tests" / "fixtures" / "real_contracts"


def test_schema_json_lists_written_files_with_their_hashes(tmp_path):
    contract = REAL / "bus-marts.odcs.yaml"
    out = tmp_path / "out"

    result = runner.invoke(
        app, ["schema", str(contract), "-o", str(out), "--format", "json"]
    )

    report = _report(result.stdout)
    assert result.exit_code == 0
    assert report["command"] == "schema"
    assert report["inputs"][0]["path"] == str(contract)
    assert report["files"]
    for f in report["files"]:
        assert f["layer"] == "schema"
        assert f["status"] == "written"
        assert f["on_disk_sha256"] is None
        assert f["sha256"] == _sha(Path(f["path"]))


def test_sources_json_lists_written_files_with_their_hashes(tmp_path):
    contract = REAL / "bus-positions-extract.odcs.yaml"
    out = tmp_path / "out"

    result = runner.invoke(
        app,
        ["sources", str(contract), "-o", str(out), "--format", "json"],
    )

    report = _report(result.stdout)
    assert result.exit_code == 0
    assert report["command"] == "sources"
    assert report["files"]
    for f in report["files"]:
        assert f["layer"] == "sources"
        assert f["status"] == "written"
        assert f["sha256"] == _sha(Path(f["path"]))


QUALITY_CONTRACT = """\
apiVersion: v3.2.0
kind: DataContract
id: 11111111-2222-4333-8444-555555555555
name: qr_demo
version: 1.0.0
status: draft
servers:
  - server: local
    type: duckdb
    database: ./x.duckdb
    schema: main
schema:
  - name: m
    physicalType: table
    description: d
    quality:
      - id: note
        type: text
        description: prose
    properties:
      - name: a
        logicalType: string
        physicalType: VARCHAR
"""


def test_unmapped_quality_rule_is_a_warning_and_an_error_under_strict(
    tmp_path,
):
    contract = _contract(tmp_path, QUALITY_CONTRACT)
    out = tmp_path / "out"

    lenient = runner.invoke(
        app, ["schema", str(contract), "-o", str(out), "--format", "json"]
    )
    strict = runner.invoke(
        app,
        [
            "schema", str(contract), "-o", str(out), "--strict",
            "--format", "json",
        ],
    )  # fmt: skip

    warned = _report(lenient.stdout)
    failed = _report(strict.stdout)
    assert lenient.exit_code == 0
    assert [
        (i["code"], i["severity"], i["object"]) for i in warned["issues"]
    ] == [("quality-rule-unmapped", "warning", "m.note")]
    assert strict.exit_code == 3
    assert failed["exit_code"] == 3
    assert [(i["code"], i["severity"]) for i in failed["issues"]] == [
        ("quality-rule-unmapped", "error")
    ]


def test_gen_json_prints_only_the_document_with_written_file_hashes(
    tmp_path,
):
    contract = _contract(tmp_path)
    out = tmp_path / "out"

    result = runner.invoke(
        app,
        ["gen", str(contract), "--raw", "-o", str(out), "--format", "json"],
    )

    report = _report(result.stdout)
    assert result.exit_code == 0
    assert report["command"] == "gen"
    assert report["inputs"][0]["path"] == str(contract)
    assert report["files"]
    for f in report["files"]:
        assert f["layer"] == "raw"
        assert f["status"] == "written"
        assert f["sha256"] == _sha(Path(f["path"]))


def test_gen_json_dry_run_and_lint_failure(tmp_path):
    contract = _contract(tmp_path)
    out = tmp_path / "out"

    dry = runner.invoke(
        app,
        [
            "gen", str(contract), "--staging", "-o", str(out), "--dry-run",
            "--format", "json",
        ],
    )  # fmt: skip
    bad = tmp_path / "bad.yml"
    bad.write_text(
        EXAMPLE.read_text().replace(
            "incrementalStrategy: merge", "incrementalStrategy: mrege"
        )
    )
    failed = runner.invoke(
        app, ["gen", str(bad), "--raw", "-o", str(out), "--format", "json"]
    )

    dry_report = _report(dry.stdout)
    assert dry.exit_code == 0
    assert not out.exists()
    assert [f["status"] for f in dry_report["files"]] == ["dry_run"] * len(
        dry_report["files"]
    )
    assert dry_report["files"]
    bad_report = _report(failed.stdout)
    assert failed.exit_code == 3
    assert bad_report["files"] == []
    assert [i["code"] for i in bad_report["issues"]] == [
        "invalid-raw-strategy"
    ]


def test_run_and_check_json_with_no_contracts_print_an_empty_report(
    tmp_path,
):
    config = tmp_path / ".modelgen.yml"
    config.write_text("contracts: contracts/*.yml\n")

    for command in ("run", "check"):
        result = runner.invoke(
            app, [command, "--config", str(config), "--format", "json"]
        )

        report = _report(result.stdout)
        assert result.exit_code == 0
        assert report["command"] == command
        assert report["inputs"] == []
        assert report["files"] == []
        assert report["issues"] == []
        assert "No contracts found" in result.stderr


def test_run_and_check_json_with_a_bad_config_print_an_error_issue(
    tmp_path,
):
    config = tmp_path / ".modelgen.yml"
    config.write_text("contracts: [unclosed\n")

    for command in ("run", "check"):
        result = runner.invoke(
            app, [command, "--config", str(config), "--format", "json"]
        )

        report = _report(result.stdout)
        assert result.exit_code == 1
        assert report["exit_code"] == 1
        assert [i["severity"] for i in report["issues"]] == ["error"]
        assert "Config error" in result.stderr


def test_run_json_reports_unmapped_rule_against_its_contract(tmp_path):
    (tmp_path / "produced").mkdir()
    contract = tmp_path / "produced" / "qr.yml"
    contract.write_text(QUALITY_CONTRACT)
    config = tmp_path / ".modelgen.yml"
    config.write_text(
        "contracts: events/*.yml\n"
        "schema_contracts: produced/*.yml\n"
        "schema_output: models/marts\n"
    )

    result = runner.invoke(
        app, ["run", "--config", str(config), "--format", "json"]
    )

    report = _report(result.stdout)
    assert [
        (i["code"], i["severity"], i["source"]) for i in report["issues"]
    ] == [("quality-rule-unmapped", "warning", str(contract))]


def test_gen_layer_schema_json_lists_written_files_with_hashes(tmp_path):
    contract = _contract(tmp_path, QUALITY_CONTRACT)
    out = tmp_path / "out"

    result = runner.invoke(
        app,
        [
            "gen", str(contract), "--layer", "schema", "-o", str(out),
            "--format", "json",
        ],
    )  # fmt: skip

    report = _report(result.stdout)
    assert result.exit_code == 0
    assert report["command"] == "gen"
    assert report["files"]
    for f in report["files"]:
        assert f["status"] == "written"
        assert f["sha256"] == _sha(Path(f["path"]))
