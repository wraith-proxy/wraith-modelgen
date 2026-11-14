"""
Generation pipeline.

The generator is deterministic: given the same contract and the same source
schema, it produces byte-identical output. This is intentional. CI can diff
generated files against committed ones to catch contract drift.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from dataclasses import field as dc_field
from enum import Enum
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from modelgen import __version__
from modelgen.contract import ColumnTest, EventContract, StagingColumn
from modelgen.introspect import IntrospectedColumn, Introspector

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"

# The snapshot stamp. dbt's run date by default; the orchestrator overrides
# it with --vars '{ds: YYYY-MM-DD}' so one SQL file serves the daily run and
# any number of parallel backfills.
_DS_VALUE = 'date(\'{{ var("ds", run_started_at.strftime("%Y-%m-%d")) }}\')'
_DS_COLUMN = f"{_DS_VALUE} as ds"
_DS_TEST_WHERE = f"ds = {_DS_VALUE}"

# The keystore is a convention, not a contract: one per warehouse, declared
# as a dbt source. It is deliberately not a modelgen model: a snapshot of
# it would be a second copy of the thing whose deletion is the erasure.
_KEYSTORE = "{{ source('keystore', 'subject_keys') }}"
_AAD = "_modelgen_aad"

# Encryption returns BYTES whatever went in, so the contract's physicalType
# describes the plaintext and the emitted column is BYTES.
_CIPHERTEXT_TYPE = "BYTES"

# LEFT JOIN, not INNER: a subject with no keyset must not silently vanish
# from staging. The row survives with NULL ciphertext, which the not_null
# test on the subject column turns into a visible failure.
_KEYSTORE_CTE = """,

keyed as (

    select
        source.*,
        keystore.kek_resource_name,
        keystore.wrapped_dek_deterministic,
        keystore.wrapped_dek_randomised,
        cast(keystore.subject_id as bytes) as {aad}
    from source
    left join {keystore} as keystore
        on keystore.subject_id = source.{subject}

)"""


class Layer(str, Enum):
    RAW = "raw"
    STAGING = "staging"


class GenerationError(RuntimeError):
    """Raised when generation fails for reasons other than contract parsing."""


def _make_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    # Jinja's tojson is HTML-safe and escapes quotes to \uXXXX, which is
    # unreadable in a generated file a human reviews in a diff.
    env.filters["json"] = json.dumps
    return env


def _join_cols(exprs: list[str], indent: int) -> str:
    pad = " " * indent
    out = []
    for i, expr in enumerate(exprs):
        comma = "," if i < len(exprs) - 1 else ""
        out.append(f"{expr}{comma}")
    return f"\n{pad}".join(out)


def _staging_name_for(contract: EventContract, source_column: str) -> str:
    """Look up the staging column name for a given source column. Falls back
    to the source name if the column isn't in the staging contract."""
    for col in contract.staging.columns:
        if col.source == source_column:
            return col.name
    return source_column


def _staging_unique_key(contract: EventContract) -> list[str]:
    return [_staging_name_for(contract, k) for k in contract.unique_key]


def _staging_unique_key_dbt(contract: EventContract) -> str:
    keys = _staging_unique_key(contract)
    if len(keys) == 1:
        return f"'{keys[0]}'"
    return "[" + ", ".join(f"'{k}'" for k in keys) + "]"


def _validate_staging_against_source(
    contract: EventContract,
    introspected: list[IntrospectedColumn],
) -> None:
    """Fail fast if the staging contract references columns that don't exist
    in source. This is the moment schema drift is caught."""
    source_names = {c.name for c in introspected}
    missing = [
        c.source
        for c in contract.staging.columns
        if c.source not in source_names
    ]
    if missing:
        raise GenerationError(
            f"Staging contract references {len(missing)} column(s)"
            f" not present in {contract.source.fqn}: {missing}. "
            "Source schema may have evolved. Update the contract"
            " to match, or remove these columns."
        )


