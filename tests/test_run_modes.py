"""`run` renders produced and extract contracts; run then check is clean."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
REAL = HERE / "fixtures" / "real_contracts"
EXAMPLE = HERE.parent / "contracts" / "example_contract.yml"
_RUNNER = "from modelgen.cli import main; main()"
SOURCES_YML = "_wraith_bus_extract_positions__sources.yml"
ROW_COUNT = "bus_positions__extract_row_count.sql"


def _cli(*args: str, cwd: Path, check: bool = True):
    env = {**os.environ, "PYTHONHASHSEED": "0", "LC_ALL": "C"}
    return subprocess.run(
        [sys.executable, "-c", _RUNNER, *args],
        cwd=cwd,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def _project(tmp_path: Path, contract: str, config: str) -> Path:
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "c.odcs.yaml").write_text(
        (REAL / f"{contract}.odcs.yaml").read_text()
    )
    (tmp_path / ".modelgen.yml").write_text(config)
    return tmp_path


def test_run_writes_extract_yml_and_check_is_clean(tmp_path):
    proj = _project(
        tmp_path,
        "bus-positions-extract",
        "sources_contracts: contracts/*.odcs.yaml\n"
        "sources_output: models/staging\n",
    )
    assert _cli("check", cwd=proj, check=False).returncode == 4
    _cli("run", cwd=proj)
    assert (proj / "models" / "staging" / SOURCES_YML).is_file()
    assert "in sync" in _cli("check", cwd=proj).stdout.lower()


def test_run_writes_produced_yml_and_check_is_clean(tmp_path):
    proj = _project(
        tmp_path,
        "bus-marts",
        "schema_contracts: contracts/*.odcs.yaml\n"
        "schema_output: models/marts\n",
    )
    _cli("run", cwd=proj)
    assert list((proj / "models" / "marts").glob("*.yml"))
    assert "in sync" in _cli("check", cwd=proj).stdout.lower()


def test_run_honours_tests_output(tmp_path):
    proj = _project(
        tmp_path,
        "bus-positions-extract",
        "sources_contracts: contracts/*.odcs.yaml\n"
        "sources_output: models/staging\n"
        "tests_output: tests\n",
    )
    _cli("run", cwd=proj)
    assert (proj / "tests" / "generated" / ROW_COUNT).is_file()
    assert not (proj / "models" / "staging" / "tests").exists()
    assert "in sync" in _cli("check", cwd=proj).stdout.lower()


def test_run_dry_run_writes_nothing(tmp_path):
    proj = _project(
        tmp_path,
        "bus-positions-extract",
        "sources_contracts: contracts/*.odcs.yaml\n"
        "sources_output: models/staging\n",
    )
    out = _cli("run", "--dry-run", cwd=proj)
    assert SOURCES_YML in out.stdout
    assert not (proj / "models").exists()


def test_run_without_output_key_writes_no_yml(tmp_path):
    proj = _project(
        tmp_path,
        "bus-positions-extract",
        "sources_contracts: contracts/*.odcs.yaml\n",
    )
    _cli("run", cwd=proj)
    assert not (proj / "models").exists()


def test_run_fails_when_a_produced_pattern_matches_nothing(tmp_path):
    proj = _project(
        tmp_path,
        "bus-marts",
        "schema_contracts: nothing/*.odcs.yaml\nschema_output: models/marts\n",
    )
    res = _cli("run", cwd=proj, check=False)
    assert res.returncode == 1
    assert "schema_contracts" in res.stderr


def test_run_exits_1_naming_a_produced_contract_that_fails_to_parse(
    tmp_path,
):
    proj = _project(
        tmp_path,
        "bus-marts",
        "schema_contracts: contracts/*.odcs.yaml\n"
        "schema_output: models/marts\n",
    )
    (proj / "contracts" / "broken.odcs.yaml").write_text("schema: [\n")
    res = _cli("run", cwd=proj, check=False)
    assert res.returncode == 1
    assert "broken.odcs.yaml" in res.stderr
    assert list((proj / "models" / "marts").glob("*.yml"))


def test_run_warns_on_stderr_about_unmapped_extract_rule_and_exits_0(
    tmp_path,
):
    proj = _project(
        tmp_path,
        "bus-positions-extract",
        "sources_contracts: contracts/*.odcs.yaml\n"
        "sources_output: models/staging\n",
    )
    _add_prose_rule(proj / "contracts" / "c.odcs.yaml")
    res = _cli("run", cwd=proj)
    assert "1 quality rule(s) not mapped: bus_positions.prose" in res.stderr
    assert (proj / "models" / "staging" / SOURCES_YML).is_file()


def test_run_warns_on_stderr_about_unmapped_produced_rule_and_exits_0(
    tmp_path,
):
    proj = _project(
        tmp_path,
        "bus-marts",
        "schema_contracts: contracts/*.odcs.yaml\n"
        "schema_output: models/marts\n",
    )
    _add_prose_rule(proj / "contracts" / "c.odcs.yaml")
    res = _cli("run", cwd=proj)
    # the fixture already carries one unmapped rule of its own
    assert res.stderr == (
        "modelgen: 2 quality rule(s) not mapped: eta_models.prose,"
        " line_punctuality.punctuality_may_be_empty\n"
    )
    assert list((proj / "models" / "marts").glob("*.yml"))


def _add_prose_rule(path: Path) -> None:
    path.write_text(
        path.read_text().replace(
            "    quality:\n",
            "    quality:\n      - id: prose\n        type: text\n"
            "        description: eyeball it\n",
            1,
        )
    )


def test_unknown_config_key_is_named_with_nearest_key(tmp_path):
    (tmp_path / ".modelgen.yml").write_text("staging_ouput: models/stg\n")
    result = _cli("run", cwd=tmp_path)
    assert "staging_ouput" in result.stderr
    assert "staging_output" in result.stderr


def test_contracts_key_without_its_output_key_is_reported(tmp_path):
    proj = _project(
        tmp_path, "bus-marts", "schema_contracts: contracts/*.odcs.yaml\n"
    )
    result = _cli("check", cwd=proj)
    assert "schema_contracts" in result.stderr
    assert "schema_output" in result.stderr


def test_config_warning_is_in_the_json_report(tmp_path):
    (tmp_path / ".modelgen.yml").write_text("contract: contracts/*.yml\n")
    report = json.loads(_cli("run", "--format", "json", cwd=tmp_path).stdout)
    assert report["ok"] is True
    assert [i["severity"] for i in report["issues"]] == ["warning"]
    assert "'contracts'" in report["issues"][0]["message"]


def test_every_documented_key_is_accepted_without_warning(tmp_path):
    proj = _project(
        tmp_path,
        "bus-marts",
        "contracts: contracts/*.yml\n"
        "raw_output: models/raw\n"
        "staging_output: models/staging\n"
        "schema_contracts: contracts/*.odcs.yaml\n"
        "schema_output: models/marts\n"
        "sources_contracts: contracts/*.odcs.yaml\n"
        "sources_output: models/staging\n"
        "tests_output: tests\n",
    )
    result = _cli("check", cwd=proj, check=False)
    assert result.returncode == 4
    assert "unknown key" not in result.stderr
    assert "is set without" not in result.stderr


def test_output_key_without_its_glob_warns_once_on_run(tmp_path):
    proj = _project(
        tmp_path,
        "bus-marts",
        "schema_contracts: contracts/*.odcs.yaml\n"
        "schema_output: models/marts\n"
        "sources_output: models/staging\n",
    )
    stderr = _cli("run", cwd=proj).stderr
    assert stderr.count("'sources_output' is set without") == 1
    assert list((proj / "models" / "marts").glob("*.yml"))


def test_config_warning_survives_a_pattern_that_matches_nothing(tmp_path):
    (tmp_path / ".modelgen.yml").write_text(
        "schema_contracts: missing/*.yml\n"
        "schema_output: models/marts\n"
        "raw_ouput: models/raw\n"
    )
    result = _cli("check", "--format", "json", cwd=tmp_path, check=False)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert sorted(i["severity"] for i in report["issues"]) == [
        "error",
        "warning",
    ]


def test_non_string_config_value_exits_1_naming_key_and_type(tmp_path):
    (tmp_path / ".modelgen.yml").write_text(
        "contracts: false\ncontract: typo\n"
    )
    for command in ("run", "check"):
        text = _cli(command, cwd=tmp_path, check=False)
        assert text.returncode == 1
        assert "'contracts' must be a string, got bool" in text.stderr
        assert "unknown key" not in text.stderr
        res = _cli(command, "--format", "json", cwd=tmp_path, check=False)
        assert res.returncode == 1
        [issue] = json.loads(res.stdout)["issues"]
        assert issue["severity"] == "error"
        assert "'contracts' must be a string, got bool" in issue["message"]


@pytest.mark.parametrize(
    "key",
    [
        "contracts",
        "raw_output",
        "staging_output",
        "schema_contracts",
        "schema_output",
        "sources_contracts",
        "sources_output",
        "tests_output",
    ],
)
@pytest.mark.parametrize(
    ("value", "type_name"),
    [("true", "bool"), ("5", "int"), ("[a, b]", "list"), ("{a: b}", "dict")],
)
def test_every_known_key_rejects_non_string_values(
    tmp_path, key, value, type_name
):
    (tmp_path / ".modelgen.yml").write_text(f"{key}: {value}\n")
    res = _cli("check", cwd=tmp_path, check=False)
    assert res.returncode == 1
    assert f"'{key}' must be a string, got {type_name}" in res.stderr


def test_null_config_values_count_as_absent(tmp_path):
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "c.yml").write_text(EXAMPLE.read_text())
    (tmp_path / ".modelgen.yml").write_text(
        "contracts:\n"
        "raw_output: null\n"
        "staging_output: ~\n"
        "schema_contracts:\n"
        "schema_output:\n"
        "sources_contracts:\n"
        "sources_output:\n"
        "tests_output:\n"
    )
    res = _cli("run", cwd=tmp_path)
    assert res.stderr == ""
    assert list((tmp_path / "models" / "raw").glob("raw__*.sql"))
    assert list((tmp_path / "models" / "staging").glob("stg__*.sql"))
