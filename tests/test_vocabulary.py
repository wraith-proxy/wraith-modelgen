"""Everything a user sees or commits uses the glossary's layer words."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCANNED = (
    "src", "docs", "README.md", "GLOSSARY.md", "contracts", "models", "tests",
)  # fmt: skip
VENDORED = "src/modelgen/schema/odcs-json-schema-*.json"
# Case-insensitive so lowercase fixture and golden names are caught too.
TAXONOMY = re.compile(
    r"\b[ROCF]DPs?\b|(Origin|Consumption|Raw|Feature) Data Product",
    re.IGNORECASE,
)


def _scanned_files():
    for name in SCANNED:
        top = ROOT / name
        for path in [top] if top.is_file() else sorted(top.rglob("*")):
            rel = path.relative_to(ROOT)
            if (
                path.is_file()
                and "__pycache__" not in rel.parts
                and not rel.match(VENDORED)
                and path != Path(__file__)
            ):
                yield rel


@pytest.mark.xfail(strict=True, reason="layer taxonomy not removed yet")
def test_no_layer_taxonomy_in_paths_or_contents():
    hits = []
    for rel in _scanned_files():
        if TAXONOMY.search(rel.as_posix()):
            hits.append(f"{rel} (path)")
        text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        for n, line in enumerate(text.splitlines(), 1):
            if TAXONOMY.search(line):
                hits.append(f"{rel}:{n}: {line.strip()[:80]}")
    assert not hits, "\n".join(hits)


def test_generic_data_product_phrases_pass():
    for phrase in (
        "sovereign data products", "Data product name", "Data Product Owner",
    ):  # fmt: skip
        assert not TAXONOMY.search(phrase)