def _new_source_columns(
    contract: EventContract,
    introspected: list[IntrospectedColumn],
) -> list[str]:
    """Source columns not yet in the staging contract. Informational only;
    raw must be regenerated to include them."""
    declared = {c.source for c in contract.staging.columns}
    return [c.name for c in introspected if c.name not in declared]


def _validate_keys_present(
    contract: EventContract,
    introspected: list[IntrospectedColumn],
) -> None:
    """Unique key and loaded_at_field must exist in source."""
    source_names = {c.name for c in introspected}
    for key in contract.unique_key:
        if key not in source_names:
            raise GenerationError(
                f"unique_key column {key!r} not found in"
                f" source {contract.source.fqn}. "
                f"Available columns: {sorted(source_names)}"
            )
    if contract.loaded_at_field not in source_names:
        raise GenerationError(
            f"loaded_at_field {contract.loaded_at_field!r}"
            f" not found in source {contract.source.fqn}."
        )


def _yml_column(col: StagingColumn, snapshot: bool) -> StagingColumn:
    """Rewrite a contract column into what the schema.yml should declare.

    A snapshot's grain is (key, ds), so a bare unique test fails as soon as
    a second partition lands, so scope it to the partition this run writes,
    as a literal so BigQuery prunes to it instead of scanning history. A
    classified column arrives as BYTES, whatever its plaintext type was.
    """
    tests = col.tests
    if snapshot:
        tests = [
            ColumnTest("unique", {"config": {"where": _DS_TEST_WHERE}})
            if t.name == "unique"
            else t
            for t in tests
        ]
    if col.classification:
        return replace(col, type=_CIPHERTEXT_TYPE, tests=tests)
    return replace(col, tests=tests)


def _plaintext_expr(col: StagingColumn) -> str:
    """The value shaped for AEAD, which takes STRING or BYTES.

    The declared cast still runs, so the plaintext is the contract's type
    before it is stringified and a reader gets back what the contract
    promised. A column already cast to STRING needs only the one cast.
    """
    if col.type.strip().upper() == "STRING":
        return f"cast({col.source} as STRING)"
    if col.needs_cast:
        return f"cast(cast({col.source} as {col.type}) as string)"
    return f"cast({col.source} as string)"


def _transform_expr(col: StagingColumn, subject_source: str = "") -> str:
    """Build the SELECT expression for one staging column.

    Classified columns are encrypted here, on the way into staging, and are
    never materialised in plaintext anywhere downstream. The subject column
    is encrypted deterministically so that joins, row counts and aggregates
    survive the crypto-shred; everything else is randomised.
    """
    if col.classification:
        deterministic = col.source == subject_source
        fn = "deterministic_encrypt" if deterministic else "aead.encrypt"
        keyset = (
            "wrapped_dek_deterministic"
            if deterministic
            else "wrapped_dek_randomised"
        )
        return (
            f"{fn}(\n"
            f"        keys.keyset_chain("
            f"kek_resource_name, {keyset}),\n"
            f"        {_plaintext_expr(col)},\n"
            f"        {_AAD}\n"
            f"    ) as {col.name}"
        )
    if col.needs_cast:
        return f"cast({col.source} as {col.type}) as {col.name}"
    if col.needs_rename:
        return f"{col.source} as {col.name}"
    return col.name


def generated_header(contract: EventContract) -> str:
    """Fixed provenance line: no paths, no timestamps. Callers prefix it
    with the comment marker of the target file type."""
    return (
        f"GENERATED by modelgen {__version__} from"
        f" {contract.contract_name or contract.name}"
        f" {contract.contract_version}. Do not edit."
    )


