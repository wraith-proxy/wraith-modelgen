from pathlib import Path
import pytest
import yaml

from modelgen.contract import EventContract, StagingColumn
from modelgen.generator import Layer, generate
from modelgen.introspect import ColumnSchema, StaticSchemaSource


FIXTURE_CONTRACT = """
version: "1"
event:
  name: user_signed_up
  description: "User signup event."
  unique_key: event_id
  loaded_at_field: received_at

  landing:
    project: test-project
    dataset: landing
    table: USER_SIGNUPS_RAW

  raw:
    dataset: raw
    dedup: true
    partition_by: received_at
    cluster_by: [event_id]
    columns:
      - name: EVENT_ID
        description: "Unique event identifier."
        tests: [not_null, unique]

  staging:
    dataset: staging
    partition_by: received_at
    columns:
      - source: EVENT_ID
        name: event_id
        description: "Unique event ID."
        tests: [not_null, unique]
      - source: USER_ID
        name: customer_id
        description: "Customer ID."
        tests: [not_null]
      - source: SIGNUP_METHOD
        name: signup_method
        tests:
          - accepted_values:
              values: [email, google, apple]
      - source: RECEIVED_AT
        name: received_at
        data_type: TIMESTAMP
        description: "Ingestion timestamp."
        tests: [not_null]
      - source: AMOUNT_PENCE
        name: amount_gbp
        expression: "AMOUNT_PENCE / 100.0"
        data_type: NUMERIC
"""


LANDING_SCHEMA = [
    ColumnSchema(name="EVENT_ID", data_type="STRING"),
    ColumnSchema(name="USER_ID", data_type="STRING"),
    ColumnSchema(name="SIGNUP_METHOD", data_type="STRING"),
    ColumnSchema(name="RECEIVED_AT", data_type="STRING"),
    ColumnSchema(name="AMOUNT_PENCE", data_type="INT64"),
    ColumnSchema(name="COUNTRY_CODE", data_type="STRING"),
]


@pytest.fixture
def contract_file(tmp_path):
    p = tmp_path / "contract.yml"
    p.write_text(FIXTURE_CONTRACT)
    return p


@pytest.fixture
def contract(contract_file):
    return EventContract.from_file(contract_file)


@pytest.fixture
def schema_source():
    return StaticSchemaSource({
        "test-project.landing.USER_SIGNUPS_RAW": LANDING_SCHEMA,
    })


class TestContract:
    def test_basic_fields(self, contract):
        assert contract.name == "user_signed_up"
        assert contract.unique_key == "event_id"
        assert contract.landing.fqn == "test-project.landing.USER_SIGNUPS_RAW"

    def test_raw_metadata(self, contract):
        assert contract.raw.dedup is True
        assert contract.raw.partition_by == "received_at"
        assert contract.raw.cluster_by == ["event_id"]
        assert len(contract.raw.columns_meta) == 1

    def test_staging_columns_count(self, contract):
        assert len(contract.staging.columns) == 5

    def test_model_names(self, contract):
        assert contract.raw_model_name == "raw__user_signed_up"
        assert contract.staging_model_name == "stg__user_signed_up"


class TestStagingColumnExpressions:
    def test_simple_rename(self):
        col = StagingColumn(source="USER_ID", name="customer_id")
        assert col.select_expression == "USER_ID as customer_id"

    def test_identity_passthrough(self):
        col = StagingColumn(source="event_id", name="event_id")
        assert col.select_expression == "event_id"

    def test_safe_cast(self):
        col = StagingColumn(source="RECEIVED_AT", name="received_at", data_type="TIMESTAMP")
        assert col.select_expression == "safe_cast(RECEIVED_AT as TIMESTAMP) as received_at"

    def test_custom_expression_no_cast(self):
        col = StagingColumn(
            source="AMOUNT_PENCE",
            name="amount_gbp",
            expression="AMOUNT_PENCE / 100.0",
        )
        assert col.select_expression == "AMOUNT_PENCE / 100.0 as amount_gbp"

    def test_custom_expression_with_cast(self):
        col = StagingColumn(
            source="AMOUNT_PENCE",
            name="amount_gbp",
            expression="AMOUNT_PENCE / 100.0",
            data_type="NUMERIC",
        )
        # data_type wraps the expression in safe_cast
        assert col.select_expression == "safe_cast(AMOUNT_PENCE / 100.0 as NUMERIC) as amount_gbp"


