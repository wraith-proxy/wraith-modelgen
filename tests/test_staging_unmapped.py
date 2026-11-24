"""Event staging reports the quality rules it does not map."""

import json
import os
import subprocess
import sys
from pathlib import Path

_RUNNER = "from modelgen.cli import main; main()"

CONTRACT = """\
apiVersion: v3.2.0
kind: DataContract
id: 5d1f7c2a-9e4b-4a0d-8c3f-2b6e1a9d7f40
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
    quality:
      - id: daily_volume
        type: sql
        query: select count(*) from {object}
        mustBeGreaterThan: 0
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
          - type: library
            metric: invalidValues
            arguments:
              validValues: [email, google, apple]
            mustBe: 0
          - type: text
            description: Routes should match the sign-up page.
      - name: received_at
        logicalType: date
        physicalType: TIMESTAMP
        required: true
"""
RULES = ["stg__signup_route.daily_volume", "stg__signup_route.rule3"]
UNMAPPED = (
    "modelgen: 2 quality rule(s) not mapped:"
    " stg__signup_route.daily_volume, stg__signup_route.rule3\n"
)


def _cli(*args: str, cwd: Path):
    env = {**os.environ, "PYTHONHASHSEED": "0", "LC_ALL": "C"}
    return subprocess.run(
        [sys.executable, "-c", _RUNNER, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def _contract(tmp_path: Path) -> Path:
    path = tmp_path / "contracts" / "signup_route.yml"
    path.parent.mkdir()
    path.write_text(CONTRACT)
    return path


def test_gen_staging_reports_unmapped_rules_and_exits_0(tmp_path):
    contract = _contract(tmp_path)
    result = _cli("gen", str(contract), "--staging", "-o", "out",
                  cwd=tmp_path)  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert result.stderr == UNMAPPED


def _unmapped_issues(stdout: str) -> list[dict]:
    report = json.loads(stdout)
    assert report["ok"] is True
    return [
        i for i in report["issues"] if i["code"] == "quality-rule-unmapped"
    ]


def test_gen_staging_json_reports_each_unmapped_rule(tmp_path):
    contract = _contract(tmp_path)
    result = _cli("gen", str(contract), "--staging", "-o", "out",
                  "--format", "json", cwd=tmp_path)  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert _unmapped_issues(result.stdout) == [
        {
            "source": str(contract),
            "object": rule,
            "code": "quality-rule-unmapped",
            "severity": "warning",
            "message": f"quality rule {rule} is not mapped to a dbt test",
            "pointer": None,
            "line": None,
            "column": None,
            "exempt_reason": None,
        }
        for rule in RULES
    ]


def test_run_reports_unmapped_staging_rules_and_exits_0(tmp_path):
    _contract(tmp_path)
    result = _cli("run", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stderr == UNMAPPED
    assert (
        tmp_path / "models" / "staging" / "stg__signup_route.sql"
    ).is_file()


def test_run_json_reports_each_unmapped_staging_rule(tmp_path):
    _contract(tmp_path)
    result = _cli("run", "--format", "json", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    issues = _unmapped_issues(result.stdout)
    assert [(i["object"], i["severity"]) for i in issues] == [
        (rule, "warning") for rule in RULES
    ]
