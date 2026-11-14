import pytest
from test_modelgen import CONTRACT_YAML

from modelgen.contract import EventContract
from modelgen.dialect import BigQueryDialect, UnknownDialectError, get_dialect

# The literal set lint.py held before the dialect seam.
_PREVIOUS_BQ_TYPES = {
    "STRING", "BYTES", "INT64", "INT", "SMALLINT", "INTEGER", "BIGINT",
    "TINYINT", "BYTEINT", "FLOAT64", "FLOAT", "NUMERIC", "DECIMAL",
    "BIGNUMERIC", "BIGDECIMAL", "BOOL", "BOOLEAN", "TIMESTAMP", "DATE",
    "TIME", "DATETIME", "GEOGRAPHY", "JSON", "INTERVAL",
}  # fmt: skip


def test_get_dialect_bigquery_returns_bigquery_dialect():
    assert isinstance(get_dialect("bigquery"), BigQueryDialect)


def test_unknown_dialect_error_lists_supported():
    with pytest.raises(UnknownDialectError) as exc:
        get_dialect("oracle")
    assert "oracle" in str(exc.value)
    assert "bigquery" in str(exc.value)


def test_bigquery_valid_types_match_previous_set():
    assert get_dialect("bigquery").valid_physical_types() == frozenset(
        _PREVIOUS_BQ_TYPES
    )


def test_bigquery_render_dbt_config_has_partition_by():
    contract = EventContract.from_text(CONTRACT_YAML)
    # Golden string captured from the pre-refactor generator.
    assert get_dialect("bigquery").render_dbt_config(contract, "raw") == (
        "{{ config(\n"
        "    materialized='incremental',\n"
        "    incremental_strategy='merge',\n"
        "    unique_key='EVENT_ID',\n"
        "    schema='raw',\n"
        "    partition_by={'field': 'RECEIVED_AT', 'data_type': 'timestamp',"
        " 'granularity': 'day'},\n"
        "    cluster_by='USER_ID'\n"
        ") }}"
    )
