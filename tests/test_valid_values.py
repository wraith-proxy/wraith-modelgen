"""Both validValues forms map to one accepted_values test in every mode."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
GOLDEN = HERE / "golden" / "valid_values"
_RUNNER = "from modelgen.cli import main; main()"

CONTRACT = """\
apiVersion: {api}
kind: DataContract
id: 3c9e8a41-7b2d-4f6e-a1c5-9d0b2e4f6a81
name: signup_route
version: 1.0.0
status: active
servers:
  - server: landing
    type: bigquery
    project: example-project
    dataset: landing
schema:
  - name: signup_route
    physicalName: signup_route
    physicalType: table
    description: One row per sign-up with the route the user came through.
    customProperties:
      - property: modelgenLoadedAtField
        value: received_at
    properties:
      - name: user_id
        logicalType: string
        physicalType: STRING
        primaryKey: true
        primaryKeyPosition: 1
        required: true
      - name: route
        logicalType: string
        physicalType: STRING
        quality:
{quality}
      - name: received_at
        logicalType: date
        physicalType: TIMESTAMP
        required: true
"""
PROPERTY = """\
          - type: library
            rule: validValues
            validValues: [email, google, apple]"""
LIBRARY = """\
          - type: library
            metric: invalidValues
            arguments:
              validValues: [email, google, apple]
            mustBe: 0"""
# The property form is valid ODCS only under v3.0.2, which requires
# `rule` on every library rule, so the library form carries one there.
BOTH = f"""\
{PROPERTY}
          - type: library
            rule: invalidValues
            metric: invalidValues
            arguments:
              validValues: [email, google, apple]
            mustBe: 0"""
FORMS = {
    "property": ("v3.0.2", PROPERTY),
    "library": ("v3.2.0", LIBRARY),
    "both": ("v3.0.2", BOTH),
}
MODES = {
    "staging": ("gen", "--staging"),
    "schema": ("schema",),
    "sources": ("sources",),
}


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


def _contract(tmp_path: Path, form: str) -> Path:
    api, quality = FORMS[form]
    path = tmp_path / "c.odcs.yaml"
    path.write_text(CONTRACT.format(api=api, quality=quality))
    return path


def _files(root: Path) -> dict[Path, str]:
    return {
        p.relative_to(root): p.read_text()
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("mode", MODES)
def test_every_form_renders_the_mode_golden(tmp_path, mode, form):
    out = tmp_path / "out"
    result = _cli(*MODES[mode], str(_contract(tmp_path, form)), "-o",
                  str(out), cwd=tmp_path)  # fmt: skip
    assert _files(out) == _files(GOLDEN / mode)
    assert "not mapped" not in result.stderr


@pytest.mark.parametrize("mode", ["schema", "sources"])
def test_strict_accepts_the_property_form(tmp_path, mode):
    out = tmp_path / "out"
    result = _cli(
        mode, str(_contract(tmp_path, "property")), "-o", str(out),
        "--strict", cwd=tmp_path, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert "not mapped" not in result.stderr
