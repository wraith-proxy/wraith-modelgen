"""
Semantic lint tests.

Each rule gets a contract that violates exactly one thing; assertions
check the finding code so messages can be reworded freely.
"""

import copy

import pytest
import yaml

from modelgen.contract import EventContract
from modelgen.lint import (
    Severity,
    errors,
    lint_contract,
    warnings,
)

VALID_CONTRACT = yaml.safe_load(
    """
version: "1"
event:
  name: user_signed_up
  unique_key: EVENT_ID
  loaded_at_field: RECEIVED_AT
  source:
    project: my-project
    dataset: landing
    table: user_signups_raw
  raw:
    dataset: raw
    incremental_strategy: merge
    dedup: true
    partition_by:
      field: RECEIVED_AT
      data_type: timestamp
      granularity: day
    cluster_by: [USER_ID]
  staging:
    dataset: staging
    incremental_strategy: merge
    partition_by:
      field: received_at
      data_type: timestamp
      granularity: day
    cluster_by: [user_id]
    columns:
      - source: EVENT_ID
        name: event_id
        type: STRING
        description: "Unique event ID."
        tests: [not_null, unique]
      - source: USER_ID
        name: user_id
        type: STRING
        description: "User."
        tests: [not_null]
      - source: RECEIVED_AT
        name: received_at
        type: TIMESTAMP
        description: "Load time."
        tests: [not_null]
"""
)


def contract_with(mutate) -> EventContract:
    raw = copy.deepcopy(VALID_CONTRACT)
    mutate(raw["event"])
    return _parse(raw)


def _parse(raw: dict, tmp_path=None) -> EventContract:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "c.yml"
        p.write_text(yaml.safe_dump(raw))
        return EventContract.from_file(p)


def error_codes(contract: EventContract) -> set[str]:
    return {f.code for f in errors(lint_contract(contract))}


def warning_codes(contract: EventContract) -> set[str]:
    return {f.code for f in warnings(lint_contract(contract))}


class TestCleanContract:
    def test_no_errors(self):
        c = contract_with(lambda e: None)
        assert errors(lint_contract(c)) == []

    def test_no_warnings(self):
        c = contract_with(lambda e: None)
        assert warnings(lint_contract(c)) == []


class TestEventChecks:
    def test_bad_event_name(self):
        c = contract_with(lambda e: e.update(name="User Signed Up!"))
        assert "invalid-event-name" in error_codes(c)

    def test_uppercase_event_name(self):
        c = contract_with(lambda e: e.update(name="UserSignedUp"))
        assert "invalid-event-name" in error_codes(c)

    def test_bad_project(self):
        c = contract_with(lambda e: e["source"].update(project="My_Project!"))
        assert "invalid-source-project" in error_codes(c)

    def test_bad_dataset(self):
        c = contract_with(
            lambda e: e["source"].update(dataset="land-ing zone")
        )
        assert "invalid-source-dataset" in error_codes(c)

    def test_bad_table(self):
        c = contract_with(
            lambda e: e["source"].update(table="users; drop table users")
        )
        assert "invalid-source-table" in error_codes(c)

    def test_bad_unique_key_identifier(self):
        c = contract_with(lambda e: e.update(unique_key="EVENT ID"))
        assert "invalid-unique-key" in error_codes(c)

    def test_duplicate_unique_key(self):
        def mutate(e):
            e["unique_key"] = ["EVENT_ID", "EVENT_ID"]

        c = contract_with(mutate)
        assert "duplicate-unique-key" in error_codes(c)

    def test_bad_loaded_at(self):
        c = contract_with(lambda e: e.update(loaded_at_field="RECEIVED AT"))
        assert "invalid-loaded-at-field" in error_codes(c)


class TestLayerConfigChecks:
    def test_bad_raw_strategy(self):
        c = contract_with(
            lambda e: e["raw"].update(incremental_strategy="mrege")
        )
        assert "invalid-raw-strategy" in error_codes(c)

    def test_bad_staging_strategy(self):
        c = contract_with(
            lambda e: e["staging"].update(incremental_strategy="upsert")
        )
        assert "invalid-staging-strategy" in error_codes(c)

    def test_microbatch_allowed(self):
        c = contract_with(
            lambda e: e["raw"].update(incremental_strategy="microbatch")
        )
        assert "invalid-raw-strategy" not in error_codes(c)

    def test_bad_partition_data_type(self):
        c = contract_with(
            lambda e: e["raw"]["partition_by"].update(data_type="varchar")
        )
        assert "invalid-raw-partition-type" in error_codes(c)

    def test_bad_partition_granularity(self):
        c = contract_with(
            lambda e: e["raw"]["partition_by"].update(granularity="week")
        )
        assert "invalid-raw-partition-granularity" in error_codes(c)

    def test_bad_raw_dataset(self):
        c = contract_with(lambda e: e["raw"].update(dataset="raw zone"))
        assert "invalid-raw-dataset" in error_codes(c)

    def test_bad_cluster_identifier(self):
        c = contract_with(lambda e: e["raw"].update(cluster_by=["USER ID"]))
        assert "invalid-raw-cluster-column" in error_codes(c)


