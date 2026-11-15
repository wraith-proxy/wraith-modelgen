import re
from pathlib import Path

import yaml

CZ_CONFIG = Path(__file__).parent.parent / ".cz.yaml"
CUSTOMIZE = yaml.safe_load(CZ_CONFIG.read_text())["commitizen"]["customize"]

COMMITS = [
    (":bug: feature/TJP-14115: x", "feature", "x"),
    ("👻 fix/TJP-15115: y", "fix", "y"),
    ("break/TJP-00001: z", "break", "z"),
]


def test_changelog_pattern_accepts_prefixed_and_plain_and_rejects_bump():
    pattern = CUSTOMIZE["changelog_pattern"]
    for message, _, _ in COMMITS:
        assert re.match(pattern, message), message
    bump = ":ghost: chore: release v0.15.0 -> v0.16.0 [skip ci]"
    assert re.match(pattern, bump) is None


def test_commit_parser_captures_type_and_message():
    for message, change_type, text in COMMITS:
        parsed = re.match(CUSTOMIZE["commit_parser"], message, re.MULTILINE)
        assert parsed is not None, message
        assert parsed["change_type"] == change_type
        assert parsed["message"] == text
