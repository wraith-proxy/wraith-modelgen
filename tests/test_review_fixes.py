"""JSON-mode guarantees: stdout is one parseable document, even when logs
are emitted or the run fails unexpectedly, and each issue names its source.
"""

import json
import logging
from pathlib import Path

import pytest
from test_quality_rules import _write as write_unmapped
from typer.testing import CliRunner

from modelgen import project
from modelgen.cli import app

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "contracts" / "example_contract.yml"

runner = CliRunner()


def _config(tmp_path: Path, extra: str = "") -> Path:
    (tmp_path / "contracts").mkdir(exist_ok=True)
    (tmp_path / "contracts" / "example.yml").write_text(EXAMPLE.read_text())
    config = tmp_path / ".modelgen.yml"
    config.write_text(
        "contracts: contracts/*.yml\n"
        "raw_output: models/raw\n"
        "staging_output: models/staging\n" + extra
    )
    return config


@pytest.mark.parametrize("command", ["run", "check"])
def test_project_commands_keep_log_records_off_stdout(
    tmp_path, monkeypatch, command
):
    config = _config(tmp_path)
    real = getattr(project, f"{command}_project")

    def logging_project(*args, **kwargs):
        logging.getLogger("modelgen").info("introspecting something")
        return real(*args, **kwargs)

    monkeypatch.setattr(f"modelgen.cli.{command}_project", logging_project)
    result = runner.invoke(
        app, [command, "--config", str(config), "--format", "json"]
    )

    json.loads(result.stdout)
    assert "introspecting something" in result.stderr


def _non_utf8(path: Path) -> Path:
    path.write_bytes(b"\xff\xfe\x00bad")
    return path


def test_validate_json_reports_a_non_utf8_contract_as_invalid(tmp_path):
    bad = _non_utf8(tmp_path / "b.yml")
    result = runner.invoke(app, ["validate", str(bad), "--format", "json"])

    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["exit_code"] == 1
    assert report["issues"][0]["source"] == str(bad)


@pytest.mark.parametrize("command", ["run", "check"])
def test_project_json_reports_a_non_utf8_contract_as_invalid(
    tmp_path, command
):
    config = _config(tmp_path)
    bad = _non_utf8(tmp_path / "contracts" / "bad.yml")
    result = runner.invoke(
        app, [command, "--config", str(config), "--format", "json"]
    )

    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert [i["source"] for i in report["issues"]] == [str(bad)]


def test_json_mode_prints_a_report_for_an_internal_error(
    tmp_path, monkeypatch
):
    contract = tmp_path / "contract.yml"
    contract.write_text(EXAMPLE.read_text())
    real_read_text = Path.read_text

    def failing_read_text(self, *args, **kwargs):
        if self == contract:
            raise RuntimeError("disk on fire")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read_text)
    result = runner.invoke(
        app, ["validate", str(contract), "--format", "json"]
    )

    assert result.exit_code == 6
    report = json.loads(result.stdout)
    assert report["command"] == "validate"
    assert report["exit_code"] == 6
    assert report["ok"] is False
    assert "disk on fire" in report["issues"][0]["message"]
    assert "internal error" in result.stderr


def test_unmapped_rule_issues_name_their_own_contract_and_object(tmp_path):
    sc = tmp_path / "sc"
    sc.mkdir()
    rules = [{"id": "note", "type": "text", "description": "prose"}]
    for name in ("a", "b"):
        made = write_unmapped(tmp_path, obj_quality=rules)
        made.rename(sc / f"{name}.yml")
    config = _config(
        tmp_path, "schema_contracts: sc/*.yml\nschema_output: models/m\n"
    )

    result = runner.invoke(
        app, ["run", "--config", str(config), "--format", "json"]
    )

    issues = [
        i
        for i in json.loads(result.stdout)["issues"]
        if i["code"] == "quality-rule-unmapped"
    ]
    assert [i["source"] for i in issues] == [
        str(sc / "a.yml"),
        str(sc / "b.yml"),
    ]
    assert {i["object"] for i in issues} == {"m.note"}


def test_check_json_reports_a_deleted_file_as_missing(tmp_path):
    config = _config(tmp_path)
    runner.invoke(app, ["run", "--config", str(config)])
    gone = next((tmp_path / "models" / "raw").glob("*.sql"))
    expected = gone.read_text()
    gone.unlink()

    result = runner.invoke(
        app, ["check", "--config", str(config), "--format", "json"]
    )

    assert result.exit_code == 4
    [entry] = [
        f for f in json.loads(result.stdout)["files"] if f["path"] == str(gone)
    ]
    assert entry["status"] == "missing"
    assert entry["on_disk_sha256"] is None
    gone.write_text(expected)
    import hashlib

    assert entry["sha256"] == hashlib.sha256(expected.encode()).hexdigest()
