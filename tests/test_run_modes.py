"""`run` renders produced and extract contracts; run then check is clean."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
REAL = HERE / "fixtures" / "real_contracts"
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


MISSING_PACKAGE = "does not install dbt_expectations"


def _dbt_project(tmp_path: Path) -> Path:
    proj = _project(
        tmp_path,
        "bus-positions-extract",
        "sources_contracts: contracts/*.odcs.yaml\n"
        "sources_output: models/staging\n",
    )
    (proj / "dbt_project.yml").write_text("name: bus\n")
    return proj


def test_missing_dbt_expectations_warns_once_and_keeps_exit_codes(tmp_path):
    proj = _dbt_project(tmp_path)
    assert _cli("run", cwd=proj).stderr.count(MISSING_PACKAGE) == 1
    result = _cli("check", "--format", "json", cwd=proj)
    assert result.stderr.count(MISSING_PACKAGE) == 1
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert [
        i["severity"]
        for i in report["issues"]
        if MISSING_PACKAGE in i["message"]
    ] == ["warning"]


def test_no_dbt_project_at_or_below_the_config_dir_stays_silent(tmp_path):
    proj = tmp_path / "repo"
    proj.mkdir()
    _dbt_project(proj)
    assert MISSING_PACKAGE in _cli("run", cwd=proj).stderr
    (proj / "dbt_project.yml").rename(tmp_path / "dbt_project.yml")
    assert MISSING_PACKAGE not in _cli("run", cwd=proj).stderr


@pytest.mark.parametrize(
    "path, content",
    [
        (
            "packages.yml",
            "packages:\n  - package: metaplane/dbt_expectations\n"
            '    version: [">=0.10.0", "<0.11.0"]\n',
        ),
        (
            "dependencies.yml",
            "packages:\n"
            "  - git: https://example.com/fork/dbt_expectations.git\n",
        ),
        (
            "dbt_packages/dbt_expectations/dbt_project.yml",
            "name: dbt_expectations\n",
        ),
    ],
    ids=["packages", "dependencies", "vendored"],
)
def test_installed_dbt_expectations_stays_silent(tmp_path, path, content):
    proj = _dbt_project(tmp_path)
    assert MISSING_PACKAGE in _cli("run", cwd=proj).stderr
    (proj / path).parent.mkdir(parents=True, exist_ok=True)
    (proj / path).write_text(content)
    assert MISSING_PACKAGE not in _cli("run", cwd=proj).stderr


def test_output_without_dbt_expectations_stays_silent(tmp_path):
    proj = _dbt_project(tmp_path)
    assert MISSING_PACKAGE in _cli("run", cwd=proj).stderr
    contract = proj / "contracts" / "c.odcs.yaml"
    contract.write_text(
        contract.read_text().replace(
            "customProperties:\n",
            "customProperties:\n  - property: dbtColumnSetTest\n"
            "    value: columns_match_set\n",
            1,
        )
    )
    assert MISSING_PACKAGE not in _cli("run", cwd=proj).stderr