def render_raw(
    contract: EventContract,
    introspector: Introspector,
) -> tuple[dict[str, str], list[IntrospectedColumn]]:
    """Render raw artefacts and return them with the introspected columns."""
    cols = introspector.get_columns(
        contract.source.project,
        contract.source.dataset,
        contract.source.table,
    )
    if not cols:
        raise GenerationError(
            f"Source table {contract.source.fqn} has no columns."
        )
    _validate_keys_present(contract, cols)

    col_names = [c.name for c in cols]
    i8 = "        "
    raw_source_cols = _join_cols(col_names, indent=8)
    raw_dedup_cols = f",\n{i8}".join(col_names) + ","
    raw_final_cols = _join_cols(col_names, indent=4)

    env = _make_env()
    header = generated_header(contract)
    ctx = {
        "event": contract,
        "header": header,
        "introspected_columns": cols,
        "freshness": contract.raw.freshness,
        "raw_source_cols": raw_source_cols,
        "raw_dedup_cols": raw_dedup_cols,
        "raw_final_cols": raw_final_cols,
        "unique_key_dbt": contract.unique_key_dbt,
        "unique_key_yaml": _render_unique_key_yaml(contract.unique_key),
        "cluster_by_literal": _render_cluster_by(contract.raw.cluster_by),
        "dbt_config": _render_dbt_config(
            unique_key_dbt=contract.unique_key_dbt,
            schema=contract.raw.dataset,
            incremental_strategy=contract.raw.incremental_strategy,
            partition_by=contract.raw.partition_by,
            cluster_by=contract.raw.cluster_by,
        ),
        "incremental_filter": _render_incremental_filter(
            loaded_at_field=contract.loaded_at_field,
            upstream_loaded_at_field=contract.loaded_at_field,
        ),
    }
    return {
        f"{contract.raw_model_name}.sql": env.get_template(
            "raw.sql.j2"
        ).render(ctx),
        f"{contract.raw_model_name}.yml": env.get_template(
            "raw_schema.yml.j2"
        ).render(ctx),
    }, cols


def render_staging(
    contract: EventContract,
    introspector: Introspector,
) -> tuple[dict[str, str], list[str]]:
    """Render staging artefacts. Returns (files, list of new source columns
    not yet in the contract). New columns are informational; nothing breaks."""
    if not contract.staging.columns:
        raise GenerationError(
            "Staging contract has no columns. Staging is the consumer-facing "
            "contract; declare what you intend to expose."
        )

    cols = introspector.get_columns(
        contract.source.project,
        contract.source.dataset,
        contract.source.table,
    )
    _validate_keys_present(contract, cols)
    _validate_staging_against_source(contract, cols)

    new_cols = _new_source_columns(contract, cols)
    if new_cols:
        logger.info(
            "Source has %d column(s) not yet in staging contract: %s",
            len(new_cols),
            new_cols,
        )

    snapshot = contract.staging.is_snapshot
    encrypts = contract.encrypts
    raw_cols = _join_cols(
        [c.source for c in contract.staging.columns], indent=8
    )
    exprs = [
        _transform_expr(c, contract.subject_field)
        for c in contract.staging.columns
    ]
    if snapshot:
        exprs.append(_DS_COLUMN)
    transformed_cols = _join_cols(exprs, indent=4)

    env = _make_env()
    header = generated_header(contract)
    staging_loaded_at = _staging_name_for(contract, contract.loaded_at_field)
    ctx = {
        "event": contract,
        "header": header,
        "snapshot": snapshot,
        "staging_columns": [
            _yml_column(c, snapshot) for c in contract.staging.columns
        ],
        "keystore_cte": (
            _KEYSTORE_CTE.format(
                aad=_AAD,
                keystore=_KEYSTORE,
                subject=contract.subject_field,
            )
            if encrypts
            else ""
        ),
        "select_from": "keyed" if encrypts else "source",
        "raw_cols": raw_cols,
        "transformed_cols": transformed_cols,
        "staging_unique_key_dbt": _staging_unique_key_dbt(contract),
        "staging_loaded_at_field": staging_loaded_at,
        "cluster_by_literal": _render_cluster_by(contract.staging.cluster_by),
        "dbt_config": _render_dbt_config(
            unique_key_dbt=(
                "" if snapshot else _staging_unique_key_dbt(contract)
            ),
            schema=contract.staging.dataset,
            incremental_strategy=contract.staging.incremental_strategy,
            partition_by=contract.staging.partition_by,
            cluster_by=contract.staging.cluster_by,
        ),
        # A snapshot reads the whole dimension every run, so there is nothing
        # to filter. Incremental reads from its own high-water mark: the
        # upstream column is the raw layer's name (== source name), the
        # same-model column is the staging name (post-rename).
        "incremental_filter": (
            ""
            if snapshot
            else _render_incremental_filter(
                loaded_at_field=staging_loaded_at,
                upstream_loaded_at_field=contract.loaded_at_field,
            )
        ),
    }
    return {
        f"{contract.staging_model_name}.sql": env.get_template(
            "staging.sql.j2"
        ).render(ctx),
        f"{contract.staging_model_name}.yml": env.get_template(
            "staging_schema.yml.j2"
        ).render(ctx),
    }, new_cols


