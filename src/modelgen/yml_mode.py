"""
What the produced and extract modes share.

Both modes read a contract's schema objects and emit one dbt yml plus
singular tests, with no SQL. They differ in the per-object entry and the
top-level wrapper (`models:` or `sources:`). Everything else lives here.
"""

from __future__ import annotations

import re
from typing import Any

import yaml


def slug(contract_name: str) -> str:
    """Lower-cased, non-alphanumerics collapsed to one underscore."""
    return re.sub(r"[^a-z0-9]+", "_", contract_name.lower()).strip("_")


def custom_properties(obj: dict[str, Any]) -> dict[str, Any]:
    """customProperties ({property, value} list) as a mapping.

    A property named more than once (ODCS allows it, and real contracts
    use it, e.g. several `consumes`) becomes a list of its values in
    declaration order, so nothing is silently dropped.
    """
    seen: dict[str, list[Any]] = {}
    for cp in obj.get("customProperties") or []:
        if "property" in cp:
            seen.setdefault(str(cp["property"]), []).append(cp.get("value"))
    return {k: v[0] if len(v) == 1 else v for k, v in seen.items()}


def emit(
    stamp: str,
    filename: str,
    top_level: dict[str, Any],
    built: list[tuple[Any, Any]],
    unmapped: list[str] | None,
) -> dict[str, str]:
    """{relative path: content}: the yml, then each object's singular tests.

    `built` is the list of (entry, found) pairs, where `found` carries
    `.singular` (path to content) and `.unmapped` (quality rules that
    could not be mapped, appended to `unmapped` when it is given).
    """
    body = yaml.safe_dump(
        top_level, sort_keys=False, allow_unicode=True, width=79
    )
    files = {filename: f"# {stamp}\n" + body}
    for _, found in built:
        files.update(found.singular)
        if unmapped is not None:
            unmapped.extend(found.unmapped)
    return files
