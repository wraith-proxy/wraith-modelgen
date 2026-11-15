"""The machine-readable report a command prints with --format json.

Its shape is pinned by schema/modelgen-report-v1.json.
"""

import hashlib
import json
import sys
from pathlib import Path

from modelgen import __version__
from modelgen.contract import Issue, node_positions, read_contract
from modelgen.exit_codes import ExitCode
from modelgen.lint import Finding
from modelgen.project import FileStatus, Problem, ProjectResult

SCHEMA_VERSION = 1


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def contract_issue(
    source: Path, issue: Issue, object_name: str | None = None
) -> dict:
    return {
        "source": str(source),
        "object": object_name,
        "code": issue.code,
        "severity": issue.severity,
        "message": issue.message,
        "pointer": issue.pointer,
        "line": issue.line,
        "column": issue.column,
        "exempt_reason": None,
    }


def finding_issue(
    source: Path,
    finding: Finding,
    object_name: str | None,
    positions: dict[str, tuple[int, int]],
) -> dict:
    line, column = positions.get(finding.pointer, (None, None))
    return {
        "source": str(source),
        "object": object_name,
        "code": finding.code,
        "severity": finding.severity.value,
        "message": finding.message,
        "pointer": finding.pointer or None,
        "line": line,
        "column": column,
        "exempt_reason": finding.exempt_reason or None,
    }


def build_report(
    command: str,
    exit_code: ExitCode,
    inputs: list[Path],
    issues: list[dict],
    files: list[dict],
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "modelgen",
        "tool_version": __version__,
        "command": command,
        "exit_code": int(exit_code),
        "ok": exit_code == ExitCode.OK,
        "inputs": [
            {"path": str(p), "sha256": sha256_bytes(p.read_bytes())}
            for p in inputs
        ],
        "issues": issues,
        "files": files,
    }


def file_entry(
    path: Path,
    layer: str,
    content: str,
    status: str,
    on_disk: str | None = None,
) -> dict:
    return {
        "path": str(path),
        "layer": layer,
        "sha256": sha256_bytes(content.encode()),
        "status": status,
        "on_disk_sha256": None
        if on_disk is None
        else sha256_bytes(on_disk.encode()),
    }


def plain_issue(
    source: Path | str,
    code: str,
    message: str,
    severity: str = "error",
    object_name: str | None = None,
) -> dict:
    return {
        "source": str(source),
        "object": object_name,
        "code": code,
        "severity": severity,
        "message": message,
        "pointer": None,
        "line": None,
        "column": None,
        "exempt_reason": None,
    }


def unmapped_issues(
    source: Path | str, rules: list[str], severity: str = "warning"
) -> list[dict]:
    return [
        plain_issue(
            source,
            "quality-rule-unmapped",
            f"quality rule {rule} is not mapped to a dbt test",
            severity,
            rule,
        )
        for rule in rules
    ]


_PROBLEM_CODE = {
    ExitCode.INVALID: "generation",
    ExitCode.ENVIRONMENT: "introspection",
}


def _problem_issues(problem: Problem) -> list[dict]:
    event = problem.label.partition(":")[2] or None
    if not problem.issues:
        code = (
            _PROBLEM_CODE.get(problem.exit_code, "contract")
            if problem.layer != "parse"
            else "contract"
        )
        return [
            plain_issue(
                problem.contract, code, problem.message, object_name=event
            )
        ]
    positions = (
        node_positions(read_contract(problem.contract))
        if any(isinstance(i, Finding) for i in problem.issues)
        else {}
    )
    return [
        finding_issue(problem.contract, i, event, positions)
        if isinstance(i, Finding)
        else contract_issue(problem.contract, i, event)
        for i in problem.issues
    ]


def project_report(command: str, result: ProjectResult) -> dict:
    issues = [i for p in result.problems for i in _problem_issues(p)]
    issues += unmapped_issues(
        result.contracts[0] if result.contracts else ".",
        result.unmapped_rules,
    )
    files = [
        file_entry(
            f.path,
            f.layer,
            f.content,
            f.status.value,
            f.on_disk if f.status is FileStatus.DRIFT else None,
        )
        for f in result.files
    ]
    return build_report(
        command, result.exit_code, result.contracts, issues, files
    )


def emit(report: dict) -> None:
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
