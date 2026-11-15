"""Introspector choice follows the event contract's dialect."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

import modelgen.introspect as intro
from modelgen.cli import app
from tests.fakes import FakeIntrospector
from tests.test_cli import _FAKE_COLUMNS, CONTRACT_YAML

runner = CliRunner()
DUCK = Path(__file__).parent / "fixtures" / "duckdb" / "single_table.yaml"
NOTICE = "No GCP credentials"


class _Forbidden:
    def __init__(self, *a, **k):
        raise AssertionError("BigQueryIntrospector constructed")


class _Fake(FakeIntrospector):
    built = 0

    def __init__(self):
        type(self).built += 1
        super().__init__(
            {("my-project", "landing", "user_signups_raw"): _FAKE_COLUMNS}
        )


def _creds(monkeypatch, value: bool, bq=_Forbidden):
    monkeypatch.setattr(intro, "has_default_credentials", lambda: value)
    monkeypatch.setattr(intro, "BigQueryIntrospector", bq)


def _project(tmp_path, monkeypatch, text: str) -> Path:
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "c.yml").write_text(text)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _gen(tmp_path, layer, contract: Path):
    return runner.invoke(
        app,
        [
            "gen",
            str(contract),
            layer,
            "--introspect",
            "-o",
            str(tmp_path / "out"),
        ],
    )


@pytest.mark.parametrize("layer", ["--staging", "--raw"])
def test_gen_duckdb_never_queries_bigquery(tmp_path, monkeypatch, layer):
    _creds(monkeypatch, True)
    r = _gen(tmp_path, layer, DUCK)
    assert r.exit_code == 0, r.output
    assert NOTICE not in r.output


def test_run_duckdb_never_queries_bigquery(tmp_path, monkeypatch):
    _creds(monkeypatch, True)
    _project(tmp_path, monkeypatch, DUCK.read_text())
    r = runner.invoke(app, ["run", "--introspect"])
    assert r.exit_code == 0, r.output
    assert NOTICE not in r.output


def test_check_duckdb_never_queries_bigquery(tmp_path, monkeypatch):
    _creds(monkeypatch, True)
    _project(tmp_path, monkeypatch, DUCK.read_text())
    assert (
        runner.invoke(app, ["run", "--introspect"]).exit_code == 0
    )  # files to check
    r = runner.invoke(app, ["check", "--introspect"])
    assert r.exit_code == 0, r.output
    assert NOTICE not in r.output


def test_bigquery_contract_with_credentials_still_queries(
    tmp_path, monkeypatch
):
    _Fake.built = 0
    _creds(monkeypatch, True, _Fake)
    p = tmp_path / "c.yml"
    p.write_text(CONTRACT_YAML)
    r = _gen(tmp_path, "--raw", p)
    assert r.exit_code == 0, r.output
    assert _Fake.built == 1


def test_run_builds_shared_bigquery_once_for_bq_only(tmp_path, monkeypatch):
    _Fake.built = 0
    _creds(monkeypatch, True, _Fake)
    _project(tmp_path, monkeypatch, CONTRACT_YAML)
    r = runner.invoke(app, ["run", "--introspect"])
    assert r.exit_code == 0, r.output
    assert _Fake.built == 1


@pytest.mark.parametrize("cmd", ["gen", "run", "check"])
def test_notice_only_for_bigquery_contracts(tmp_path, monkeypatch, cmd):
    _creds(monkeypatch, False)

    def go(text, name):
        d = tmp_path / name
        d.mkdir()
        (d / "contracts").mkdir()
        (d / "contracts" / "c.yml").write_text(text)
        monkeypatch.chdir(d)
        args = (
            ["gen", "contracts/c.yml", "--raw", "--introspect", "-o", "out"]
            if cmd == "gen"
            else [cmd, "--introspect"]
        )
        return runner.invoke(app, args).output

    assert NOTICE not in go(DUCK.read_text(), "duck")
    assert NOTICE in go(CONTRACT_YAML, "bq")


def _mixed_project(tmp_path, monkeypatch) -> Path:
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "a_duck.yml").write_text(DUCK.read_text())
    (tmp_path / "contracts" / "b_bq.yml").write_text(CONTRACT_YAML)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_run_mixed_project_builds_bigquery_once_for_bq_contract_only(
    tmp_path, monkeypatch
):
    _Fake.built = 0
    _creds(monkeypatch, True, _Fake)
    _mixed_project(tmp_path, monkeypatch)
    r = runner.invoke(app, ["run", "--introspect"])
    assert r.exit_code == 0, r.output
    assert _Fake.built == 1
    assert NOTICE not in r.output


def test_run_mixed_project_without_credentials_notices_once(
    tmp_path, monkeypatch
):
    _creds(monkeypatch, False)
    _mixed_project(tmp_path, monkeypatch)
    r = runner.invoke(app, ["run", "--introspect"])
    assert r.exit_code == 0, r.output
    assert r.stdout.count(NOTICE) == 1


@pytest.mark.parametrize("cmd", ["gen", "run"])
def test_from_contract_never_builds_bigquery_introspector(
    tmp_path, monkeypatch, cmd
):
    _creds(monkeypatch, True)  # constructing BigQueryIntrospector raises
    if cmd == "gen":
        p = tmp_path / "c.yml"
        p.write_text(CONTRACT_YAML)
        args = [
            "gen",
            str(p),
            "--raw",
            "-o",
            str(tmp_path / "out"),
            "--from-contract",
        ]
    else:
        _project(tmp_path, monkeypatch, CONTRACT_YAML)
        args = ["run", "--from-contract"]
    r = runner.invoke(app, args)
    assert r.exit_code == 0, r.output
    assert NOTICE not in r.output
