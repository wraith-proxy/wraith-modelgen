"""Each failure class exits with its own code.

Codes are asserted as literal numbers so these tests check the published
contract independently of the ExitCode enum.
"""

from pathlib import Path

from typer.testing import CliRunner

from modelgen.cli import app

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "contracts" / "example_contract.yml"

runner = CliRunner()


def _contract(tmp_path: Path, text: str | None = None) -> Path:
    path = tmp_path / "contract.yml"
    path.write_text(EXAMPLE.read_text() if text is None else text)
    return path


def test_valid_contract_exits_0(tmp_path):
    result = runner.invoke(app, ["validate", str(_contract(tmp_path))])
    assert result.exit_code == 0, result.output


def test_yaml_syntax_error_exits_1(tmp_path):
    path = _contract(tmp_path, "name: [unclosed\n")
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 1, result.output


def test_both_raw_and_staging_exits_2(tmp_path):
    result = runner.invoke(
        app,
        ["gen", str(_contract(tmp_path)), "--raw", "--staging"],
    )
    assert result.exit_code == 2, result.output


def _lint_failing() -> str:
    text = EXAMPLE.read_text()
    assert "incrementalStrategy: merge" in text
    return text.replace(
        "incrementalStrategy: merge", "incrementalStrategy: mrege"
    )


def test_lint_failure_exits_3(tmp_path):
    path = _contract(tmp_path, _lint_failing())
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 3, result.output


def _project(tmp_path: Path, contracts: dict[str, str]) -> Path:
    (tmp_path / "contracts").mkdir()
    for name, text in contracts.items():
        (tmp_path / "contracts" / name).write_text(text)
    config = tmp_path / ".modelgen.yml"
    config.write_text(
        "contracts: contracts/*.yml\n"
        "raw_output: models/raw\n"
        "staging_output: models/staging\n"
    )
    return config


def test_drift_exits_4(tmp_path):
    config = _project(tmp_path, {"example.yml": EXAMPLE.read_text()})
    ran = runner.invoke(app, ["run", "--config", str(config)])
    assert ran.exit_code == 0, ran.output
    generated = next((tmp_path / "models" / "raw").glob("*.sql"))
    generated.write_text(generated.read_text() + "\n-- hand edit\n")
    result = runner.invoke(app, ["check", "--config", str(config)])
    assert result.exit_code == 4, result.output


def test_missing_credentials_exits_5(tmp_path, monkeypatch):
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    def no_credentials(*args, **kwargs):
        raise DefaultCredentialsError("no credentials")

    monkeypatch.setattr(google.auth, "default", no_credentials)
    result = runner.invoke(
        app, ["new", "proj.dataset.table", "-o", str(tmp_path)]
    )
    assert result.exit_code == 5, result.output


def test_unexpected_error_exits_6(tmp_path, monkeypatch):
    path = _contract(tmp_path)
    real_read_text = Path.read_text

    def failing_read_text(self, *args, **kwargs):
        if self == path:
            raise RuntimeError("disk on fire")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read_text)
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 6, result.output
    assert (
        "modelgen: internal error: RuntimeError: disk on fire."
        " This is a bug; run with --verbose for the traceback."
    ) in result.stderr


def test_run_with_invalid_and_lint_failing_contracts_exits_1(tmp_path):
    config = _project(
        tmp_path,
        {
            "a_lint.yml": _lint_failing(),
            "b_invalid.yml": "name: [unclosed\n",
        },
    )
    result = runner.invoke(app, ["run", "--config", str(config)])
    assert result.exit_code == 1, result.output


def _exit_code_rows() -> list[tuple[int, str]]:
    text = (ROOT / "docs" / "usage.md").read_text()
    section = text.split("\n## Exit codes\n", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0].strip("`").isdigit():
            rows.append((int(cells[0].strip("`")), cells[1]))
    return rows


def test_usage_docs_list_every_exit_code():
    assert _exit_code_rows() == [
        (0, "OK"),
        (1, "INVALID"),
        (2, "USAGE"),
        (3, "LINT"),
        (4, "DRIFT"),
        (5, "ENVIRONMENT"),
        (6, "INTERNAL"),
    ]
    assert "docs/usage.md#exit-codes" in (ROOT / "README.md").read_text()
