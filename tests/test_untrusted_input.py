"""Contracts and warehouse metadata are untrusted input.

A contract may come from another team, and generated files are later run
by dbt with warehouse credentials. Nothing a contract or a table's
metadata says may change the structure of a generated file, place a file
outside the output directory, or exhaust the process.
"""

import copy
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import modelgen.introspect as intro
from modelgen.cli import app
from modelgen.contract import ContractError, EventContract
from modelgen.generator import GenerationError, Layer, generate
from modelgen.introspect import (
    ContractIntrospector,
    IntrospectedColumn,
)
from modelgen.lint import lint_contract
from modelgen.schema_mode import render_schema
from modelgen.sources_mode import render_sources
from tests.fakes import FakeIntrospector

ROOT = Path(__file__).parent.parent
EXAMPLE_PATH = ROOT / "contracts" / "example_contract.yml"
EXAMPLE = yaml.safe_load(EXAMPLE_PATH.read_text())
REAL = Path(__file__).parent / "fixtures" / "real_contracts"
PRODUCED = REAL / "bus-marts.odcs.yaml"
EXTRACT = REAL / "bus-positions-extract.odcs.yaml"
SOURCE_KEY = ("my-gcp-project", "landing", "user_signups_raw")
HOSTILE = 'a "quoted" back\\slash\nsecond line: - name: injected'


def _event(mutate) -> EventContract:
    doc = copy.deepcopy(EXAMPLE)
    mutate(doc)
    return EventContract.from_text(yaml.safe_dump(doc, sort_keys=False))


def _mutated(path: Path, tmp_path: Path, mutate) -> Path:
    doc = yaml.safe_load(path.read_text())
    mutate(doc)
    out = tmp_path / path.name
    out.write_text(yaml.safe_dump(doc, sort_keys=False))
    return out


def _warehouse(description: str) -> FakeIntrospector:
    cols = [
        IntrospectedColumn("EVENT_ID", "STRING", description=description),
        IntrospectedColumn("USER_ID", "STRING"),
        IntrospectedColumn("SIGNUP_METHOD", "STRING"),
        IntrospectedColumn("RECEIVED_AT", "TIMESTAMP"),
        IntrospectedColumn("COUNTRY_CODE", "STRING"),
    ]
    return FakeIntrospector({SOURCE_KEY: cols})


def _columns(yml: Path) -> list[dict]:
    return yaml.safe_load(yml.read_text())["models"][0]["columns"]


@pytest.mark.parametrize("field", ["name", "version"])
def test_contract_name_or_version_with_line_break_is_rejected(field):
    def mutate(doc):
        doc[field] = "x\ninjected: 1"

    with pytest.raises(ContractError, match=field):
        _event(mutate)


@pytest.mark.parametrize("text", ["{{ x }}", "{% x %}", "{# x #}"])
def test_jinja_delimiters_in_contract_text_are_rejected(text):
    def mutate(doc):
        doc["schema"][0]["description"] = f"harmless {text}"

    with pytest.raises(ContractError, match="Jinja"):
        _event(mutate)


@pytest.mark.parametrize(
    "render,contract",
    [(render_schema, PRODUCED), (render_sources, EXTRACT)],
    ids=["produced", "extract"],
)
def test_object_name_with_path_separator_is_rejected(
    render, contract, tmp_path
):
    def mutate(doc):
        doc["schema"][0]["name"] = "../../escaped"

    with pytest.raises(ContractError, match="name"):
        render(_mutated(contract, tmp_path, mutate))


def test_extract_source_name_with_quote_is_rejected(tmp_path):
    def mutate(doc):
        props = [
            p
            for p in doc.get("customProperties") or []
            if p.get("property") != "dbtSourceName"
        ]
        props.append({"property": "dbtSourceName", "value": "it's"})
        doc["customProperties"] = props

    with pytest.raises(ContractError, match="dbtSourceName"):
        render_sources(_mutated(EXTRACT, tmp_path, mutate))


