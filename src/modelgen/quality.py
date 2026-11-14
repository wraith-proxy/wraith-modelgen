"""
ODCS quality rules mapped to dbt tests (schema mode).

Mapping, per rule, in contract order:

- invalidValues + arguments.validValues, mustBe 0 -> `accepted_values`
  (NULL-tolerant `where` when the column is not required)
- rowCount with a bound -> `expect_table_row_count_to_be_between`
- duplicateValues mustBe 0 -> `unique` / compound-unique test
- nullValues mustBe 0 -> `not_null`
- `type: sql` with a scalar query and an operator, and library metrics
  with another threshold -> a generated singular test returning failing
  rows
- anything else (text, custom, unknown metrics) is reported, never
  dropped silently.

Severity: error -> dbt default (no config), warning/info -> `warn`.
A test identical to one already present (e.g. a primary-key `unique`) is
not repeated; the rule counts as covered.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field
from typing import Any

COMPOUND_TEST = "dbt_expectations.expect_compound_columns_to_be_unique"
ROWS_TEST = "dbt_expectations.expect_table_row_count_to_be_between"
TESTS_DIR = "tests/generated"

_OPERATORS = (
    "mustBe",
    "mustNotBe",
    "mustBeGreaterThan",
    "mustBeGreaterOrEqualTo",
    "mustBeLessThan",
    "mustBeLessOrEqualTo",
    "mustBeBetween",
    "mustNotBeBetween",
)
_SYMBOL = {
    "mustBe": "=",
    "mustNotBe": "<>",
    "mustBeGreaterThan": ">",
    "mustBeGreaterOrEqualTo": ">=",
    "mustBeLessThan": "<",
    "mustBeLessOrEqualTo": "<=",
}
_PLACEHOLDER = re.compile(r"\$?\{(object|schema|table|property|column)\}")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _operator(rule: dict[str, Any]) -> tuple[str, Any] | None:
    """The comparison, validated: numeric threshold or numeric pair."""
    for op in _OPERATORS:
        if op not in rule:
            continue
        val = rule[op]
        if op.endswith("Between"):
            ok = (
                isinstance(val, (list, tuple))
                and len(val) == 2
                and all(_num(v) for v in val)
            )
        else:
            ok = _num(val)
        return (op, val) if ok else None
    return None


def predicate(op: str, val: Any) -> str:
    if op == "mustBeBetween":
        return f"v between {val[0]} and {val[1]}"
    if op == "mustNotBeBetween":
        return f"v not between {val[0]} and {val[1]}"
    return f"v {_SYMBOL[op]} {val}"


@dataclass
class ModelTests:
    """Tests collected for one model."""

    model_tests: list[Any] = field(default_factory=list)
    column_tests: dict[str, list[Any]] = field(default_factory=dict)
    singular: dict[str, str] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)


def _with_severity(test: Any, severity: str | None) -> Any:
    if severity not in ("warning", "warn", "info"):
        return test
    cfg = {"severity": "warn"}
    if isinstance(test, str):
        return {test: {"config": cfg}}
    ((name, args),) = test.items()
    args = dict(args)
    args["config"] = {**args.get("config", {}), **cfg}
    return {name: args}


def _add(tests: list[Any], test: Any) -> None:
    if test not in tests:
        tests.append(test)


def _rows_test(op: str, val: Any) -> dict[str, Any] | None:
    if op == "mustBe":
        return {"min_value": val, "max_value": val}
    if op == "mustBeBetween":
        return {"min_value": val[0], "max_value": val[1]}
    if op in ("mustBeGreaterThan", "mustBeGreaterOrEqualTo"):
        args: dict[str, Any] = {"min_value": val}
    elif op in ("mustBeLessThan", "mustBeLessOrEqualTo"):
        args = {"max_value": val}
    else:
        return None
    if op in ("mustBeGreaterThan", "mustBeLessThan"):
        args["strictly"] = True
    return args


def _metric_query(metric: str, cols: list[str], percent: bool) -> str | None:
    """Scalar SQL for a library metric over {{ ref }} (single column)."""
    if len(cols) != 1:
        return None
    c = cols[0]
    if metric == "nullValues":
        n = f"(count(*) - count({c}))"
        total = "count(*)"
    elif metric == "duplicateValues":
        n = f"(count({c}) - count(distinct {c}))"
        total = f"count({c})"
    else:
        return None
    body = f"100.0 * {n} / nullif({total}, 0)" if percent else n
    return f"select {body} from {{object}}"


def _singular(
    header: str, rule_label: str, model: str, query: str,
    op: str, val: Any, severity: str | None, column: str | None,
    source: str | None = None,
) -> str | None:  # fmt: skip
    if column is None and re.search(r"\$?\{(property|column)\}", query):
        return None

    def sub(m: re.Match[str]) -> str:
        if m.group(1) in ("property", "column"):
            return str(column)
        if source is not None:
            return f"{{{{ source('{source}', '{model}') }}}}"
        return f"{{{{ ref('{model}') }}}}"

    body = _PLACEHOLDER.sub(sub, query.strip().rstrip(";").rstrip())
    lines = [f"-- {header}", f"-- Rule {rule_label}"]
    if severity in ("warning", "warn", "info"):
        lines.append("{{ config(severity='warn') }}")
    lines += [
        "",
        "select 1 as failure",
        "from (",
        textwrap.indent(body, "    "),
        ") as q(v)",
        f"where v is null or not ({predicate(op, val)})",
        "",
    ]
    return "\n".join(lines)


def map_object(
    obj: dict[str, Any],
    header: str,
    existing: ModelTests,
    source: str | None = None,
) -> ModelTests:
    """Map every quality rule of one schema object.

    `existing` carries the tests the schema already declares (keys,
    not_null) so identical rules are not repeated. With `source`, generated
    singular tests select from `{{ source(<source>, <table>) }}` rather than
    `{{ ref(<table>) }}`.
    """
    model = str(obj["name"])
    out = existing
    props = {str(p["name"]): p for p in obj.get("properties") or []}
    rules: list[tuple[dict[str, Any], str | None]] = [
        (r, None) for r in obj.get("quality") or []
    ]
    for pname, p in props.items():
        rules += [(r, pname) for r in p.get("quality") or []]

    used: set[str] = set()
    for ordinal, (rule, column) in enumerate(rules, start=1):
        rid = slug(str(rule["id"])) if rule.get("id") else f"rule{ordinal}"
        label = f"{model}.{rid}"
        if not _map_rule(
            rule, column, props, model, rid, label, header, used, out,
            source,
        ):  # fmt: skip
            out.unmapped.append(label)
    return out


def _map_rule(
    rule: dict[str, Any],
    column: str | None,
    props: dict[str, dict[str, Any]],
    model: str,
    rid: str,
    label: str,
    header: str,
    used: set[str],
    out: ModelTests,
    source: str | None = None,
) -> bool:
    sev = rule.get("severity")
    rtype = rule.get("type") or ("library" if rule.get("metric") else None)
    comp = _operator(rule)
    percent = rule.get("unit") == "percent"
    query: str | None = None
    cols = [column] if column else []

    if rtype == "library":
        metric = rule.get("metric")
        args = rule.get("arguments") or {}
        if metric == "invalidValues":
            values = args.get("validValues")
            if column is None or not values or comp != ("mustBe", 0):
                return False
            test: dict[str, Any] = {"values": list(values)}
            if not props[column].get("required") and not props[column].get(
                "primaryKey"
            ):
                test["config"] = {"where": f"{column} is not null"}
            _add(
                out.column_tests.setdefault(column, []),
                _with_severity({"accepted_values": test}, sev),
            )
            return True
        if comp is None:
            return False
        if metric == "rowCount":
            if column is not None or percent:
                return False
            args_ = _rows_test(*comp)
            if args_ is not None:
                _add(
                    out.model_tests,
                    _with_severity({ROWS_TEST: args_}, sev),
                )
                return True
            query = "select count(*) from {object}"
        elif metric in ("nullValues", "duplicateValues"):
            if column is None and metric == "duplicateValues":
                cols = [str(c) for c in args.get("properties") or []]
            if not cols or any(c not in props for c in cols):
                return False
            if comp[0] == "mustBe" and comp[1] == 0:
                return _zero_rule(metric, cols, sev, out)
            query = _metric_query(metric, cols, percent)
            column = cols[0]
        if query is None:
            return False
    elif rtype == "sql":
        query = rule.get("query")
        if not isinstance(query, str) or comp is None or percent:
            return False
    else:
        return False

    assert comp is not None
    base = f"{TESTS_DIR}/{model}__{rid}.sql"
    if base in used:
        return False
    text = _singular(
        header, label, model, query, comp[0], comp[1], sev, column, source
    )
    if text is None:
        return False
    used.add(base)
    out.singular[base] = text
    return True


def _zero_rule(
    metric: str, cols: list[str], sev: str | None, out: ModelTests
) -> bool:
    if metric == "nullValues":
        for c in cols:
            _add(
                out.column_tests.setdefault(c, []),
                _with_severity("not_null", sev),
            )
    elif len(cols) == 1:
        _add(
            out.column_tests.setdefault(cols[0], []),
            _with_severity("unique", sev),
        )
    else:
        _add(
            out.model_tests,
            _with_severity({COMPOUND_TEST: {"column_list": cols}}, sev),
        )
    return True