class TestRawGeneration:
    def test_raw_files_created(self, contract, schema_source, tmp_path):
        written = generate(contract, Layer.RAW, schema_source, tmp_path)
        assert len(written) == 2
        assert any(p.name == "raw__user_signed_up.sql" for p in written)

    def test_raw_sql_has_config(self, contract, schema_source, tmp_path):
        generate(contract, Layer.RAW, schema_source, tmp_path)
        sql = (tmp_path / "raw__user_signed_up.sql").read_text()
        assert "materialized='incremental'" in sql
        assert "partition_by" in sql
        assert "cluster_by" in sql

    def test_raw_sql_includes_all_landing_cols(self, contract, schema_source, tmp_path):
        generate(contract, Layer.RAW, schema_source, tmp_path)
        sql = (tmp_path / "raw__user_signed_up.sql").read_text()
        for col in LANDING_SCHEMA:
            assert col.name in sql

    def test_raw_sql_uses_qualify_for_dedup(self, contract, schema_source, tmp_path):
        generate(contract, Layer.RAW, schema_source, tmp_path)
        sql = (tmp_path / "raw__user_signed_up.sql").read_text()
        assert "qualify row_number() over" in sql
        # Raw uses landing casing for column names
        assert "partition by EVENT_ID" in sql

    def test_raw_sql_no_casts(self, contract, schema_source, tmp_path):
        """Raw is 1:1 with landing. Casts happen in staging."""
        generate(contract, Layer.RAW, schema_source, tmp_path)
        sql = (tmp_path / "raw__user_signed_up.sql").read_text()
        assert "safe_cast" not in sql
        assert "cast(" not in sql

    def test_raw_yml_includes_all_landing_cols(self, contract, schema_source, tmp_path):
        generate(contract, Layer.RAW, schema_source, tmp_path)
        parsed = yaml.safe_load((tmp_path / "raw__user_signed_up.yml").read_text())
        col_names = [c["name"] for c in parsed["models"][0]["columns"]]
        for col in LANDING_SCHEMA:
            assert col.name in col_names

    def test_raw_yml_applies_metadata_overlay(self, contract, schema_source, tmp_path):
        generate(contract, Layer.RAW, schema_source, tmp_path)
        parsed = yaml.safe_load((tmp_path / "raw__user_signed_up.yml").read_text())
        event_id = next(c for c in parsed["models"][0]["columns"] if c["name"] == "EVENT_ID")
        assert event_id["description"] == "Unique event identifier."
        assert "not_null" in event_id["tests"]

    def test_raw_validates_loaded_at_field_exists(self, schema_source, tmp_path, contract_file):
        # Use a contract that references a field not in landing
        bad = contract_file.read_text().replace(
            "loaded_at_field: received_at",
            "loaded_at_field: nonexistent_field",
        )
        (tmp_path / "bad.yml").write_text(bad)
        bad_contract = EventContract.from_file(tmp_path / "bad.yml")
        with pytest.raises(ValueError, match="loaded_at_field"):
            generate(bad_contract, Layer.RAW, schema_source, tmp_path)


class TestStagingGeneration:
    def test_staging_files_created(self, contract, schema_source, tmp_path):
        written = generate(contract, Layer.STAGING, schema_source, tmp_path)
        assert len(written) == 2

    def test_staging_sql_uses_ref(self, contract, schema_source, tmp_path):
        generate(contract, Layer.STAGING, schema_source, tmp_path)
        sql = (tmp_path / "stg__user_signed_up.sql").read_text()
        assert "ref('raw__user_signed_up')" in sql
        assert "source(" not in sql

    def test_staging_sql_has_safe_cast(self, contract, schema_source, tmp_path):
        generate(contract, Layer.STAGING, schema_source, tmp_path)
        sql = (tmp_path / "stg__user_signed_up.sql").read_text()
        assert "safe_cast(RECEIVED_AT as TIMESTAMP) as received_at" in sql

    def test_staging_sql_has_renames(self, contract, schema_source, tmp_path):
        generate(contract, Layer.STAGING, schema_source, tmp_path)
        sql = (tmp_path / "stg__user_signed_up.sql").read_text()
        assert "USER_ID as customer_id" in sql

    def test_staging_sql_has_custom_expression(self, contract, schema_source, tmp_path):
        generate(contract, Layer.STAGING, schema_source, tmp_path)
        sql = (tmp_path / "stg__user_signed_up.sql").read_text()
        assert "safe_cast(AMOUNT_PENCE / 100.0 as NUMERIC) as amount_gbp" in sql

    def test_staging_excludes_undeclared_columns(self, contract, schema_source, tmp_path):
        """COUNTRY_CODE exists in landing but isn't declared in staging."""
        generate(contract, Layer.STAGING, schema_source, tmp_path)
        sql = (tmp_path / "stg__user_signed_up.sql").read_text()
        assert "country_code" not in sql.lower()
        assert "COUNTRY_CODE" not in sql

    def test_staging_validates_source_columns(self, schema_source, tmp_path, contract_file):
        bad = contract_file.read_text().replace(
            "source: SIGNUP_METHOD",
            "source: NONEXISTENT_COLUMN",
        )
        (tmp_path / "bad.yml").write_text(bad)
        bad_contract = EventContract.from_file(tmp_path / "bad.yml")
        with pytest.raises(ValueError, match="NONEXISTENT_COLUMN"):
            generate(bad_contract, Layer.STAGING, schema_source, tmp_path)

    def test_staging_yml_uses_declared_names(self, contract, schema_source, tmp_path):
        generate(contract, Layer.STAGING, schema_source, tmp_path)
        parsed = yaml.safe_load((tmp_path / "stg__user_signed_up.yml").read_text())
        col_names = [c["name"] for c in parsed["models"][0]["columns"]]
        assert "customer_id" in col_names  # renamed
        assert "USER_ID" not in col_names  # source name not exposed


class TestEmptyStagingContract:
    def test_empty_staging_raises(self, schema_source, tmp_path, contract_file):
        empty_contract = contract_file.read_text().split("staging:")[0] + """staging:
    dataset: staging
    columns: []
"""
        (tmp_path / "empty.yml").write_text(empty_contract)
        c = EventContract.from_file(tmp_path / "empty.yml")
        with pytest.raises(ValueError, match="must explicitly declare"):
            generate(c, Layer.STAGING, schema_source, tmp_path)


class TestDryRun:
    def test_dry_run_writes_nothing(self, contract, schema_source, tmp_path):
        out = tmp_path / "out"
        generate(contract, Layer.RAW, schema_source, out, dry_run=True)
        assert not out.exists()