def test_contract_description_cannot_add_columns_to_staging_yml(tmp_path):
    def mutate(doc):
        doc["schema"][0]["properties"][0]["description"] = HOSTILE

    clean, hostile = _event(lambda doc: None), _event(mutate)
    for event, out in ((clean, tmp_path / "a"), (hostile, tmp_path / "b")):
        generate(event, ContractIntrospector(event), out, Layer.STAGING)
    yml = "stg__user_signed_up.yml"
    expected = [c["name"] for c in _columns(tmp_path / "a" / yml)]
    got = _columns(tmp_path / "b" / yml)

    assert [c["name"] for c in got] == expected
    assert got[0]["description"] == HOSTILE


def test_warehouse_description_cannot_add_columns_to_raw_yml(tmp_path):
    event = _event(lambda doc: None)

    generate(event, _warehouse(HOSTILE), tmp_path, Layer.RAW)

    got = _columns(tmp_path / "raw__user_signed_up.yml")
    assert len(got) == 5
    assert got[0]["description"] == HOSTILE


def test_warehouse_description_with_jinja_fails_generation(tmp_path):
    event = _event(lambda doc: None)

    with pytest.raises(GenerationError, match="Jinja"):
        generate(event, _warehouse("{{ env_var('X') }}"), tmp_path, Layer.RAW)


def test_alias_expansion_is_rejected_before_validation():
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 7):
        refs = ", ".join([f"*a{i - 1}"] * 9)
        lines.append(f"a{i}: &a{i} [{refs}]")

    with pytest.raises(ContractError, match="nodes"):
        EventContract.from_text("\n".join(lines))


def test_deeply_nested_contract_is_a_contract_error():
    text = "a: " + "[" * 5000 + "]" * 5000

    with pytest.raises(ContractError, match="nested"):
        EventContract.from_text(text)


def test_oversized_contract_is_rejected_unparsed():
    with pytest.raises(ContractError, match="bytes"):
        EventContract.from_text("x: " + "a" * 5_000_000)


def test_generate_refuses_to_write_through_a_symlink(tmp_path):
    event = _event(lambda doc: None)
    out, victim = tmp_path / "out", tmp_path / "victim.txt"
    out.mkdir()
    victim.write_text("keep")
    (out / "raw__user_signed_up.sql").symlink_to(victim)

    with pytest.raises(GenerationError, match="symlink"):
        generate(event, _warehouse(""), out, Layer.RAW)

    assert victim.read_text() == "keep"


def test_schema_command_refuses_to_write_through_a_symlink(tmp_path):
    out, victim = tmp_path / "out", tmp_path / "victim.txt"
    out.mkdir()
    victim.write_text("keep")
    yml = next(f for f in render_schema(PRODUCED) if f.endswith("models.yml"))
    (out / yml).symlink_to(victim)

    result = CliRunner().invoke(app, ["schema", str(PRODUCED), "-o", str(out)])

    assert result.exit_code == 1
    assert victim.read_text() == "keep"


class _Forbidden:
    def __init__(self, *a, **k):
        raise AssertionError("BigQuery queried without --introspect")


def test_gen_does_not_query_bigquery_unless_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(intro, "has_default_credentials", lambda: True)
    monkeypatch.setattr(intro, "BigQueryIntrospector", _Forbidden)

    result = CliRunner().invoke(
        app, ["gen", str(EXAMPLE_PATH), "--raw", "-o", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output


def test_gen_queries_bigquery_with_introspect(tmp_path, monkeypatch):
    monkeypatch.setattr(intro, "has_default_credentials", lambda: True)
    monkeypatch.setattr(
        intro, "BigQueryIntrospector", lambda: _warehouse("from warehouse")
    )

    result = CliRunner().invoke(
        app,
        [
            "gen",
            str(EXAMPLE_PATH),
            "--raw",
            "--introspect",
            "-o",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    got = _columns(tmp_path / "raw__user_signed_up.yml")
    assert got[0]["description"] == "from warehouse"


def test_identifier_with_trailing_line_break_fails_lint():
    def mutate(doc):
        doc["servers"][0]["dataset"] = "landing\n"

    codes = [f.code for f in lint_contract(_event(mutate))]

    assert "invalid-source-dataset" in codes
