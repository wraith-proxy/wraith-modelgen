"""Sources mode and the drift gate for generated sources."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from modelgen.generator import mask_version

HERE = Path(__file__).parent
CONTRACT = (
    HERE / "fixtures" / "real_contracts" / "bus-positions-extract.odcs.yaml"
)
GOLDEN = HERE / "golden" / "sources" / "bus-positions-extract"
SOURCES_YML = "_wraith_bus_extract_positions__sources.yml"
_RUNNER = "from modelgen.cli import main; main()"


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


def render_sources(*args, **kw):
    from modelgen.sources_mode import render_sources as impl

    return impl(*args, **kw)


def _doc() -> dict:
    return yaml.safe_load(render_sources(CONTRACT)[SOURCES_YML])


def _table() -> dict:
    return _doc()["sources"][0]["tables"][0]


def test_filename_and_source_name_default_to_contract_name():
    assert list(render_sources(CONTRACT))[0] == SOURCES_YML
    assert _doc()["sources"][0]["name"] == "wraith_bus_extract_positions"


def test_db_source_name_custom_property_wins(tmp_path):
    c = tmp_path / "c.yaml"
    c.write_text(
        CONTRACT.read_text().replace(
            "customProperties:\n",
            "customProperties:\n  - property: dbtSourceName\n"
            "    value: bods\n",
            1,
        )
    )
    doc = yaml.safe_load(render_sources(c)[SOURCES_YML])
    assert doc["sources"][0]["name"] == "bods"
    sql = render_sources(c)[
        "tests/generated/bus_positions__extract_row_count.sql"
    ]
    assert "{{ source('bods', 'bus_positions') }}" in sql


def test_external_location_in_meta():
    assert _table()["meta"] == {
        "external_location": (
            "read_parquet('data/bus_positions_*.parquet', union_by_name=true)"
        )
    }


def test_required_properties_get_not_null():
    cols = {c["name"]: c for c in _table()["columns"]}
    assert "not_null" in cols["timestamp"]["data_tests"]
    assert "not_null" in cols["bus_id"]["data_tests"]
    assert "not_null" not in cols["operator"].get("data_tests", [])


def _two_table_contract(tmp_path, *, obj_loc=False, doc_loc=True):
    doc = yaml.safe_load(CONTRACT.read_text().split("\n---\n", 1)[1])
    doc["customProperties"] = [
        cp
        for cp in doc.get("customProperties", [])
        if cp["property"] != "externalLocation"
    ]
    if doc_loc:
        doc["customProperties"].append(
            {"property": "externalLocation", "value": "read_parquet('x')"}
        )
    first = doc["schema"][0]
    second = {**first, "name": "other_positions"}
    for o in (first, second):
        o["quality"] = []
    if obj_loc:
        first["customProperties"] = [
            {"property": "externalLocation", "value": "read_parquet('a')"}
        ]
        second["customProperties"] = [
            {"property": "externalLocation", "value": "read_parquet('b')"}
        ]
    doc["schema"] = [first, second]
    c = tmp_path / "multi.yaml"
    c.write_text(yaml.safe_dump(doc, sort_keys=False))
    return c


def test_primary_key_gets_not_null_and_unique(tmp_path):
    c = tmp_path / "pk.yaml"
    c.write_text(
        CONTRACT.read_text().replace(
            "        name: operator\n",
            "        name: operator\n        primaryKey: true\n",
            1,
        )
    )
    cols = {
        col["name"]: col
        for col in yaml.safe_load(render_sources(c)[SOURCES_YML])["sources"][
            0
        ]["tables"][0]["columns"]
    }
    assert cols["operator"]["data_tests"] == ["not_null", "unique"]


def test_object_level_external_location_wins(tmp_path):
    c = _two_table_contract(tmp_path, obj_loc=True)
    tables = yaml.safe_load(render_sources(c)[SOURCES_YML])["sources"][0][
        "tables"
    ]
    assert [t["meta"]["external_location"] for t in tables] == [
        "read_parquet('a')",
        "read_parquet('b')",
    ]


def test_contract_level_location_with_many_objects_is_an_error(tmp_path):
    from modelgen.contract import ContractError

    c = _two_table_contract(tmp_path)
    with pytest.raises(ContractError, match="externalLocation"):
        render_sources(c)


def test_contract_level_location_with_one_object_still_works():
    assert _table()["meta"]["external_location"].startswith("read_parquet(")


def test_freshness_and_loaded_at_field():
    src = _doc()["sources"][0]
    assert src["loaded_at_field"] == '"timestamp"'
    assert src["freshness"] == {
        "warn_after": {"count": 2, "period": "day"},
        "error_after": {"count": 5, "period": "day"},
    }


def test_bad_freshness_is_a_contract_error(tmp_path):
    from modelgen.contract import ContractError

    c = tmp_path / "c.yaml"
    c.write_text(CONTRACT.read_text().replace("value: 2 days", "value: soon"))
    with pytest.raises(ContractError, match="freshnessWarnAfter"):
        render_sources(c)


def test_columns_match_set_covers_every_column_in_order():
    props = yaml.safe_load(CONTRACT.read_text().split("\n---\n", 1)[1])[
        "schema"
    ][0]["properties"]
    (test, *_) = _table()["data_tests"]
    assert test == {
        "dbt_expectations.expect_table_columns_to_match_set": {
            "column_list": [p["name"] for p in props]
        }
    }
    assert [c["name"] for c in _table()["columns"]] == [
        p["name"] for p in props
    ]


def test_quality_rules_use_source_refs():
    files = render_sources(CONTRACT)
    singular = {f: t for f, t in files.items() if f.endswith(".sql")}
    assert len(singular) == 4
    for text in singular.values():
        assert (
            "{{ source('wraith_bus_extract_positions', 'bus_positions')"
            in text
        )
        assert "ref(" not in text


def test_unmapped_rules_are_reported(tmp_path):
    c = tmp_path / "c.yaml"
    c.write_text(
        CONTRACT.read_text().replace(
            "    quality:\n",
            "    quality:\n      - id: prose\n        type: text\n"
            "        description: eyeball it\n",
            1,
        )
    )
    unmapped: list[str] = []
    render_sources(c, unmapped=unmapped)
    assert unmapped == ["bus_positions.prose"]


def test_unknown_table_rejected():
    from modelgen.contract import ContractError

    with pytest.raises(ContractError, match="Unknown table"):
        render_sources(CONTRACT, tables=["nope"])


def test_golden_matches(tmp_path):
    _cli("sources", str(CONTRACT), "-o", str(tmp_path), cwd=tmp_path)
    assert sorted(
        p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()
    ) == sorted(
        p.relative_to(GOLDEN) for p in GOLDEN.rglob("*") if p.is_file()
    )
    for p in GOLDEN.rglob("*"):
        if p.is_file():
            rel = p.relative_to(GOLDEN)
            assert mask_version((tmp_path / rel).read_text()) == (
                mask_version(p.read_text())
            )


def test_output_deterministic_and_free_of_paths(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for out, seed in ((a, "0"), (b, "1")):
        _cli(
            "sources", str(CONTRACT), "-o", str(out),
            cwd=tmp_path, hashseed=seed,
        )  # fmt: skip
    diff = subprocess.run(
        ["diff", "-r", str(a), str(b)], capture_output=True, text=True
    )
    assert diff.returncode == 0, diff.stdout
    first = (a / SOURCES_YML).read_text().splitlines()[0]
    assert first.startswith("# GENERATED by modelgen ")
    assert "/" not in first


def test_gen_layer_sources_matches_sources_command(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _cli("sources", str(CONTRACT), "-o", str(a), cwd=tmp_path)
    _cli(
        "gen", str(CONTRACT), "--layer", "sources", "-o", str(b), cwd=tmp_path
    )
    assert [p.read_text() for p in sorted(a.rglob("*")) if p.is_file()] == [
        p.read_text() for p in sorted(b.rglob("*")) if p.is_file()
    ]


def _project(tmp_path: Path) -> Path:
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "extract.odcs.yaml").write_text(
        CONTRACT.read_text()
    )
    (tmp_path / ".modelgen.yml").write_text(
        "sources_contracts: contracts/*.odcs.yaml\n"
        "sources_output: models/staging\n"
    )
    return tmp_path


def _generate(proj: Path) -> Path:
    out = proj / "models" / "staging"
    _cli(
        "sources", str(proj / "contracts" / "extract.odcs.yaml"),
        "-o", str(out), cwd=proj,
    )  # fmt: skip
    return out


def test_check_in_sync_after_generation(tmp_path):
    proj = _project(tmp_path)
    _generate(proj)
    ok = _cli("check", cwd=proj)
    assert "in sync" in ok.stdout.lower()


def test_check_detects_hand_edit_of_sources_yml(tmp_path):
    proj = _project(tmp_path)
    out = _generate(proj)
    target = out / SOURCES_YML
    target.write_text(target.read_text() + "# hand edit\n")
    res = _cli("check", "--diff", cwd=proj, check=False)
    assert res.returncode == 4
    assert "drift" in res.stdout.lower()
    assert "hand edit" in res.stdout


def test_check_detects_hand_edit_of_singular_test(tmp_path):
    proj = _project(tmp_path)
    out = _generate(proj)
    target = (
        out / "tests" / "generated" / "bus_positions__extract_row_count.sql"
    )
    target.write_text(target.read_text().replace("500000", "1"))
    assert _cli("check", cwd=proj, check=False).returncode == 4


@pytest.mark.parametrize(
    "rel",
    [SOURCES_YML, "tests/generated/bus_positions__extract_lat_bounds.sql"],
)
def test_check_detects_missing_generated_file(tmp_path, rel):
    proj = _project(tmp_path)
    out = _generate(proj)
    (out / rel).unlink()
    res = _cli("check", cwd=proj, check=False)
    assert res.returncode == 4
    assert "missing" in res.stdout.lower()


def test_check_with_nothing_generated_is_missing(tmp_path):
    proj = _project(tmp_path)
    res = _cli("check", cwd=proj, check=False)
    assert res.returncode == 4
    assert "missing" in res.stdout.lower()


def test_contract_level_location_is_consistent_under_table_filter(tmp_path):
    from modelgen.contract import ContractError

    c = _two_table_contract(tmp_path)
    with pytest.raises(ContractError, match="externalLocation"):
        render_sources(c, tables=["other_positions"])


@pytest.mark.parametrize("prop", ["loadedAtField", "dbtSourceName"])
def test_list_valued_scalar_property_is_a_contract_error(tmp_path, prop):
    from modelgen.contract import ContractError

    doc = yaml.safe_load(CONTRACT.read_text().split("\n---\n", 1)[1])
    doc["customProperties"] = [
        cp for cp in doc["customProperties"] if cp["property"] != prop
    ] + [{"property": prop, "value": ["a", "b"]}]
    c = tmp_path / "c.yaml"
    c.write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(ContractError, match=prop):
        render_sources(c)


@pytest.mark.parametrize("val", ["0 days", "0 hours", "0 minutes", "00 days"])
def test_zero_freshness_is_a_contract_error(tmp_path, val):
    from modelgen.contract import ContractError

    c = tmp_path / "c.yaml"
    c.write_text(
        CONTRACT.read_text().replace("value: 2 days", f"value: {val}")
    )
    with pytest.raises(ContractError, match="freshnessWarnAfter"):
        render_sources(c)


def test_column_set_test_name_override(tmp_path):
    c = tmp_path / "c.yaml"
    c.write_text(
        CONTRACT.read_text().replace(
            "customProperties:\n",
            "customProperties:\n  - property: dbtColumnSetTest\n"
            "    value: columns_match_set\n",
            1,
        )
    )
    doc = yaml.safe_load(render_sources(c)[SOURCES_YML])
    (test, *_) = doc["sources"][0]["tables"][0]["data_tests"]
    default = _table()["data_tests"][0]
    (args,) = default.values()
    assert test == {"columns_match_set": args}


def test_column_set_test_name_list_is_a_contract_error(tmp_path):
    from modelgen.contract import ContractError

    c = tmp_path / "c.yaml"
    c.write_text(
        CONTRACT.read_text().replace(
            "customProperties:\n",
            "customProperties:\n  - property: dbtColumnSetTest\n"
            "    value: [a, b]\n",
            1,
        )
    )
    with pytest.raises(ContractError):
        render_sources(c)


_ROW_COUNT = "bus_positions__extract_row_count.sql"


def test_tests_output_rebases_singular_tests_on_write(tmp_path):
    out, tests = tmp_path / "models" / "staging", tmp_path / "tests"
    _cli(
        "sources", str(CONTRACT), "-o", str(out),
        "--tests-output", str(tests), cwd=tmp_path,
    )  # fmt: skip
    assert (out / SOURCES_YML).is_file()
    assert (tests / "generated" / _ROW_COUNT).is_file()
    assert not (out / "tests").exists()


def _tests_project(tmp_path: Path) -> Path:
    proj = _project(tmp_path)
    (proj / ".modelgen.yml").write_text(
        "sources_contracts: contracts/*.odcs.yaml\n"
        "sources_output: models/staging\n"
        "tests_output: tests\n"
    )
    _cli(
        "sources", str(proj / "contracts" / "extract.odcs.yaml"),
        "-o", str(proj / "models" / "staging"),
        "--tests-output", str(proj / "tests"), cwd=proj,
    )  # fmt: skip
    return proj


def test_check_clean_when_tests_output_matches(tmp_path):
    proj = _tests_project(tmp_path)
    ok = _cli("check", cwd=proj)
    assert "in sync" in ok.stdout.lower()


def test_check_flags_hand_edit_in_tests_output(tmp_path):
    proj = _tests_project(tmp_path)
    target = proj / "tests" / "generated" / _ROW_COUNT
    target.write_text(target.read_text().replace("500000", "1"))
    assert _cli("check", cwd=proj, check=False).returncode == 4


def test_check_flags_missing_file_in_tests_output(tmp_path):
    proj = _tests_project(tmp_path)
    (proj / "tests" / "generated" / _ROW_COUNT).unlink()
    res = _cli("check", cwd=proj, check=False)
    assert res.returncode == 4
    assert "missing" in res.stdout.lower()


def test_schema_command_writes_singular_tests_under_tests_output(tmp_path):
    marts = HERE / "fixtures" / "real_contracts" / "bus-marts.odcs.yaml"
    out, tests = tmp_path / "models", tmp_path / "tests"
    _cli(
        "schema", str(marts), "-o", str(out),
        "--tests-output", str(tests), cwd=tmp_path,
    )  # fmt: skip
    assert list((tests / "generated").glob("*.sql"))
    assert not (out / "tests").exists()