class TestStagingColumnChecks:
    def test_duplicate_source(self):
        def mutate(e):
            cols = e["staging"]["columns"]
            cols.append(dict(cols[0], name="event_id_2"))

        c = contract_with(mutate)
        assert "duplicate-column-source" in error_codes(c)

    def test_duplicate_name(self):
        def mutate(e):
            cols = e["staging"]["columns"]
            cols.append(dict(cols[0], source="USER_ID"))

        c = contract_with(mutate)
        # USER_ID now declared twice (dupe source) and event_id twice
        assert "duplicate-column-name" in error_codes(c)

    def test_invalid_cast_type(self):
        def mutate(e):
            e["staging"]["columns"][0]["type"] = "VARCHAR(255)"

        c = contract_with(mutate)
        assert "invalid-column-type" in error_codes(c)

    def test_parameterised_types_allowed(self):
        def mutate(e):
            e["staging"]["columns"][0]["type"] = "NUMERIC(10, 2)"

        c = contract_with(mutate)
        assert "invalid-column-type" not in error_codes(c)

    def test_array_struct_types_allowed(self):
        def mutate(e):
            e["staging"]["columns"][0]["type"] = "ARRAY<STRING>"

        c = contract_with(mutate)
        assert "invalid-column-type" not in error_codes(c)

    def test_invalid_column_name(self):
        def mutate(e):
            e["staging"]["columns"][0]["name"] = "event id"

        c = contract_with(mutate)
        assert "invalid-column-name" in error_codes(c)

    def test_unique_key_missing_from_staging(self):
        def mutate(e):
            e["staging"]["columns"] = [
                c for c in e["staging"]["columns"] if c["source"] != "EVENT_ID"
            ]

        c = contract_with(mutate)
        assert "unique-key-not-in-staging" in error_codes(c)

    def test_loaded_at_missing_from_staging(self):
        def mutate(e):
            e["staging"]["columns"] = [
                c
                for c in e["staging"]["columns"]
                if c["source"] != "RECEIVED_AT"
            ]
            e["staging"].pop("partition_by")

        c = contract_with(mutate)
        assert "loaded-at-not-in-staging" in error_codes(c)

    def test_partition_field_uses_staging_name(self):
        def mutate(e):
            # Using the SOURCE name where the staging name is required
            e["staging"]["partition_by"]["field"] = "RECEIVED_AT"

        c = contract_with(mutate)
        assert "partition-field-not-in-staging" in error_codes(c)

    def test_cluster_column_must_be_declared(self):
        def mutate(e):
            e["staging"]["cluster_by"] = ["country_code"]

        c = contract_with(mutate)
        assert "cluster-column-not-in-staging" in error_codes(c)

    def test_empty_staging_skips_column_checks(self):
        def mutate(e):
            e["staging"] = {"dataset": "staging", "columns": []}

        c = contract_with(mutate)
        assert error_codes(c) == set()


class TestWarnings:
    def test_missing_description_warns(self):
        def mutate(e):
            e["staging"]["columns"][1].pop("description")

        c = contract_with(mutate)
        assert "missing-column-description" in warning_codes(c)

    def test_untested_unique_key_warns(self):
        def mutate(e):
            e["staging"]["columns"][0].pop("tests")

        c = contract_with(mutate)
        assert "unique-key-untested" in warning_codes(c)

    def test_warnings_are_not_errors(self):
        def mutate(e):
            e["staging"]["columns"][1].pop("description")

        c = contract_with(mutate)
        assert errors(lint_contract(c)) == []


class TestFindingModel:
    def test_severity_flags(self):
        c = contract_with(
            lambda e: e["raw"].update(incremental_strategy="nope")
        )
        finding = errors(lint_contract(c))[0]
        assert finding.is_error
        assert finding.severity is Severity.ERROR

    def test_multiple_errors_collected_in_one_pass(self):
        def mutate(e):
            e["raw"]["incremental_strategy"] = "nope"
            e["staging"]["incremental_strategy"] = "also-nope"
            e["staging"]["columns"][0]["type"] = "VARCHAR"

        c = contract_with(mutate)
        codes = error_codes(c)
        assert {
            "invalid-raw-strategy",
            "invalid-staging-strategy",
            "invalid-column-type",
        } <= codes


@pytest.mark.parametrize(
    "bq_type",
    [
        "STRING",
        "INT64",
        "FLOAT64",
        "NUMERIC",
        "BIGNUMERIC",
        "BOOL",
        "TIMESTAMP",
        "DATE",
        "TIME",
        "DATETIME",
        "GEOGRAPHY",
        "JSON",
        "BYTES",
        "string",
        "timestamp",
        "STRING(255)",
        "NUMERIC(38, 9)",
        "ARRAY<INT64>",
        "STRUCT<a INT64, b STRING>",
    ],
)
def test_valid_bq_types_accepted(bq_type):
    def mutate(e):
        e["staging"]["columns"][0]["type"] = bq_type

    c = contract_with(mutate)
    assert "invalid-column-type" not in error_codes(c)


@pytest.mark.parametrize(
    "bq_type",
    ["VARCHAR", "TEXT", "DOUBLE", "CHAR(4)", "BLOB"],
)
def test_invalid_bq_types_rejected(bq_type):
    def mutate(e):
        e["staging"]["columns"][0]["type"] = bq_type

    c = contract_with(mutate)
    assert "invalid-column-type" in error_codes(c)
