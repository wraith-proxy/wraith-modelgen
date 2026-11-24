"""Single-contract commands write every file or none."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
REAL = HERE / "fixtures" / "real_contracts"
MULTI = HERE / "fixtures" / "multi_table" / "bus_marts.yaml"
_RUNNER = "from modelgen.cli import main; main()"


def _cli(*args: str, cwd: Path):
    env = {**os.environ, "PYTHONHASHSEED": "0", "LC_ALL": "C"}
    return subprocess.run(
        [sys.executable, "-c", _RUNNER, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def _trap(out: Path, name: str, tmp_path: Path) -> Path:
    """Symlink `name` under `out` to a file outside it; return that file."""
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (out / name).parent.mkdir(parents=True, exist_ok=True)
    (out / name).symlink_to(victim)
    return victim


def _written(out: Path) -> list[Path]:
    return [p for p in out.rglob("*") if p.is_file() and not p.is_symlink()]


def test_gen_writes_nothing_when_a_later_table_is_refused(tmp_path):
    out = tmp_path / "out"
    victim = _trap(out, "raw__localities.sql", tmp_path)

    result = _cli("gen", str(MULTI), "--raw", "-o", str(out), cwd=tmp_path)

    assert result.returncode == 1
    assert "Refusing to write" in result.stderr
    assert _written(out) == []
    assert victim.read_text() == "keep"


@pytest.mark.parametrize(
    "command, contract, refused",
    [
        (
            "schema",
            "bus-marts",
            "tests/generated/localities__localities_contains_known_place.sql",
        ),
        (
            "sources",
            "bus-positions-extract",
            "tests/generated/bus_positions__extract_row_count.sql",
        ),
    ],
)
def test_mode_writes_nothing_when_a_later_file_is_refused(
    tmp_path, command, contract, refused
):
    out = tmp_path / "out"
    victim = _trap(out, refused, tmp_path)
    path = REAL / f"{contract}.odcs.yaml"

    result = _cli(command, str(path), "-o", str(out), cwd=tmp_path)

    assert result.returncode == 1
    assert "Refusing to write" in result.stderr
    assert _written(out) == []
    assert victim.read_text() == "keep"
