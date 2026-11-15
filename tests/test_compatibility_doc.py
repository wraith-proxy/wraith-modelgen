"""The compatibility policy page agrees with the code and is linked."""

from pathlib import Path

from modelgen.contract import _LEGACY_ODCS_VERSIONS, SUPPORTED_ODCS_VERSIONS

ROOT = Path(__file__).parent.parent
PAGE = ROOT / "docs" / "compatibility.md"


def _page() -> str:
    return PAGE.read_text()


def test_page_lists_every_odcs_version():
    page = _page()
    for version in (*SUPPORTED_ODCS_VERSIONS, *_LEGACY_ODCS_VERSIONS):
        assert f"`{version}`" in page


def test_page_links_api_linting_and_exit_codes():
    page = _page()
    assert "(api.md)" in page
    assert "(linting.md)" in page
    assert "(usage.md#exit-codes)" in page


def test_readme_links_the_page_and_marks_legacy_deprecated():
    readme = (ROOT / "README.md").read_text()
    assert "(docs/compatibility.md)" in readme
    assert "| Deprecated: still accepted" in readme


def test_page_is_in_the_nav():
    nav = (ROOT / "mkdocs.yml").read_text()
    assert "    - Compatibility: compatibility.md" in nav
