"""The machine-readable report a command prints with --format json.

Its shape is pinned by schema/modelgen-report-v1.json.
"""

import hashlib
import json
import sys
from pathlib import Path

from modelgen import __version__
from modelgen.contract import Issue
from modelgen.exit_codes import ExitCode
from modelgen.lint import Finding

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


def emit(report: dict) -> None:
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
