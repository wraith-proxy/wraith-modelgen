"""Legacy ODCS apiVersions are accepted but deprecated."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from modelgen.cli import app
from tests.test_cli import CONTRACT_YAML

runner = CliRunner()

DEPRECATION = (
    "apiVersion v3.0.1 is deprecated and will stop being accepted in a"
    " later minor release. Supported: v3.0.2, v3.1.0, v3.2.0."
)


@pytest.fixture
def legacy(tmp_path):
    p = tmp_path / "legacy.yml"
    p.write_text(
        CONTRACT_YAML.replace("apiVersion: v3.0.2", "apiVersion: v3.0.1")
    )
    return p


@pytest.fixture
def current(tmp_path):
    p = tmp_path / "current.yml"
    p.write_text(CONTRACT_YAML)
    return p


def test_validate_warns_on_stderr_for_legacy_version(legacy):
    result = runner.invoke(app, ["validate", str(legacy)])
    assert result.exit_code == 0
    assert f"Warning: {DEPRECATION}" in result.stderr
    assert "deprecated" not in result.stdout


def test_validate_json_has_deprecation_issue(legacy):
    result = runner.invoke(app, ["validate", str(legacy), "--format", "json"])
    assert result.exit_code == 0
    issues = json.loads(result.stdout)["issues"]
    [issue] = [i for i in issues if i["code"] == "odcs-version-deprecated"]
    assert issue["severity"] == "warning"
    assert issue["pointer"] == "/apiVersion"
    assert issue["message"] == DEPRECATION


def test_validate_current_version_has_no_deprecation(current):
    result = runner.invoke(app, ["validate", str(current)])
    assert result.exit_code == 0
    assert "deprecated" not in result.output


def test_gen_warns_for_legacy_version(legacy, tmp_path):
    out = tmp_path / "out"
    result = runner.invoke(
        app, ["gen", str(legacy), "--raw", "-o", str(out), "--dry-run"]
    )
    assert result.exit_code == 0
    assert f"Warning: {DEPRECATION}" in result.stderr
    assert "deprecated" not in result.stdout


def test_gen_json_has_deprecation_issue(legacy, tmp_path):
    result = runner.invoke(
        app,
        ["gen", str(legacy), "--raw", "--dry-run", "--format", "json"],
    )
    assert result.exit_code == 0
    codes = [i["code"] for i in json.loads(result.stdout)["issues"]]
    assert codes == ["odcs-version-deprecated"]


@pytest.mark.parametrize(
    "args",
    [
        ["schema"],
        ["sources"],
        ["gen", "--layer", "schema"],
        ["gen", "--layer", "sources"],
    ],
)
def test_produced_commands_warn_for_legacy_version(args, legacy):
    cmd = [args[0], str(legacy), *args[1:], "--dry-run"]
    result = runner.invoke(app, cmd)
    assert result.exit_code == 0
    assert f"Warning: {DEPRECATION}" in result.stderr
    assert "deprecated" not in result.stdout

    as_json = runner.invoke(app, [*cmd, "--format", "json"])
    assert as_json.exit_code == 0
    [issue] = [
        i
        for i in json.loads(as_json.stdout)["issues"]
        if i["code"] == "odcs-version-deprecated"
    ]
    assert issue["severity"] == "warning"
    assert issue["pointer"] == "/apiVersion"


@pytest.fixture
def project(legacy, tmp_path):
    root = tmp_path / "proj"
    (root / "contracts").mkdir(parents=True)
    (root / "produced").mkdir()
    text = legacy.read_text()
    (root / "contracts" / "events.yml").write_text(text)
    (root / "produced" / "marts.yml").write_text(text)
    config = root / ".modelgen.yml"
    config.write_text(
        "contracts: contracts/*.yml\n"
        "schema_contracts: produced/*.yml\n"
        "raw_output: models/raw\n"
        "staging_output: models/staging\n"
        "schema_output: models/marts\n"
    )
    return config


@pytest.mark.parametrize("command", ["run", "check"])
def test_project_commands_warn_in_text_mode(command, project):
    runner.invoke(app, ["run", "--config", str(project)])
    result = runner.invoke(app, [command, "--config", str(project)])
    assert result.exit_code == 0
    assert f"Warning: {DEPRECATION}" in result.stderr
    assert "deprecated" not in result.stdout


@pytest.mark.parametrize("command", ["run", "check"])
def test_project_commands_report_notices_in_json(command, project):
    runner.invoke(app, ["run", "--config", str(project)])
    result = runner.invoke(
        app, [command, "--config", str(project), "--format", "json"]
    )
    assert result.exit_code == 0
    report = json.loads(result.stdout)
    notices = [
        i for i in report["issues"] if i["code"] == "odcs-version-deprecated"
    ]
    assert sorted(Path(i["source"]).name for i in notices) == [
        "events.yml",
        "marts.yml",
    ]
    assert {i["severity"] for i in notices} == {"warning"}
    assert {i["pointer"] for i in notices} == {"/apiVersion"}
