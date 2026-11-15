"""ODCS quality rules mapped to dbt tests in schema mode."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).parent
REAL = HERE / "fixtures" / "real_contracts"
GOLDEN = HERE / "golden" / "quality_rules"
_RUNNER = "from modelgen.cli import main; main()"
EXPECT_ROWS = "dbt_expectations.expect_table_row_count_to_be_between"
COMPOUND = "dbt_expectations.expect_compound_columns_to_be_unique"

HEAD = """\
apiVersion: v3.2.0
kind: DataContract
id: 11111111-2222-4333-8444-555555555555
name: qr_demo
version: 1.0.0
status: draft
servers:
  - server: local
    type: duckdb
    database: ./x.duckdb
    schema: main
"""


def _cli(*args, cwd, hashseed="0", check=True):
    env = {**os.environ, "PYTHONHASHSEED": hashseed, "LC_ALL": "C"}
    return subprocess.run(
        [sys.executable, "-c", _RUNNER, *args],
        cwd=cwd,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def _write(tmp_path, obj_quality=None, prop_quality=None, required=False):
    """One model `m` with columns a (quality: prop_quality) and b."""
    obj = {
        "name": "m",
        "physicalType": "table",
        "description": "d",
        "properties": [
            {
                "name": "a",
                "logicalType": "string",
                "physicalType": "VARCHAR",
                "required": required,
                **({"quality": prop_quality} if prop_quality else {}),
            },
            {
                "name": "b",
                "logicalType": "string",
                "physicalType": "VARCHAR",
            },
        ],
    }
    if obj_quality:
        obj["quality"] = obj_quality
    text = HEAD + yaml.safe_dump({"schema": [obj]}, sort_keys=False)
    path = tmp_path / "c.odcs.yaml"
    path.write_text(text)
    return path


def _render(path, **kw):
    from modelgen.schema_mode import render_schema

    unmapped: list[str] = []
    files = render_schema(path, unmapped=unmapped, **kw)
    return files, unmapped


def _model(files):
    ymls = [t for f, t in files.items() if f.endswith(".yml")]
    assert len(ymls) == 1
    return yaml.safe_load(ymls[0])["models"][0]


def _col(model, name):
    return next(c for c in model["columns"] if c["name"] == name)


def test_valid_values_become_accepted_values_null_tolerant(tmp_path):
    q = [
        {
            "id": "a_valid",
            "metric": "invalidValues",
            "mustBe": 0,
            "arguments": {"validValues": ["x", "y"]},
        }
    ]
    files, unmapped = _render(_write(tmp_path, prop_quality=q))
    assert unmapped == []
    tests = _col(_model(files), "a")["data_tests"]
    assert tests == [
        {
            "accepted_values": {
                "values": ["x", "y"],
                "config": {"where": "a is not null"},
            }
        }
    ]
    files, _ = _render(_write(tmp_path, prop_quality=q, required=True))
    tests = _col(_model(files), "a")["data_tests"]
    assert tests[-1] == {"accepted_values": {"values": ["x", "y"]}}


@pytest.mark.parametrize(
    "rule,expected",
    [
        ({"mustBeGreaterThan": 500}, {"min_value": 500, "strictly": True}),
        ({"mustBeGreaterOrEqualTo": 500}, {"min_value": 500}),
        ({"mustBeLessThan": 9}, {"max_value": 9, "strictly": True}),
        ({"mustBeLessOrEqualTo": 9}, {"max_value": 9}),
        ({"mustBeBetween": [5, 9]}, {"min_value": 5, "max_value": 9}),
        ({"mustBe": 7}, {"min_value": 7, "max_value": 7}),
    ],
)
def test_row_count_becomes_expect_row_count_between(tmp_path, rule, expected):
    q = [{"id": "rc", "metric": "rowCount", **rule}]
    files, unmapped = _render(_write(tmp_path, obj_quality=q))
    assert unmapped == []
    assert _model(files)["data_tests"] == [{EXPECT_ROWS: expected}]


def test_duplicate_values_single_and_compound(tmp_path):
    single = [
        {
            "id": "d1",
            "metric": "duplicateValues",
            "mustBe": 0,
            "arguments": {"properties": ["a"]},
        }
    ]
    files, unmapped = _render(_write(tmp_path, obj_quality=single))
    assert unmapped == []
    assert _col(_model(files), "a")["data_tests"] == ["unique"]
    compound = [
        {
            "id": "d2",
            "metric": "duplicateValues",
            "mustBe": 0,
            "arguments": {"properties": ["a", "b"]},
        }
    ]
    files, _ = _render(_write(tmp_path, obj_quality=compound))
    assert _model(files)["data_tests"] == [
        {COMPOUND: {"column_list": ["a", "b"]}}
    ]
    prop = [{"id": "d3", "metric": "duplicateValues", "mustBe": 0}]
    files, _ = _render(_write(tmp_path, prop_quality=prop))
    assert _col(_model(files), "a")["data_tests"] == ["unique"]


def test_null_values_become_not_null(tmp_path):
    q = [{"id": "n", "metric": "nullValues", "mustBe": 0}]
    files, unmapped = _render(_write(tmp_path, prop_quality=q))
    assert unmapped == []
    assert _col(_model(files), "a")["data_tests"] == ["not_null"]


def test_severity_warning_becomes_warn(tmp_path):
    q = [
        {"id": "n", "metric": "nullValues", "mustBe": 0, "severity": "info"},
    ]
    files, _ = _render(_write(tmp_path, prop_quality=q))
    assert _col(_model(files), "a")["data_tests"] == [
        {"not_null": {"config": {"severity": "warn"}}}
    ]
    q = [
        {
            "id": "rc",
            "metric": "rowCount",
            "mustBeGreaterThan": 1,
            "severity": "warning",
        },
        {
            "id": "s",
            "type": "sql",
            "query": "select count(*) from {object}",
            "mustBe": 0,
            "severity": "warning",
        },
    ]
    files, _ = _render(_write(tmp_path, obj_quality=q))
    assert _model(files)["data_tests"][0][EXPECT_ROWS]["config"] == {
        "severity": "warn"
    }
    sql = next(t for f, t in files.items() if f.endswith(".sql"))
    assert "config(severity='warn')" in sql


def test_sql_rule_placeholders_substituted_with_ref(tmp_path):
    q = [
        {
            "id": "Bad-Rows",
            "type": "sql",
            "query": "SELECT COUNT(*) FROM {object} WHERE {property} < 0;",
            "mustBeLessThan": 3,
        },
        {
            "type": "sql",
            "query": "select count(*) from ${table} where {column} is null",
            "mustNotBe": 1,
        },
    ]
    files, unmapped = _render(_write(tmp_path, prop_quality=q))
    assert unmapped == []
    sqls = {f: t for f, t in files.items() if f.endswith(".sql")}
    assert list(sqls) == [
        "tests/generated/m__bad_rows.sql",
        "tests/generated/m__rule2.sql",
    ]
    first, second = sqls.values()
    assert first.splitlines()[0].startswith("-- GENERATED by modelgen ")
    assert first.splitlines()[0].endswith(" from qr_demo 1.0.0. Do not edit.")
    assert "{{ ref('m') }}" in first
    assert "WHERE a < 0" in first
    assert "{object}" not in first and ";" not in first
    assert "select 1 as failure" in first
    assert "as q(v)" in first
    assert "v is null or not (v < 3)" in first
    assert "{{ ref('m') }}" in second and "a is null" in second
    assert "not (v <> 1)" in second
    assert "config(" not in first


@pytest.mark.parametrize(
    "rule,pred",
    [
        ({"mustBe": 0}, "v = 0"),
        ({"mustNotBe": 0}, "v <> 0"),
        ({"mustBeGreaterThan": 1}, "v > 1"),
        ({"mustBeGreaterOrEqualTo": 1}, "v >= 1"),
        ({"mustBeLessThan": 1}, "v < 1"),
        ({"mustBeLessOrEqualTo": 1.5}, "v <= 1.5"),
        ({"mustBeBetween": [1, 2]}, "v between 1 and 2"),
        ({"mustNotBeBetween": [1, 2]}, "v not between 1 and 2"),
    ],
)
def test_sql_operators(tmp_path, rule, pred):
    q = [{"id": "s", "type": "sql", "query": "select 1 from {object}", **rule}]
    files, unmapped = _render(_write(tmp_path, obj_quality=q))
    assert unmapped == []
    sql = files["tests/generated/m__s.sql"]
    assert f"where v is null or not ({pred})" in sql


def test_non_zero_null_and_duplicate_thresholds_use_singular_tests(tmp_path):
    q = [
        {
            "id": "n",
            "metric": "nullValues",
            "mustBeLessThan": 5,
            "unit": "percent",
        },
        {"id": "d", "metric": "duplicateValues", "mustBeLessThan": 3},
    ]
    files, unmapped = _render(_write(tmp_path, prop_quality=q))
    assert unmapped == []
    n = files["tests/generated/m__n.sql"]
    d = files["tests/generated/m__d.sql"]
    assert "100.0 *" in n and "count(a)" in n
    assert "count(distinct a)" in d


def test_unmapped_rules_reported_and_strict_fails(tmp_path):
    q = [
        {"id": "note", "type": "text", "description": "prose"},
        {"id": "cust", "type": "custom", "engine": "x", "implementation": "y"},
        {"id": "rc", "metric": "rowCount", "mustBeGreaterThan": 1},
    ]
    path = _write(tmp_path, obj_quality=q)
    files, unmapped = _render(path)
    assert unmapped == ["m.note", "m.cust"]
    out = tmp_path / "out"
    ok = _cli("schema", str(path), "-o", str(out), cwd=tmp_path)
    assert ok.returncode == 0
    assert "modelgen: 2 quality rule(s) not mapped: m.note, m.cust" in (
        ok.stderr
    )
    out2 = tmp_path / "out2"
    bad = _cli(
        "schema", str(path), "-o", str(out2), "--strict",
        cwd=tmp_path, check=False,
    )  # fmt: skip
    assert bad.returncode == 3
    assert "2 quality rule(s) not mapped" in bad.stderr


def test_rule_without_id_gets_deterministic_slug(tmp_path):
    q = [
        {"type": "sql", "query": "select 1", "mustBe": 1},
        {"type": "text", "description": "x"},
    ]
    files, unmapped = _render(_write(tmp_path, obj_quality=q))
    assert "tests/generated/m__rule1.sql" in files
    assert unmapped == ["m.rule2"]


def _count_rules(doc):
    n = 0
    for obj in doc["schema"]:
        n += len(obj.get("quality") or [])
        for p in obj["properties"]:
            n += len(p.get("quality") or [])
    return n


def test_real_bus_marts_rules_all_mapped():
    path = REAL / "bus-marts.odcs.yaml"
    doc = yaml.safe_load(path.read_text())
    total = _count_rules(doc)
    assert total == 11
    files, unmapped = _render(path)
    assert unmapped == ["line_punctuality.punctuality_may_be_empty"]
    sqls = sorted(f for f in files if f.endswith(".sql"))
    assert sqls == [
        "tests/generated/eta_models__eta_model_global_fallback_exists.sql",
        "tests/generated/eta_models__eta_model_speeds_plausible.sql",
        "tests/generated/eta_models__eta_n_obs_floor.sql",
        "tests/generated/eta_models__eta_r2_in_range.sql",
        "tests/generated/localities__localities_contains_known_place.sql",
    ]
    models = {
        m["name"]: m
        for m in yaml.safe_load(
            next(t for f, t in files.items() if f.endswith(".yml"))
        )["models"]
    }
    row_tests = [
        m["name"]
        for m in models.values()
        for t in m.get("data_tests", [])
        if EXPECT_ROWS in t
    ]
    assert row_tests == ["eta_models", "localities"]
    # duplicate/null rules coincide with PK/required tests: covered, not
    # duplicated
    for m in models.values():
        keys = [str(t) for t in m.get("data_tests", [])]
        assert len(keys) == len(set(keys))
    loc = _col(models["localities"], "locality_norm")
    assert loc["data_tests"].count("not_null") == 1
    # 5 singular + 2 row count = 7 emitted; 3 coincide with existing
    # tests; 1 unmapped.
    assert len(sqls) + len(row_tests) + 3 + len(unmapped) == total


def test_real_bus_positions_extract_rules_mapped():
    path = REAL / "bus-positions-extract.odcs.yaml"
    files, unmapped = _render(path)
    assert unmapped == []
    sqls = {f: t for f, t in files.items() if f.endswith(".sql")}
    assert sorted(sqls) == [
        "tests/generated/bus_positions__extract_lat_bounds.sql",
        "tests/generated/bus_positions__extract_lon_bounds.sql",
        "tests/generated/bus_positions__extract_row_count.sql",
        "tests/generated/bus_positions__extract_unique_key.sql",
    ]
    rc = sqls["tests/generated/bus_positions__extract_row_count.sql"]
    assert "v >= 500000" in rc
    lat = sqls["tests/generated/bus_positions__extract_lat_bounds.sql"]
    assert "config(severity='warn')" in lat and "lat < 49.8" in lat
    lon = sqls["tests/generated/bus_positions__extract_lon_bounds.sql"]
    assert "lon < -8.7" in lon
    uq = sqls["tests/generated/bus_positions__extract_unique_key.sql"]
    assert "count(DISTINCT (timestamp, bus_id, operator, line))" in uq


def test_generated_ymls_parse(tmp_path):
    for name in (
        "bus-marts", "bus-cdp", "bus-fdp", "gtfs-timetable",
        "bus-positions-extract",
    ):  # fmt: skip
        files, _ = _render(REAL / f"{name}.odcs.yaml")
        for fname, text in files.items():
            if fname.endswith(".yml"):
                assert yaml.safe_load(text)["models"]


def test_goldens_match(tmp_path):
    for name in ("bus-marts", "bus-positions-extract"):
        out = tmp_path / name
        _cli("schema", str(REAL / f"{name}.odcs.yaml"), "-o", str(out),
             cwd=tmp_path)  # fmt: skip
        golden = GOLDEN / name
        got = sorted(p.relative_to(out) for p in out.rglob("*") if p.is_file())
        want = sorted(
            p.relative_to(golden) for p in golden.rglob("*") if p.is_file()
        )
        assert got == want
        for rel in got:
            assert (out / rel).read_text() == (golden / rel).read_text()


def test_generated_tests_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for out, seed in ((a, "0"), (b, "1")):
        for name in ("bus-marts", "bus-positions-extract"):
            _cli("schema", str(REAL / f"{name}.odcs.yaml"), "-o", str(out),
                 cwd=tmp_path, hashseed=seed)  # fmt: skip
    diff = subprocess.run(
        ["diff", "-r", str(a), str(b)], capture_output=True, text=True
    )
    assert diff.returncode == 0, diff.stdout
    assert len(list((a / "tests" / "generated").iterdir())) == 9


def test_check_covers_generated_tests(tmp_path):
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "bus-marts.odcs.yaml").write_text(
        (REAL / "bus-marts.odcs.yaml").read_text()
    )
    (tmp_path / ".modelgen.yml").write_text(
        "schema_contracts: contracts/*.odcs.yaml\n"
        "schema_output: models/marts\n"
    )
    out = tmp_path / "models" / "marts"
    _cli("schema", "contracts/bus-marts.odcs.yaml", "-o", str(out),
         cwd=tmp_path)  # fmt: skip
    assert "in sync" in _cli("check", cwd=tmp_path).stdout.lower()
    target = next((out / "tests" / "generated").iterdir())
    target.write_text(target.read_text() + "-- hand edit\n")
    drift = _cli("check", cwd=tmp_path, check=False)
    assert drift.returncode == 4
    target.unlink()
    missing = _cli("check", cwd=tmp_path, check=False)
    assert missing.returncode == 4
