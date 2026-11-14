"""Schema-only mode: models yml for tables a project produces."""

import filecmp
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).parent
REAL = HERE / "fixtures" / "real_contracts"
GOLDEN = HERE / "golden" / "schema_mode"
_RUNNER = "from modelgen.cli import main; main()"
NAMES = ["bus-marts", "bus-cdp", "bus-fdp", "gtfs-timetable"]
COMPOUND = "dbt_expectations.expect_compound_columns_to_be_unique"


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


def render_schema(*args, **kw):
    from modelgen.schema_mode import render_schema as impl

    return impl(*args, **kw)


def _contract(name: str) -> Path:
    return REAL / f"{name}.odcs.yaml"


def _rendered(name: str, **kw) -> tuple[str, dict]:
    files = render_schema(_contract(name), **kw)
    assert len(files) == 1
    ((fname, text),) = files.items()
    return fname, yaml.safe_load(text)


def _models(name: str, **kw) -> dict:
    return {m["name"]: m for m in _rendered(name, **kw)[1]["models"]}


def test_schema_mode_consumes_real_bus_marts():
    fname, doc = _rendered("bus-marts")
    assert fname == "_wraith_bus_ingestor_marts__models.yml"
    assert [m["name"] for m in doc["models"]] == [
        "eta_models",
        "localities",
        "line_punctuality",
    ]


def test_schema_mode_consumes_real_cdp_and_fdp():
    for name in ("bus-cdp", "bus-fdp"):
        _, doc = _rendered(name)
        assert doc["models"]


@pytest.mark.parametrize("name", NAMES)
def test_model_and_column_counts_match_contract(name):
    contract = yaml.safe_load(_contract(name).read_text())
    _, doc = _rendered(name)
    objs = contract["schema"]
    assert [m["name"] for m in doc["models"]] == [o["name"] for o in objs]
    for model, obj in zip(doc["models"], objs):
        assert [c["name"] for c in model["columns"]] == [
            p["name"] for p in obj["properties"]
        ]


def test_contract_enforced_and_data_types_present():
    for name in NAMES:
        for model in _models(name).values():
            assert model["config"] == {"contract": {"enforced": True}}
            assert model["description"]
            for col in model["columns"]:
                assert col["data_type"]
                assert "description" in col
    cols = {
        c["name"]: c for c in _models("bus-marts")["eta_models"]["columns"]
    }
    assert cols["operator"]["data_type"] == "VARCHAR"


def test_single_pk_gets_unique_and_not_null():
    model = _models("bus-marts")["localities"]
    contract = yaml.safe_load(_contract("bus-marts").read_text())
    obj = next(o for o in contract["schema"] if o["name"] == "localities")
    pks = [p["name"] for p in obj["properties"] if p.get("primaryKey")]
    assert len(pks) == 1
    col = next(c for c in model["columns"] if c["name"] == pks[0])
    assert "unique" in col["data_tests"]
    assert "not_null" in col["data_tests"]
    assert {"type": "not_null"} in col["constraints"]
    assert all(c["type"] != "primary_key" for c in col["constraints"])
    assert COMPOUND not in str(model.get("data_tests", ""))


def test_compound_pk_gets_dbt_expectations_test_in_position_order():
    model = _models("bus-marts")["eta_models"]
    assert model["data_tests"] == [
        {
            COMPOUND: {
                "column_list": [
                    "operator",
                    "line",
                    "direction",
                    "day_type",
                    "hour_band",
                ]
            }
        }
    ]
    for col in model["columns"]:
        if col["name"] in model["data_tests"][0][COMPOUND]["column_list"]:
            assert col["data_tests"] == ["not_null"]
            assert {"type": "not_null"} in col["constraints"]


def test_no_sources_block_and_no_loaded_at_required():
    for name in NAMES:
        _, text = next(iter(render_schema(_contract(name)).items()))
        doc = yaml.safe_load(text)
        assert "sources" not in doc
        assert "landed events" not in text
        assert "modelgenLoadedAtField" not in text


def test_custom_properties_become_meta():
    models = _models("bus-cdp")
    contract = yaml.safe_load(_contract("bus-cdp").read_text())
    for obj in contract["schema"]:
        expected: dict = {}
        for cp in obj.get("customProperties", []):
            expected.setdefault(cp["property"], []).append(cp["value"])
        expected = {k: v[0] if len(v) == 1 else v for k, v in expected.items()}
        if expected:
            assert models[obj["name"]]["meta"] == expected
        else:
            assert "meta" not in models[obj["name"]]
    # repeated properties keep every value, in declaration order
    pings = models["stg_bods__vehicle_pings"]["meta"]["consumes"]
    assert isinstance(pings, list) and len(pings) > 1
    assert "meta" not in _models("bus-marts")["eta_models"]


def test_table_and_server_options_are_honoured():
    only = _models("bus-marts", tables=["localities"])
    assert list(only) == ["localities"]
    with pytest.raises(Exception, match="No server named"):
        render_schema(_contract("bus-marts"), server="nope")


@pytest.mark.parametrize("name", NAMES)
def test_goldens_match(name, tmp_path):
    _cli("schema", str(_contract(name)), "-o", str(tmp_path), cwd=tmp_path)
    golden = GOLDEN / name
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        p.name for p in golden.iterdir()
    )
    for p in golden.iterdir():
        assert (tmp_path / p.name).read_text() == p.read_text()


def test_header_has_no_paths_or_timestamps(tmp_path):
    _, text = next(iter(render_schema(_contract("bus-marts")).items()))
    first = text.splitlines()[0]
    assert first.startswith("# GENERATED by modelgen ")
    assert first.endswith(
        " from wraith_bus_ingestor_marts 3.1.1. Do not edit."
    )
    assert "/" not in first


def test_schema_mode_output_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for name in NAMES:
        for out, seed in ((a, "0"), (b, "1")):
            _cli(
                "schema", str(_contract(name)), "-o", str(out),
                cwd=tmp_path, hashseed=seed,
            )  # fmt: skip
    diff = subprocess.run(
        ["diff", "-r", str(a), str(b)], capture_output=True, text=True
    )
    assert diff.returncode == 0, diff.stdout
    assert len(list(a.iterdir())) == 4
    cmp = filecmp.dircmp(a, b)
    assert not cmp.diff_files


def test_gen_layer_schema_matches_schema_command(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    c = _contract("bus-marts")
    _cli("schema", str(c), "-o", str(a), cwd=tmp_path)
    _cli("gen", str(c), "--layer", "schema", "-o", str(b), cwd=tmp_path)
    assert [p.read_text() for p in a.iterdir()] == [
        p.read_text() for p in b.iterdir()
    ]


def _project(tmp_path: Path) -> Path:
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "bus-marts.odcs.yaml").write_text(
        _contract("bus-marts").read_text()
    )
    (tmp_path / ".modelgen.yml").write_text(
        "schema_contracts: contracts/*.odcs.yaml\n"
        "schema_output: models/marts\n"
    )
    return tmp_path


def test_check_covers_schema_layer(tmp_path):
    proj = _project(tmp_path)
    missing = _cli("check", cwd=proj, check=False)
    assert missing.returncode == 1
    _cli(
        "schema", str(proj / "contracts" / "bus-marts.odcs.yaml"),
        "-o", str(proj / "models" / "marts"), cwd=proj,
    )  # fmt: skip
    ok = _cli("check", cwd=proj)
    assert "in sync" in ok.stdout.lower()
    target = next((proj / "models" / "marts").iterdir())
    target.write_text(target.read_text() + "# drift\n")
    drift = _cli("check", cwd=proj, check=False)
    assert drift.returncode == 1
