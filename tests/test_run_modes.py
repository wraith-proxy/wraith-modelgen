"""`run` renders produced and extract contracts; run then check is clean."""

import os
import subprocess
import sys
from pathlib import Path

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
    assert _cli("check", cwd=proj, check=False).returncode == 1
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
    assert "schema_contracts" in res.stdout


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
    assert "broken.odcs.yaml" in res.stdout
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
