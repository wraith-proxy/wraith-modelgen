"""
Semantic lint tests.

lint_contract operates on a parsed EventContract, so these tests build the
object directly and mutate fields — the source format (ODCS) is irrelevant
to what lint checks. Assertions check the finding code so messages can be
reworded freely.
"""

import pytest

from modelgen.contract import (
    ColumnTest,
    EventContract,
    PartitionConfig,
    RawConfig,
    SourceRef,
    StagingColumn,
    StagingConfig,
)
from modelgen.lint import (
    Severity,
    errors,
    lint_contract,
    warnings,
)


def base_contract() -> EventContract:
    return EventContract(
        name="user_signed_up",
        description="",
        unique_key=["EVENT_ID"],
        loaded_at_field="RECEIVED_AT",
        source=SourceRef("my-project", "landing", "user_signups_raw"),
        raw=RawConfig(
            dataset="raw",
            incremental_strategy="merge",
            dedup=True,
            partition_by=PartitionConfig("RECEIVED_AT", "timestamp", "day"),
            cluster_by=["USER_ID"],
        ),
        staging=StagingConfig(
            dataset="staging",
            incremental_strategy="merge",
            partition_by=PartitionConfig("received_at", "timestamp", "day"),
            cluster_by=["user_id"],
            columns=[
                StagingColumn(
                    "EVENT_ID",
                    "event_id",
                    "STRING",
                    "Unique event ID.",
                    [ColumnTest("not_null"), ColumnTest("unique")],
                ),
                StagingColumn(
                    "USER_ID",
                    "user_id",
                    "STRING",
                    "User.",
                    [ColumnTest("not_null")],
                ),
                StagingColumn(
                    "RECEIVED_AT",
                    "received_at",
                    "TIMESTAMP",
                    "Load time.",
                    [ColumnTest("not_null")],
                ),
            ],
        ),
    )


def contract_with(mutate) -> EventContract:
    c = base_contract()
    mutate(c)
    return c


def error_codes(contract: EventContract) -> set[str]:
    return {f.code for f in errors(lint_contract(contract))}


def warning_codes(contract: EventContract) -> set[str]:
    return {f.code for f in warnings(lint_contract(contract))}


class TestCleanContract:
    def test_no_errors(self):
        assert errors(lint_contract(base_contract())) == []

    def test_no_warnings(self):
        assert warnings(lint_contract(base_contract())) == []


class TestEventChecks:
    def test_bad_event_name(self):
        c = contract_with(lambda c: setattr(c, "name", "User Signed Up!"))
        assert "invalid-event-name" in error_codes(c)

    def test_uppercase_event_name(self):
        c = contract_with(lambda c: setattr(c, "name", "UserSignedUp"))
        assert "invalid-event-name" in error_codes(c)

    def test_bad_project(self):
        c = contract_with(
            lambda c: setattr(
                c, "source", SourceRef("My_Project!", "landing", "t")
            )
        )
        assert "invalid-source-project" in error_codes(c)

    def test_bad_dataset(self):
        c = contract_with(
            lambda c: setattr(
                c, "source", SourceRef("my-project", "land-ing zone", "t")
            )
        )
        assert "invalid-source-dataset" in error_codes(c)

    def test_bad_table(self):
        c = contract_with(
            lambda c: setattr(
                c,
                "source",
                SourceRef("my-project", "landing", "users; drop table users"),
            )
        )
        assert "invalid-source-table" in error_codes(c)

    def test_bad_unique_key_identifier(self):
        c = contract_with(lambda c: setattr(c, "unique_key", ["EVENT ID"]))
        assert "invalid-unique-key" in error_codes(c)

    def test_duplicate_unique_key(self):
        c = contract_with(
            lambda c: setattr(c, "unique_key", ["EVENT_ID", "EVENT_ID"])
        )
        assert "duplicate-unique-key" in error_codes(c)

    def test_bad_loaded_at(self):
        c = contract_with(
            lambda c: setattr(c, "loaded_at_field", "RECEIVED AT")
        )
        assert "invalid-loaded-at-field" in error_codes(c)


class TestLayerConfigChecks:
    def test_bad_raw_strategy(self):
        c = contract_with(
            lambda c: setattr(c.raw, "incremental_strategy", "mrege")
        )
        assert "invalid-raw-strategy" in error_codes(c)

    def test_bad_staging_strategy(self):
        c = contract_with(
            lambda c: setattr(c.staging, "incremental_strategy", "upsert")
        )
        assert "invalid-staging-strategy" in error_codes(c)

    def test_microbatch_allowed(self):
        c = contract_with(
            lambda c: setattr(c.raw, "incremental_strategy", "microbatch")
        )
        assert "invalid-raw-strategy" not in error_codes(c)

    def test_bad_partition_data_type(self):
        c = contract_with(
            lambda c: setattr(
                c.raw,
                "partition_by",
                PartitionConfig("RECEIVED_AT", "varchar"),
            )
        )
        assert "invalid-raw-partition-type" in error_codes(c)

    def test_bad_partition_granularity(self):
        c = contract_with(
            lambda c: setattr(
                c.raw,
                "partition_by",
                PartitionConfig("RECEIVED_AT", "timestamp", "week"),
            )
        )
        assert "invalid-raw-partition-granularity" in error_codes(c)

    def test_bad_raw_dataset(self):
        c = contract_with(lambda c: setattr(c.raw, "dataset", "raw zone"))
        assert "invalid-raw-dataset" in error_codes(c)

    def test_bad_cluster_identifier(self):
        c = contract_with(lambda c: setattr(c.raw, "cluster_by", ["USER ID"]))
        assert "invalid-raw-cluster-column" in error_codes(c)


