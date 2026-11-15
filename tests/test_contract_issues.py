"""validate reports every contract problem with where it sits in the file.

Line numbers are those of contracts/example_contract.yml, which the
contracts below copy line for line before changing single lines.
"""

from pathlib import Path

from typer.testing import CliRunner

from modelgen.cli import app
from tests.test_cli import strip_ansi

runner = CliRunner()
EXAMPLE = Path(__file__).parent.parent / "contracts" / "example_contract.yml"


def _contract(tmp_path: Path, changes: dict[int, tuple[str, str]]) -> Path:
    lines = EXAMPLE.read_text().splitlines(keepends=True)
    for number, (old, new) in changes.items():
        assert lines[number - 1] == old + "\n"
        lines[number - 1] = new + "\n"
    out = tmp_path / "contract.yml"
    out.write_text("".join(lines))
    return out


def test_validate_lists_every_schema_error_with_its_line(tmp_path):
    contract = _contract(
        tmp_path,
        {
            42: ("kind: DataContract", "kind: Bogus"),
            53: ("    type: bigquery", "    type: nosuchdb"),
            124: (
                "        logicalType: string",
                "        logicalType: bogus",
            ),
        },
    )

    result = runner.invoke(app, ["validate", str(contract)])

    out = " ".join(strip_ansi(result.output).split())
    assert result.exit_code == 1
    assert "(3 error(s))" in out
    assert "/kind (line 42): 'Bogus' is not one of" in out
    assert "/servers/0/type (line 53): 'nosuchdb' is not one of" in out
    assert (
        "/schema/0/properties/2/logicalType (line 124): 'bogus' is not one of"
        in out
    )


def test_validate_locates_the_last_of_a_duplicated_key(tmp_path):
    contract = _contract(
        tmp_path, {45: ("status: active", "status: active\nkind: Bogus")}
    )

    result = runner.invoke(app, ["validate", str(contract)])

    out = " ".join(strip_ansi(result.output).split())
    assert result.exit_code == 1
    assert "/kind (line 46): 'Bogus' is not one of" in out


def test_validate_names_the_line_of_a_yaml_syntax_error(tmp_path):
    contract = _contract(
        tmp_path, {46: ("name: user_signed_up", "name: user: signed_up")}
    )

    result = runner.invoke(app, ["validate", str(contract)])

    out = " ".join(strip_ansi(result.output).split())
    assert result.exit_code == 1
    assert "(line 46, column 11)" in out