def _render_cluster_by(cols: list[str]) -> str:
    """Render a cluster_by list as a dbt config literal."""
    if not cols:
        return ""
    if len(cols) == 1:
        return f"'{cols[0]}'"
    return "[" + ", ".join(f"'{c}'" for c in cols) + "]"


def _render_unique_key_yaml(keys: list[str]) -> str:
    """Render a unique_key as a YAML scalar or flow list for schema.yml."""
    if len(keys) == 1:
        return keys[0]
    return "[" + ", ".join(keys) + "]"


def _render_dbt_config(
    *,
    unique_key_dbt: str,
    schema: str,
    incremental_strategy: str,
    partition_by,
    cluster_by: list[str],
) -> str:
    """Render the full {{ config(...) }} block. Pre-built so the SQL template
    doesn't need to juggle conditional commas."""
    lines = [
        "    materialized='incremental'",
        f"    incremental_strategy='{incremental_strategy}'",
    ]
    # insert_overwrite replaces whole partitions and never reads unique_key.
    if unique_key_dbt:
        lines.append(f"    unique_key={unique_key_dbt}")
    lines.append(f"    schema='{schema}'")
    if partition_by is not None:
        lines.append(
            f"    partition_by={{"
            f"'field': '{partition_by.field}', "
            f"'data_type': '{partition_by.data_type}', "
            f"'granularity': '{partition_by.granularity}'"
            f"}}"
        )
    if cluster_by:
        lines.append(f"    cluster_by={_render_cluster_by(cluster_by)}")
    body = ",\n".join(lines)
    return "{{ config(\n" + body + "\n) }}"


def _render_incremental_filter(
    loaded_at_field: str, upstream_loaded_at_field: str
) -> str:
    """Render the {% if is_incremental() %} block that filters by loaded_at.

    upstream_loaded_at_field is the column name in this model's *upstream*
    (for raw: source column; for staging: raw column == source column).
    loaded_at_field is the column name in *this* model
    (used in select max(...)).
    """
    return (
        "    {%- if is_incremental() %}\n"
        f"    where {upstream_loaded_at_field} > (\n"
        f"        select max({loaded_at_field})\n"
        "        from {{ this }}\n"
        "    )\n"
        "    {%- endif %}"
    )


@dataclass
class GenerationResult:
    written: list[Path]
    new_source_columns: list[str] = dc_field(default_factory=list)
    introspected_columns: list[IntrospectedColumn] = dc_field(
        default_factory=list
    )
    artefacts: dict[str, str] = dc_field(default_factory=dict)


def generate(
    contract: EventContract,
    introspector: Introspector,
    output_dir: Path,
    layer: Layer,
    dry_run: bool = False,
) -> GenerationResult:
    """Render artefacts for one layer."""
    if layer == Layer.RAW:
        artefacts, introspected = render_raw(contract, introspector)
        new_cols: list[str] = []
    elif layer == Layer.STAGING:
        artefacts, new_cols = render_staging(contract, introspector)
        introspected = []
    else:
        raise GenerationError(f"Unknown layer: {layer}")

    written: list[Path] = []
    for filename, content in artefacts.items():
        dest = output_dir / filename
        if not dry_run:
            output_dir.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        written.append(dest)

    return GenerationResult(
        written=written,
        new_source_columns=new_cols,
        introspected_columns=introspected,
        artefacts=artefacts,
    )
