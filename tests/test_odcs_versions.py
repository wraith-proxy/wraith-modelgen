"""ODCS apiVersion support: v3.0.2 through v3.2.0."""

from pathlib import Path

import pytest
import yaml

from modelgen.contract import (
    SUPPORTED_ODCS_VERSIONS,
    ContractError,
    EventContract,
    odcs_schema_errors,
)

FIXTURES = Path(__file__).parent / "fixtures" / "odcs_v3_2"


def test_supported_versions_constant():
    assert SUPPORTED_ODCS_VERSIONS == ("v3.0.2", "v3.1.0", "v3.2.0")


NAMES = ["bus-marts", "bus-positions", "gtfs-timetable"]


def _load(name):
    return yaml.safe_load((FIXTURES / f"{name}.odcs.yaml").read_text())


@pytest.mark.parametrize("name", NAMES)
def test_accepts_v3_2_0_contracts(name):
    doc = _load(name)
    assert doc["apiVersion"] == "v3.2.0"
    errors = odcs_schema_errors(doc)
    version_errors = [e for e in errors if list(e.path) == ["apiVersion"]]
    assert version_errors == []


@pytest.mark.parametrize(
    "name",
    [
        "bus-marts",
        pytest.param(
            "bus-positions",
            marks=pytest.mark.xfail(
                strict=True,
                reason="quality dimension 'validity' is not in the v3.2.0"
                " enum (schema/0/quality/2/dimension)",
            ),
        ),
        pytest.param(
            "gtfs-timetable",
            marks=pytest.mark.xfail(
                strict=True,
                reason="unquoted YAML description text in"
                " schema/1/properties/4 and schema/2/properties/10"
                " yields stray keys (unevaluated properties)",
            ),
        ),
    ],
)
def test_v3_2_0_contracts_fully_schema_valid(name):
    assert odcs_schema_errors(_load(name)) == []


def test_accepts_v3_1_0_minimal():
    doc = {
        "apiVersion": "v3.1.0",
        "kind": "DataContract",
        "id": "11111111-2222-3333-4444-555555555555",
        "version": "1.0.0",
        "status": "active",
    }
    assert odcs_schema_errors(doc) == []


def test_rejects_unknown_version():
    text = (
        "apiVersion: v9.9.9\nkind: DataContract\n"
        "id: x\nversion: 1.0.0\nstatus: active\n"
    )
    with pytest.raises(ContractError) as exc:
        EventContract.from_text(text)
    msg = str(exc.value)
    assert "v9.9.9" in msg
    for v in SUPPORTED_ODCS_VERSIONS:
        assert v in msg
