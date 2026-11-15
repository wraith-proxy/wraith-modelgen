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
