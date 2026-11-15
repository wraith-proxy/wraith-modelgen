"""Several schema objects per contract, explicit --server and --table."""

import filecmp
import os
import subprocess
import sys
from pathlib import Path

import pytest

from modelgen.contract import ContractError, EventContract
from modelgen.generator import Layer, generate
from modelgen.introspect import ContractIntrospector

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "multi_table" / "bus_marts.yaml"
SINGLE = HERE / "fixtures" / "duckdb" / "single_table.yaml"
GOLDEN = HERE / "golden" / "multi_table"
SINGLE_GOLDEN = HERE / "golden" / "duckdb"
_RUNNER = "from modelgen.cli import main; main()"
# Contract order, deliberately not alphabetical.
OBJECTS = ["eta_models", "localities", "line_punctuality"]


def _cli(*args: str, cwd: Path, hashseed: str = "0", check: bool = True):
    env = {**os.environ, "PYTHONHASHSEED": hashseed, "LC_ALL": "C"}
    return subprocess.run(
        [sys.executable, "-c", _RUNNER, *args],
        cwd=cwd,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def _gen_all(contract: Path, out: Path, *extra: str, hashseed="0") -> None:
    for layer in ("raw", "staging"):
        _cli(
            "gen", str(contract), f"--{layer}", "--from-contract",
            "-o", str(out), *extra,
            cwd=contract.parent, hashseed=hashseed,
        )  # fmt: skip


def _files(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir())


def test_three_objects_generate_three_raw_and_three_staging(tmp_path):
    events = EventContract.from_file_all(FIXTURE)
    assert [e.name for e in events] == OBJECTS
    for event in events:
        introspector = ContractIntrospector(event)
        for layer in (Layer.RAW, Layer.STAGING):
            generate(event, introspector, tmp_path, layer)
    expected = sorted(
        f"{prefix}__{name}.{ext}"
        for name in OBJECTS
        for prefix in ("raw", "stg")
        for ext in ("sql", "yml")
    )
    assert _files(tmp_path) == expected
    assert _files(tmp_path) == _files(GOLDEN)
    for name in expected:
        assert (tmp_path / name).read_text() == (GOLDEN / name).read_text()


def test_single_object_contract_output_unchanged(tmp_path):
    events = EventContract.from_file_all(SINGLE)
    assert len(events) == 1
    for layer in (Layer.RAW, Layer.STAGING):
        generate(events[0], ContractIntrospector(events[0]), tmp_path, layer)
    assert _files(tmp_path) == _files(SINGLE_GOLDEN)
    for name in _files(SINGLE_GOLDEN):
        assert (tmp_path / name).read_text() == (
            SINGLE_GOLDEN / name
        ).read_text()
    # The single-object entry point keeps working and agrees.
    assert EventContract.from_file(SINGLE).name == events[0].name
    out = tmp_path / "cli"
    _gen_all(SINGLE, out)
    for name in _files(SINGLE_GOLDEN):
        assert (out / name).read_text() == (SINGLE_GOLDEN / name).read_text()


def test_table_option_limits_output(tmp_path):
    _gen_all(FIXTURE, tmp_path, "--table", "localities")
    assert _files(tmp_path) == sorted(
        f"{p}__localities.{e}" for p in ("raw", "stg") for e in ("sql", "yml")
    )
    both = tmp_path / "two"
    _gen_all(FIXTURE, both, "--table", "localities", "--table", "eta_models")
    assert {n.split("__", 1)[1].rsplit(".", 1)[0] for n in _files(both)} == {
        "localities",
        "eta_models",
    }
    events = EventContract.from_file_all(
        FIXTURE, tables=["localities", "eta_models"]
    )
    # Contract order wins over the order --table was given in.
    assert [e.name for e in events] == ["eta_models", "localities"]


def test_unknown_table_errors_listing_objects(tmp_path):
    with pytest.raises(ContractError) as exc:
        EventContract.from_file_all(FIXTURE, tables=["nope"])
    msg = str(exc.value)
    assert "nope" in msg
    for name in OBJECTS:
        assert name in msg
    r = _cli(
        "gen", str(FIXTURE), "--raw", "--from-contract", "--table", "nope",
        "-o", str(tmp_path), cwd=FIXTURE.parent, check=False,
    )  # fmt: skip
    assert r.returncode == 1
    assert not list(tmp_path.iterdir())
    out = r.stdout + r.stderr
    assert "nope" in out and "line_punctuality" in out


def test_server_option_selects_named_server(tmp_path):
    default = EventContract.from_file_all(FIXTURE)[0]
    # First SUPPORTED server: the leading `custom` entry is skipped.
    assert default.source.dataset == "main"
    named = EventContract.from_file_all(FIXTURE, server="marts-replica")[0]
    assert named.source.dataset == "replica"
    assert named.source.project.endswith("replica.duckdb")
    _gen_all(FIXTURE, tmp_path, "--server", "marts-replica")
    assert "replica" in (tmp_path / "raw__eta_models.yml").read_text()
    other = tmp_path / "default"
    _gen_all(FIXTURE, other)
    assert "replica" not in (other / "raw__eta_models.yml").read_text()


def test_unknown_server_errors_listing_servers(tmp_path):
    with pytest.raises(ContractError) as exc:
        EventContract.from_file_all(FIXTURE, server="nope")
    msg = str(exc.value)
    assert "nope" in msg
    for name in ("marts-docs", "marts-prod", "marts-replica"):
        assert name in msg
    r = _cli(
        "gen", str(FIXTURE), "--raw", "--from-contract", "--server", "nope",
        "-o", str(tmp_path), cwd=FIXTURE.parent, check=False,
    )  # fmt: skip
    assert r.returncode == 1
    assert "marts-prod" in r.stdout + r.stderr
    # A listed server with no dialect is rejected explicitly, not skipped.
    with pytest.raises(ContractError) as exc2:
        EventContract.from_file_all(FIXTURE, server="marts-docs")
    assert "custom" in str(exc2.value)


def test_multi_table_output_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _gen_all(FIXTURE, a, hashseed="0")
    _gen_all(FIXTURE, b, hashseed="1")
    assert _files(a) == _files(b) == _files(GOLDEN)
    for name in _files(a):
        assert filecmp.cmp(a / name, b / name, shallow=False), name
    diff = subprocess.run(
        ["diff", "-r", str(a), str(b)], capture_output=True, text=True
    )
    assert diff.returncode == 0, diff.stdout


def _project(root: Path) -> Path:
    (root / "contracts").mkdir(parents=True)
    (root / "contracts" / "marts.yml").write_text(FIXTURE.read_text())
    return root


def test_check_covers_all_objects(tmp_path):
    root = _project(tmp_path)
    _cli("run", "--from-contract", cwd=root)
    ok = _cli("check", "--from-contract", cwd=root)
    assert "12 file(s)" in ok.stdout.replace("\n", " ")
    victim = root / "models" / "staging" / "stg__line_punctuality.sql"
    victim.write_text(victim.read_text() + "-- drift\n")
    (root / "models" / "raw" / "raw__localities.yml").unlink()
    bad = _cli("check", "--from-contract", cwd=root, check=False)
    assert bad.returncode == 4
    out = bad.stdout
    assert "stg__line_punctuality.sql" in out
    assert "raw__localities.yml" in out
    v = _cli("validate", str(root / "contracts" / "marts.yml"), cwd=root)
    for name in OBJECTS:
        assert name in v.stdout


def test_validate_reports_error_in_later_object(tmp_path):
    text = FIXTURE.read_text().replace(
        "physicalType: BIGINT\n      - name: computed_at",
        "physicalType: FLOAT64\n      - name: computed_at",
        1,
    )
    bad = tmp_path / "bad.yml"
    bad.write_text(text)
    r = _cli("validate", str(bad), cwd=tmp_path, check=False)
    assert r.returncode == 3
    assert "invalid-column-type" in r.stdout
    assert "OK" in r.stdout  # the other objects still report