class TestStagingColumnChecks:
    def test_duplicate_source(self):
        def mutate(c):
            cols = c.staging.columns
            cols.append(StagingColumn("EVENT_ID", "event_id_2", "STRING", "x"))

        assert "duplicate-column-source" in error_codes(contract_with(mutate))

    def test_duplicate_name(self):
        def mutate(c):
            cols = c.staging.columns
            cols.append(StagingColumn("USER_ID", "event_id", "STRING", "x"))

        assert "duplicate-column-name" in error_codes(contract_with(mutate))

    def test_invalid_cast_type(self):
        def mutate(c):
            c.staging.columns[0].type = "VARCHAR(255)"

        assert "invalid-column-type" in error_codes(contract_with(mutate))

    def test_parameterised_types_allowed(self):
        def mutate(c):
            c.staging.columns[0].type = "NUMERIC(10, 2)"

        assert "invalid-column-type" not in error_codes(contract_with(mutate))

    def test_array_struct_types_allowed(self):
        def mutate(c):
            c.staging.columns[0].type = "ARRAY<STRING>"

        assert "invalid-column-type" not in error_codes(contract_with(mutate))

    def test_invalid_column_name(self):
        def mutate(c):
            c.staging.columns[0].name = "event id"

        assert "invalid-column-name" in error_codes(contract_with(mutate))

    def test_unique_key_missing_from_staging(self):
        def mutate(c):
            c.staging.columns = [
                col for col in c.staging.columns if col.source != "EVENT_ID"
            ]

        assert "unique-key-not-in-staging" in error_codes(
            contract_with(mutate)
        )

    def test_loaded_at_missing_from_staging(self):
        def mutate(c):
            c.staging.columns = [
                col for col in c.staging.columns if col.source != "RECEIVED_AT"
            ]
            c.staging.partition_by = None

        assert "loaded-at-not-in-staging" in error_codes(contract_with(mutate))

    def test_partition_field_uses_staging_name(self):
        def mutate(c):
            # Using the SOURCE name where the staging name is required
            c.staging.partition_by = PartitionConfig("RECEIVED_AT")

        assert "partition-field-not-in-staging" in error_codes(
            contract_with(mutate)
        )

    def test_cluster_column_must_be_declared(self):
        def mutate(c):
            c.staging.cluster_by = ["country_code"]

        assert "cluster-column-not-in-staging" in error_codes(
            contract_with(mutate)
        )

    def test_empty_staging_skips_column_checks(self):
        def mutate(c):
            c.staging = StagingConfig(dataset="staging", columns=[])

        assert error_codes(contract_with(mutate)) == set()


class TestWarnings:
    def test_missing_description_warns(self):
        def mutate(c):
            c.staging.columns[1].description = ""

        assert "missing-column-description" in warning_codes(
            contract_with(mutate)
        )

    def test_untested_unique_key_warns(self):
        def mutate(c):
            c.staging.columns[0].tests = []

        assert "unique-key-untested" in warning_codes(contract_with(mutate))

    def test_warnings_are_not_errors(self):
        def mutate(c):
            c.staging.columns[1].description = ""

        assert errors(lint_contract(contract_with(mutate))) == []


class TestFindingModel:
    def test_severity_flags(self):
        c = contract_with(
            lambda c: setattr(c.raw, "incremental_strategy", "nope")
        )
        finding = errors(lint_contract(c))[0]
        assert finding.is_error
        assert finding.severity is Severity.ERROR

    def test_multiple_errors_collected_in_one_pass(self):
        def mutate(c):
            c.raw.incremental_strategy = "nope"
            c.staging.incremental_strategy = "also-nope"
            c.staging.columns[0].type = "VARCHAR"

        codes = error_codes(contract_with(mutate))
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
    def mutate(c):
        c.staging.columns[0].type = bq_type

    assert "invalid-column-type" not in error_codes(contract_with(mutate))


@pytest.mark.parametrize(
    "bq_type",
    ["VARCHAR", "TEXT", "DOUBLE", "CHAR(4)", "BLOB"],
)
def test_invalid_bq_types_rejected(bq_type):
    def mutate(c):
        c.staging.columns[0].type = bq_type

    assert "invalid-column-type" in error_codes(contract_with(mutate))
