"""
Comprehensive test suite for modelgen.

Uses FakeIntrospector throughout. No live BigQuery needed.

Contract convention: `unique_key` and `loaded_at_field` name SOURCE columns.
Raw layer uses them directly; staging looks up their renamed counterparts.
"""

from pathlib import Path

import pytest
import yaml

from modelgen.config import ConfigError, ModelgenConfig
from modelgen.contract import (
    ContractError,
    EventContract,
    FreshnessConfig,
    FreshnessThreshold,
    StagingColumn,
)
from modelgen.generator import (
    GenerationError,
    Layer,
    generate,
)
from modelgen.introspect import (
    ContractIntrospector,
    FakeIntrospector,
    IntrospectedColumn,
    IntrospectionError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SOURCE_PROJECT = "my-project"
SOURCE_DATASET = "landing"
SOURCE_TABLE = "user_signups_raw"

CONTRACT_YAML = """
version: "1.0.0"
kind: DataContract
apiVersion: v3.0.2
id: 11111111-1111-1111-1111-111111111111
status: active
name: user_signed_up
description:
  purpose: "Fired when a user signs up."
servers:
  - server: landing
    type: bigquery
    project: my-project
    dataset: landing
schema:
  - name: user_signed_up
    physicalName: user_signups_raw
    physicalType: table
    description: "Fired when a user signs up."
    customProperties:
      - property: modelgenLoadedAtField
        value: RECEIVED_AT
      - property: modelgenRaw
        value:
          dataset: raw
          incrementalStrategy: merge
          dedup: true
          partitionBy:
            field: RECEIVED_AT
            dataType: timestamp
            granularity: day
          freshness:
            warnAfter: {count: 12, period: hour}
            errorAfter: {count: 24, period: hour}
          clusterBy: [USER_ID]
      - property: modelgenStaging
        value:
          dataset: staging
          materialization: incremental
          incrementalStrategy: merge
          partitionBy:
            field: received_at
            dataType: timestamp
            granularity: day
    properties:
      - name: event_id
        physicalName: EVENT_ID
        logicalType: string
        physicalType: STRING
        description: "Unique event ID."
        primaryKey: true
        primaryKeyPosition: 1
        required: true
        unique: true
      - name: user_id
        physicalName: USER_ID
        logicalType: string
        physicalType: STRING
        required: true
      - name: signup_method
        physicalName: SIGNUP_METHOD
        logicalType: string
        physicalType: STRING
        quality:
          - type: library
            rule: validValues
            validValues: ["email", "google", "apple"]
      - name: received_at
        physicalName: RECEIVED_AT
        logicalType: date
        physicalType: TIMESTAMP
        required: true
"""


SNAPSHOT_CONTRACT_YAML = CONTRACT_YAML.replace(
    """          materialization: incremental
          incrementalStrategy: merge
          partitionBy:
            field: received_at
            dataType: timestamp
            granularity: day
""",
    "          materialization: snapshot\n",
)


# user_id is the subject (deterministic); received_at is payload
# (randomised). event_id and signup_method stay plaintext.
CLASSIFIED_CONTRACT_YAML = (
    SNAPSHOT_CONTRACT_YAML.replace(
        """      - property: modelgenLoadedAtField
        value: RECEIVED_AT
""",
        """      - property: modelgenLoadedAtField
        value: RECEIVED_AT
      - property: modelgenSubjectField
        value: USER_ID
""",
    )
    .replace(
        """      - name: user_id
        physicalName: USER_ID
        logicalType: string
        physicalType: STRING
        required: true
""",
        """      - name: user_id
        physicalName: USER_ID
        logicalType: string
        physicalType: STRING
        required: true
        classification: pii
""",
    )
    .replace(
        """      - name: received_at
        physicalName: RECEIVED_AT
        logicalType: date
        physicalType: TIMESTAMP
        required: true
""",
        """      - name: received_at
        physicalName: RECEIVED_AT
        logicalType: date
        physicalType: TIMESTAMP
        required: true
        classification: psi
""",
    )
)


def _source_columns() -> list[IntrospectedColumn]:
    return [
        IntrospectedColumn("EVENT_ID", "STRING", "REQUIRED"),
        IntrospectedColumn("USER_ID", "STRING", "REQUIRED"),
        IntrospectedColumn("SIGNUP_METHOD", "STRING"),
        IntrospectedColumn("RECEIVED_AT", "STRING"),
        IntrospectedColumn("COUNTRY_CODE", "STRING"),
    ]


@pytest.fixture
def contract_file(tmp_path) -> Path:
    p = tmp_path / "contract.yml"
    p.write_text(CONTRACT_YAML)
    return p


@pytest.fixture
def contract(contract_file) -> EventContract:
    return EventContract.from_file(contract_file)


@pytest.fixture
def snapshot_contract() -> EventContract:
    return EventContract.from_text(SNAPSHOT_CONTRACT_YAML)


@pytest.fixture
def classified_contract() -> EventContract:
    return EventContract.from_text(CLASSIFIED_CONTRACT_YAML)


@pytest.fixture
def introspector() -> FakeIntrospector:
    return FakeIntrospector(
        {
            (SOURCE_PROJECT, SOURCE_DATASET, SOURCE_TABLE): _source_columns(),
        }
    )


# ---------------------------------------------------------------------------
# Contract parsing
# ---------------------------------------------------------------------------


class TestContractParsing:
    def test_basic_fields(self, contract):
        assert contract.name == "user_signed_up"
        assert contract.unique_key == ["EVENT_ID"]
        assert contract.loaded_at_field == "RECEIVED_AT"

    def test_unique_key_dbt_string(self, contract):
        assert contract.unique_key_dbt == "'EVENT_ID'"

    def test_source(self, contract):
        assert contract.source.fqn == "my-project.landing.user_signups_raw"

    def test_raw_config(self, contract):
        assert contract.raw.dataset == "raw"
        assert contract.raw.dedup is True
        assert contract.raw.partition_by.field == "RECEIVED_AT"
        assert contract.raw.cluster_by == ["USER_ID"]

    def test_staging_columns_parsed(self, contract):
        assert len(contract.staging.columns) == 4
        event_id = contract.staging.columns[0]
        assert event_id.source == "EVENT_ID"
        assert event_id.name == "event_id"
        assert event_id.type == "STRING"
        assert event_id.needs_cast and event_id.needs_rename

    def test_default_staging_name(self):
        col = StagingColumn(source="USER_ID")
        assert col.name == "user_id"

    def test_accepted_values_test(self, contract):
        method_col = next(
            c for c in contract.staging.columns if c.name == "signup_method"
        )
        av = next(t for t in method_col.tests if t.name == "accepted_values")
        assert av.args["values"] == ["email", "google", "apple"]


class TestContractValidation:
    def test_missing_server_raises(self, tmp_path):
        # Valid ODCS skeleton but no bigquery server to source from.
        yaml_text = CONTRACT_YAML.replace(
            "servers:\n"
            "  - server: landing\n"
            "    type: bigquery\n"
            "    project: my-project\n"
            "    dataset: landing\n",
            "",
        )
        p = tmp_path / "c.yml"
        p.write_text(yaml_text)
        with pytest.raises(ContractError, match="BigQuery server"):
            EventContract.from_file(p)

    def test_unsupported_api_version(self, tmp_path):
        p = tmp_path / "c.yml"
        p.write_text(
            CONTRACT_YAML.replace("apiVersion: v3.0.2", "apiVersion: v9.9.9")
        )
        with pytest.raises(ContractError, match="not valid ODCS"):
            EventContract.from_file(p)

    def test_missing_schema_block(self, tmp_path):
        p = tmp_path / "c.yml"
        p.write_text(
            'version: "1.0.0"\nkind: DataContract\napiVersion: v3.0.2\n'
            "id: x\nstatus: active\n"
        )
        with pytest.raises(ContractError, match="one schema object"):
            EventContract.from_file(p)

    def test_not_a_data_contract(self, tmp_path):
        p = tmp_path / "c.yml"
        p.write_text(
            CONTRACT_YAML.replace("kind: DataContract", "kind: DataProduct")
        )
        with pytest.raises(ContractError):
            EventContract.from_file(p)

    def test_malformed_yaml(self, tmp_path):
        p = tmp_path / "c.yml"
        p.write_text("not: : valid: yaml: :")
        with pytest.raises(ContractError, match="Failed to parse YAML"):
            EventContract.from_file(p)

    def test_malformed_exemption_list(self, tmp_path):
        p = tmp_path / "c.yml"
        p.write_text(
            CONTRACT_YAML.replace(
                "      - property: modelgenLoadedAtField\n"
                "        value: RECEIVED_AT\n",
                "      - property: modelgenLoadedAtField\n"
                "        value: RECEIVED_AT\n"
                "      - property: modelgenExempt\n"
                "        value:\n"
                "          - just a string\n",
            )
        )
        with pytest.raises(ContractError, match="modelgenExempt"):
            EventContract.from_file(p)


class TestCompositeKeys:
    def test_composite_unique_key(self, tmp_path):
        # Mark USER_ID as the second primary-key column.
        yaml_text = CONTRACT_YAML.replace(
            "      - name: user_id\n"
            "        physicalName: USER_ID\n"
            "        logicalType: string\n"
            "        physicalType: STRING\n"
            "        required: true\n",
            "      - name: user_id\n"
            "        physicalName: USER_ID\n"
            "        logicalType: string\n"
            "        physicalType: STRING\n"
            "        required: true\n"
            "        primaryKey: true\n"
            "        primaryKeyPosition: 2\n",
        )
        p = tmp_path / "c.yml"
        p.write_text(yaml_text)
        c = EventContract.from_file(p)
        assert c.unique_key == ["EVENT_ID", "USER_ID"]
        assert c.unique_key_dbt == "['EVENT_ID', 'USER_ID']"


# ---------------------------------------------------------------------------
# Raw layer generation
# ---------------------------------------------------------------------------


class TestRawGeneration:
    def test_files_produced(self, contract, introspector, tmp_path):
        result = generate(contract, introspector, tmp_path, Layer.RAW)
        assert len(result.written) == 2
        names = {p.name for p in result.written}
        assert names == {"raw__user_signed_up.sql", "raw__user_signed_up.yml"}

    def test_sql_uses_source(self, contract, introspector, tmp_path):
        generate(contract, introspector, tmp_path, Layer.RAW)
        sql = (tmp_path / "raw__user_signed_up.sql").read_text()
        assert "source('landing', 'user_signups_raw')" in sql

    def test_sql_uses_explicit_columns(self, contract, introspector, tmp_path):
        generate(contract, introspector, tmp_path, Layer.RAW)
        sql = (tmp_path / "raw__user_signed_up.sql").read_text()
        assert "select *" not in sql
        assert "EVENT_ID" in sql
        assert "USER_ID" in sql
        assert "cast(" not in sql

    def test_sql_has_dedup_block(self, contract, introspector, tmp_path):
        generate(contract, introspector, tmp_path, Layer.RAW)
        sql = (tmp_path / "raw__user_signed_up.sql").read_text()
        assert "row_number()" in sql
        assert "partition by EVENT_ID" in sql
        assert "_modelgen_rn = 1" in sql

    def test_sql_has_partition_by(self, contract, introspector, tmp_path):
        generate(contract, introspector, tmp_path, Layer.RAW)
        sql = (tmp_path / "raw__user_signed_up.sql").read_text()
        assert "partition_by=" in sql
        assert "'field': 'RECEIVED_AT'" in sql
        assert "'granularity': 'day'" in sql

    def test_sql_has_cluster_by(self, contract, introspector, tmp_path):
        generate(contract, introspector, tmp_path, Layer.RAW)
        sql = (tmp_path / "raw__user_signed_up.sql").read_text()
        assert "cluster_by='USER_ID'" in sql

    def test_sql_incremental_filter(self, contract, introspector, tmp_path):
        generate(contract, introspector, tmp_path, Layer.RAW)
        sql = (tmp_path / "raw__user_signed_up.sql").read_text()
        assert "is_incremental()" in sql

    def test_yml_lists_all_source_columns(
        self, contract, introspector, tmp_path
    ):
        generate(contract, introspector, tmp_path, Layer.RAW)
        parsed = yaml.safe_load(
            (tmp_path / "raw__user_signed_up.yml").read_text()
        )
        source_cols = parsed["sources"][0]["tables"][0]["columns"]
        names = {c["name"] for c in source_cols}
        assert names == {
            "EVENT_ID",
            "USER_ID",
            "SIGNUP_METHOD",
            "RECEIVED_AT",
            "COUNTRY_CODE",
        }

    def test_yml_is_valid_dbt_yaml(self, contract, introspector, tmp_path):
        generate(contract, introspector, tmp_path, Layer.RAW)
        parsed = yaml.safe_load(
            (tmp_path / "raw__user_signed_up.yml").read_text()
        )
        assert parsed["version"] == 2
        assert parsed["models"][0]["name"] == "raw__user_signed_up"
        assert parsed["models"][0]["config"]["schema"] == "raw"

    def test_yml_emits_source_freshness(
        self, contract, introspector, tmp_path
    ):
        generate(contract, introspector, tmp_path, Layer.RAW)
        parsed = yaml.safe_load(
            (tmp_path / "raw__user_signed_up.yml").read_text()
        )
        table = parsed["sources"][0]["tables"][0]
        assert table["freshness"] == {
            "warn_after": {"count": 12, "period": "hour"},
            "error_after": {"count": 24, "period": "hour"},
        }

    def test_yml_omits_freshness_when_undeclared(
        self, contract, introspector, tmp_path
    ):
        contract.raw.freshness = None
        generate(contract, introspector, tmp_path, Layer.RAW)
        text = (tmp_path / "raw__user_signed_up.yml").read_text()
        parsed = yaml.safe_load(text)
        assert "freshness" not in text
        assert "loaded_at_field" in parsed["sources"][0]["tables"][0]

    def test_yml_omits_freshness_when_only_one_bound(
        self, contract, introspector, tmp_path
    ):
        contract.raw.freshness = FreshnessConfig(
            warn_after=FreshnessThreshold(6, "hour")
        )
        generate(contract, introspector, tmp_path, Layer.RAW)
        parsed = yaml.safe_load(
            (tmp_path / "raw__user_signed_up.yml").read_text()
        )
        table = parsed["sources"][0]["tables"][0]
        assert table["freshness"] == {
            "warn_after": {"count": 6, "period": "hour"}
        }

    def test_empty_freshness_block_parses_as_undeclared(self):
        text = CONTRACT_YAML.replace(
            "          freshness:\n"
            "            warnAfter: {count: 12, period: hour}\n"
            "            errorAfter: {count: 24, period: hour}\n",
            "          freshness: {}\n",
        )
        assert EventContract.from_text(text).raw.freshness is None


class TestRawWithoutDedup:
    def test_no_dedup_means_no_window(
        self, contract_file, introspector, tmp_path
    ):
        yaml_text = contract_file.read_text().replace(
            "dedup: true", "dedup: false"
        )
        contract_file.write_text(yaml_text)
        contract = EventContract.from_file(contract_file)

        generate(contract, introspector, tmp_path, Layer.RAW)
        sql = (tmp_path / "raw__user_signed_up.sql").read_text()
        assert "row_number()" not in sql
        assert "select *" not in sql
        assert "EVENT_ID" in sql


# ---------------------------------------------------------------------------
# Staging layer generation
# ---------------------------------------------------------------------------


class TestStagingGeneration:
    def test_files_produced(self, contract, introspector, tmp_path):
        result = generate(contract, introspector, tmp_path, Layer.STAGING)
        assert len(result.written) == 2

    def test_sql_refs_raw(self, contract, introspector, tmp_path):
        generate(contract, introspector, tmp_path, Layer.STAGING)
        sql = (tmp_path / "stg__user_signed_up.sql").read_text()
        assert "ref('raw__user_signed_up')" in sql
        assert "source(" not in sql

    def test_sql_applies_casts(self, contract, introspector, tmp_path):
        generate(contract, introspector, tmp_path, Layer.STAGING)
        sql = (tmp_path / "stg__user_signed_up.sql").read_text()
        assert "cast(RECEIVED_AT as TIMESTAMP) as received_at" in sql
        assert "cast(EVENT_ID as STRING) as event_id" in sql

    def test_sql_selects_source_columns_in_cte(
        self, contract, introspector, tmp_path
    ):
        generate(contract, introspector, tmp_path, Layer.STAGING)
        sql = (tmp_path / "stg__user_signed_up.sql").read_text()
        for raw_name in [
            "EVENT_ID",
            "USER_ID",
            "SIGNUP_METHOD",
            "RECEIVED_AT",
        ]:
            assert raw_name in sql

    def test_country_code_not_in_staging(
        self, contract, introspector, tmp_path
    ):
        generate(contract, introspector, tmp_path, Layer.STAGING)
        sql = (tmp_path / "stg__user_signed_up.sql").read_text()
        assert "COUNTRY_CODE" not in sql
        assert "country_code" not in sql

    def test_yml_only_has_contract_columns(
        self, contract, introspector, tmp_path
    ):
        generate(contract, introspector, tmp_path, Layer.STAGING)
        parsed = yaml.safe_load(
            (tmp_path / "stg__user_signed_up.yml").read_text()
        )
        col_names = {c["name"] for c in parsed["models"][0]["columns"]}
        assert col_names == {
            "event_id",
            "user_id",
            "signup_method",
            "received_at",
        }

    def test_yml_includes_tests(self, contract, introspector, tmp_path):
        generate(contract, introspector, tmp_path, Layer.STAGING)
        parsed = yaml.safe_load(
            (tmp_path / "stg__user_signed_up.yml").read_text()
        )
        event_id = next(
            c
            for c in parsed["models"][0]["columns"]
            if c["name"] == "event_id"
        )
        assert "not_null" in event_id["tests"]
        assert "unique" in event_id["tests"]

    def test_yml_accepted_values_test(self, contract, introspector, tmp_path):
        generate(contract, introspector, tmp_path, Layer.STAGING)
        parsed = yaml.safe_load(
            (tmp_path / "stg__user_signed_up.yml").read_text()
        )
        method = next(
            c
            for c in parsed["models"][0]["columns"]
            if c["name"] == "signup_method"
        )
        av_test = next(
            t
            for t in method["tests"]
            if isinstance(t, dict) and "accepted_values" in t
        )
        assert av_test["accepted_values"]["values"] == [
            "email",
            "google",
            "apple",
        ]


# ---------------------------------------------------------------------------
# Schema evolution: the enterprise scenarios
# ---------------------------------------------------------------------------


class TestSnapshotStaging:
    """Staging defaults to a datestamped daily snapshot: one full copy of
    the dimension per ds partition, no incremental high-water mark."""

    def sql(self, contract, introspector, tmp_path) -> str:
        generate(contract, introspector, tmp_path, Layer.STAGING)
        return (tmp_path / "stg__user_signed_up.sql").read_text()

    def yml(self, contract, introspector, tmp_path) -> dict:
        generate(contract, introspector, tmp_path, Layer.STAGING)
        return yaml.safe_load(
            (tmp_path / "stg__user_signed_up.yml").read_text()
        )

    def test_snapshot_is_the_default(self):
        stripped = SNAPSHOT_CONTRACT_YAML.replace(
            "          materialization: snapshot\n", ""
        )
        assert EventContract.from_text(stripped).staging.is_snapshot

    def test_partition_derives_to_ds(self, snapshot_contract):
        pb = snapshot_contract.staging.partition_by
        assert (pb.field, pb.data_type, pb.granularity) == (
            "ds",
            "date",
            "day",
        )

    def test_strategy_derives_to_insert_overwrite(self, snapshot_contract):
        assert (
            snapshot_contract.staging.incremental_strategy
            == "insert_overwrite"
        )

    def test_staging_rejects_dedup(self):
        with pytest.raises(ContractError, match="dedup"):
            EventContract.from_text(
                SNAPSHOT_CONTRACT_YAML.replace(
                    "          materialization: snapshot\n",
                    "          materialization: snapshot\n"
                    "          dedup: true\n",
                )
            )

    def test_sql_stamps_ds_from_var(
        self, snapshot_contract, introspector, tmp_path
    ):
        sql = self.sql(snapshot_contract, introspector, tmp_path)
        assert (
            'date(\'{{ var("ds",'
            ' run_started_at.strftime("%Y-%m-%d")) }}\') as ds' in sql
        )

    def test_sql_config_partitions_on_ds(
        self, snapshot_contract, introspector, tmp_path
    ):
        sql = self.sql(snapshot_contract, introspector, tmp_path)
        assert "incremental_strategy='insert_overwrite'" in sql
        assert "'field': 'ds', 'data_type': 'date'" in sql

    def test_sql_omits_unique_key(
        self, snapshot_contract, introspector, tmp_path
    ):
        # insert_overwrite replaces whole partitions and never reads it.
        assert "unique_key" not in self.sql(
            snapshot_contract, introspector, tmp_path
        )

    def test_sql_has_no_incremental_filter(
        self, snapshot_contract, introspector, tmp_path
    ):
        sql = self.sql(snapshot_contract, introspector, tmp_path)
        assert "is_incremental()" not in sql

    def test_yml_declares_ds_column(
        self, snapshot_contract, introspector, tmp_path
    ):
        parsed = self.yml(snapshot_contract, introspector, tmp_path)
        ds = parsed["models"][0]["columns"][-1]
        assert ds["name"] == "ds"
        assert ds["data_type"] == "DATE"
        assert "not_null" in ds["tests"]

    def test_yml_scopes_unique_test_to_one_partition(
        self, snapshot_contract, introspector, tmp_path
    ):
        # The grain is (key, ds), so an unscoped unique test would fail as
        # soon as a second partition lands.
        parsed = self.yml(snapshot_contract, introspector, tmp_path)
        event_id = parsed["models"][0]["columns"][0]
        unique = next(
            t["unique"] for t in event_id["tests"] if isinstance(t, dict)
        )
        assert unique["config"]["where"].startswith("ds = date(")

    def test_incremental_mode_keeps_high_water_mark(
        self, contract, introspector, tmp_path
    ):
        generate(contract, introspector, tmp_path, Layer.STAGING)
        sql = (tmp_path / "stg__user_signed_up.sql").read_text()
        assert "is_incremental()" in sql
        assert "unique_key='event_id'" in sql
        assert " as ds" not in sql


class TestClassifiedStaging:
    """Classification drives encryption at generation time. A classified
    column is never materialised in plaintext."""

    def sql(self, contract, introspector, tmp_path) -> str:
        generate(contract, introspector, tmp_path, Layer.STAGING)
        return (tmp_path / "stg__user_signed_up.sql").read_text()

    def yml_col(self, contract, introspector, tmp_path, name) -> dict:
        generate(contract, introspector, tmp_path, Layer.STAGING)
        parsed = yaml.safe_load(
            (tmp_path / "stg__user_signed_up.yml").read_text()
        )
        return next(
            c for c in parsed["models"][0]["columns"] if c["name"] == name
        )

    def test_classification_parsed(self, classified_contract):
        by_name = {c.name: c for c in classified_contract.staging.columns}
        assert by_name["user_id"].classification == "pii"
        assert by_name["received_at"].classification == "psi"
        assert by_name["event_id"].classification == ""
        assert classified_contract.subject_field == "USER_ID"
        assert classified_contract.encrypts

    def test_subject_is_deterministic(
        self, classified_contract, introspector, tmp_path
    ):
        # Stable ciphertext per keyset is what lets joins, counts and
        # aggregates survive the crypto-shred.
        sql = self.sql(classified_contract, introspector, tmp_path)
        assert "deterministic_encrypt(" in sql
        assert "wrapped_dek_deterministic" in sql

    def test_payload_is_randomised(
        self, classified_contract, introspector, tmp_path
    ):
        sql = self.sql(classified_contract, introspector, tmp_path)
        assert "aead.encrypt(" in sql
        assert "wrapped_dek_randomised" in sql

    def test_no_classified_column_emitted_in_plaintext(
        self, classified_contract, introspector, tmp_path
    ):
        sql = self.sql(classified_contract, introspector, tmp_path)
        # The plaintext source names appear only inside an encrypt call or
        # the source CTE — never as a bare projection into the model.
        assert "cast(USER_ID as STRING) as user_id" not in sql
        assert "cast(RECEIVED_AT as TIMESTAMP) as received_at" not in sql

    def test_unclassified_columns_untouched(
        self, classified_contract, introspector, tmp_path
    ):
        sql = self.sql(classified_contract, introspector, tmp_path)
        assert "cast(EVENT_ID as STRING) as event_id" in sql

    def test_keystore_joined_on_the_subject(
        self, classified_contract, introspector, tmp_path
    ):
        sql = self.sql(classified_contract, introspector, tmp_path)
        assert "source('keystore', 'subject_keys')" in sql
        assert "on keystore.subject_id = source.USER_ID" in sql
        assert "from keyed" in sql

    def test_keystore_join_is_left(
        self, classified_contract, introspector, tmp_path
    ):
        # An inner join would silently drop every row whose subject has no
        # keyset. The row must survive so not_null can surface it.
        sql = self.sql(classified_contract, introspector, tmp_path)
        assert "left join" in sql

    def test_aad_bound_to_the_subject(
        self, classified_contract, introspector, tmp_path
    ):
        sql = self.sql(classified_contract, introspector, tmp_path)
        assert "cast(keystore.subject_id as bytes) as _modelgen_aad" in sql
        assert sql.count("_modelgen_aad") == 3  # declared + two encrypts

    def test_plaintext_cast_not_doubled(
        self, classified_contract, introspector, tmp_path
    ):
        sql = self.sql(classified_contract, introspector, tmp_path)
        assert "cast(cast(USER_ID as STRING) as string)" not in sql
        assert "cast(USER_ID as STRING)" in sql

    def test_non_string_type_stringified_for_aead(
        self, classified_contract, introspector, tmp_path
    ):
        # AEAD takes STRING or BYTES, so a TIMESTAMP is cast twice: once to
        # honour the contract type, once to hand AEAD something it accepts.
        sql = self.sql(classified_contract, introspector, tmp_path)
        assert "cast(cast(RECEIVED_AT as TIMESTAMP) as string)" in sql

    def test_yml_declares_ciphertext_as_bytes(
        self, classified_contract, introspector, tmp_path
    ):
        col = self.yml_col(
            classified_contract, introspector, tmp_path, "user_id"
        )
        assert col["data_type"] == "BYTES"
        assert col["meta"] == {"classification": "pii", "encrypted": True}

    def test_yml_leaves_unclassified_type_alone(
        self, classified_contract, introspector, tmp_path
    ):
        col = self.yml_col(
            classified_contract, introspector, tmp_path, "event_id"
        )
        assert col["data_type"] == "STRING"
        assert "meta" not in col

    def test_unclassified_contract_has_no_keystore_join(
        self, snapshot_contract, introspector, tmp_path
    ):
        sql = self.sql(snapshot_contract, introspector, tmp_path)
        assert "keystore" not in sql
        assert "encrypt" not in sql
        assert "from source" in sql


class TestSchemaEvolution:
    def test_new_source_column_flows_through_raw(self, contract, tmp_path):
        """A new column in source appears in raw after regeneration."""
        introspector = FakeIntrospector(
            {
                (
                    SOURCE_PROJECT,
                    SOURCE_DATASET,
                    SOURCE_TABLE,
                ): _source_columns()
                + [
                    IntrospectedColumn("DEVICE_TYPE", "STRING"),
                ],
            }
        )
        generate(contract, introspector, tmp_path, Layer.RAW)
        parsed = yaml.safe_load(
            (tmp_path / "raw__user_signed_up.yml").read_text()
        )
        raw_cols = {c["name"] for c in parsed["models"][0]["columns"]}
        assert "DEVICE_TYPE" in raw_cols

    def test_new_source_column_does_not_break_staging(
        self, contract, tmp_path
    ):
        """New columns must not appear in staging (silent invisibility) and
        the user gets a structured notification."""
        introspector = FakeIntrospector(
            {
                (
                    SOURCE_PROJECT,
                    SOURCE_DATASET,
                    SOURCE_TABLE,
                ): _source_columns()
                + [
                    IntrospectedColumn("DEVICE_TYPE", "STRING"),
                ],
            }
        )
        result = generate(contract, introspector, tmp_path, Layer.STAGING)
        sql = (tmp_path / "stg__user_signed_up.sql").read_text()
        assert "DEVICE_TYPE" not in sql
        assert "DEVICE_TYPE" in result.new_source_columns
        # COUNTRY_CODE was also in source but not in contract; also listed
        assert "COUNTRY_CODE" in result.new_source_columns

    def test_dropped_source_column_fails_loudly(self, contract, tmp_path):
        """If source drops a column staging references, fail. Not silently."""
        cols = [c for c in _source_columns() if c.name != "SIGNUP_METHOD"]
        introspector = FakeIntrospector(
            {
                (SOURCE_PROJECT, SOURCE_DATASET, SOURCE_TABLE): cols,
            }
        )
        with pytest.raises(GenerationError, match="SIGNUP_METHOD"):
            generate(contract, introspector, tmp_path, Layer.STAGING)

    def test_renamed_source_column_requires_contract_update(
        self, contract_file, tmp_path
    ):
        """Source renamed EVENT_ID to EVENT_GUID. The contract is updated by
        changing the property's physicalName; the staging name (and the
        unique_key, derived from the primaryKey property) follow."""
        # Step 1: update contract to reflect rename
        yaml_text = contract_file.read_text()
        yaml_text = yaml_text.replace(
            "physicalName: EVENT_ID", "physicalName: EVENT_GUID"
        )
        contract_file.write_text(yaml_text)
        contract = EventContract.from_file(contract_file)

        # Step 2: source now has EVENT_GUID instead of EVENT_ID
        introspector = FakeIntrospector(
            {
                (SOURCE_PROJECT, SOURCE_DATASET, SOURCE_TABLE): [
                    IntrospectedColumn("EVENT_GUID", "STRING"),
                    IntrospectedColumn("USER_ID", "STRING"),
                    IntrospectedColumn("SIGNUP_METHOD", "STRING"),
                    IntrospectedColumn("RECEIVED_AT", "STRING"),
                ],
            }
        )

        # Generation succeeds; staging name (event_id) stays stable downstream
        generate(contract, introspector, tmp_path, Layer.STAGING)
        sql = (tmp_path / "stg__user_signed_up.sql").read_text()
        assert "EVENT_GUID" in sql
        assert "as event_id" in sql  # staging name unchanged

    def test_retyped_source_column_handled_by_cast(
        self, contract, introspector, tmp_path
    ):
        """Source type changes don't break staging because staging casts."""
        # RECEIVED_AT is STRING in source, but staging casts it to TIMESTAMP.
        # If source later changes RECEIVED_AT to INT64, the cast still works
        # (BQ will fail at query time if values are incompatible — correct).
        generate(contract, introspector, tmp_path, Layer.STAGING)
        sql = (tmp_path / "stg__user_signed_up.sql").read_text()
        assert "cast(RECEIVED_AT as TIMESTAMP) as received_at" in sql


class TestKeyValidation:
    def test_missing_unique_key_in_source_fails(self, contract, tmp_path):
        introspector = FakeIntrospector(
            {
                (SOURCE_PROJECT, SOURCE_DATASET, SOURCE_TABLE): [
                    IntrospectedColumn("USER_ID", "STRING"),
                    IntrospectedColumn("RECEIVED_AT", "STRING"),
                    IntrospectedColumn("SIGNUP_METHOD", "STRING"),
                    # EVENT_ID missing
                ],
            }
        )
        with pytest.raises(GenerationError, match="EVENT_ID"):
            generate(contract, introspector, tmp_path, Layer.RAW)

    def test_missing_loaded_at_field_fails(self, contract, tmp_path):
        introspector = FakeIntrospector(
            {
                (SOURCE_PROJECT, SOURCE_DATASET, SOURCE_TABLE): [
                    IntrospectedColumn("EVENT_ID", "STRING"),
                    IntrospectedColumn("USER_ID", "STRING"),
                    IntrospectedColumn("SIGNUP_METHOD", "STRING"),
                    # RECEIVED_AT missing
                ],
            }
        )
        with pytest.raises(GenerationError, match="RECEIVED_AT"):
            generate(contract, introspector, tmp_path, Layer.RAW)


# ---------------------------------------------------------------------------
# BQ type rendering
# ---------------------------------------------------------------------------


class TestBigQueryTypes:
    def test_repeated_renders_as_array(self):
        col = IntrospectedColumn("tags", "STRING", mode="REPEATED")
        assert col.dbt_data_type == "ARRAY<STRING>"

    def test_record_renders_as_struct(self):
        col = IntrospectedColumn("metadata", "RECORD")
        assert col.dbt_data_type == "STRUCT"

    def test_repeated_record_renders_as_array_struct(self):
        col = IntrospectedColumn("items", "RECORD", mode="REPEATED")
        assert col.dbt_data_type == "ARRAY<STRUCT>"

    def test_scalar_unchanged(self):
        assert IntrospectedColumn("x", "INT64").dbt_data_type == "INT64"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_two_runs_produce_identical_output(
        self, contract, introspector, tmp_path
    ):
        out1 = tmp_path / "run1"
        out2 = tmp_path / "run2"
        generate(contract, introspector, out1, Layer.RAW)
        generate(contract, introspector, out2, Layer.RAW)
        assert (out1 / "raw__user_signed_up.sql").read_text() == (
            out2 / "raw__user_signed_up.sql"
        ).read_text()
        assert (out1 / "raw__user_signed_up.yml").read_text() == (
            out2 / "raw__user_signed_up.yml"
        ).read_text()


# ---------------------------------------------------------------------------
# ModelgenConfig
# ---------------------------------------------------------------------------


class TestModelgenConfig:
    def test_defaults_when_no_file(self, tmp_path):
        cfg = ModelgenConfig.discover(tmp_path)
        assert cfg.contracts_glob == "contracts/*.yml"
        assert cfg.raw_output == tmp_path / "models/raw"
        assert cfg.staging_output == tmp_path / "models/staging"
        assert cfg.root == tmp_path

    def test_from_file_reads_values(self, tmp_path):
        f = tmp_path / ".modelgen.yml"
        f.write_text(
            "contracts: schemas/*.yml\n"
            "raw_output: out/raw\n"
            "staging_output: out/staging\n"
        )
        cfg = ModelgenConfig.from_file(f)
        assert cfg.contracts_glob == "schemas/*.yml"
        assert cfg.raw_output == tmp_path / "out/raw"
        assert cfg.staging_output == tmp_path / "out/staging"

    def test_discover_finds_config_in_parent(self, tmp_path):
        (tmp_path / ".modelgen.yml").write_text("contracts: contracts/*.yml\n")
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        cfg = ModelgenConfig.discover(subdir)
        assert cfg.root == tmp_path

    def test_contract_files_returns_sorted(self, tmp_path):
        contracts = tmp_path / "contracts"
        contracts.mkdir()
        (contracts / "b.yml").write_text("")
        (contracts / "a.yml").write_text("")
        cfg = ModelgenConfig._build(tmp_path, {})
        assert [f.name for f in cfg.contract_files()] == ["a.yml", "b.yml"]

    def test_malformed_config_raises(self, tmp_path):
        f = tmp_path / ".modelgen.yml"
        f.write_text("not: : valid: yaml: :")
        with pytest.raises(ConfigError):
            ModelgenConfig.from_file(f)

    def test_partial_overrides_keep_defaults(self, tmp_path):
        f = tmp_path / ".modelgen.yml"
        f.write_text("raw_output: custom/raw\n")
        cfg = ModelgenConfig.from_file(f)
        assert cfg.contracts_glob == "contracts/*.yml"
        assert cfg.raw_output == tmp_path / "custom/raw"
        assert cfg.staging_output == tmp_path / "models/staging"


class TestContractIntrospector:
    def test_returns_staging_columns(self, contract):
        intr = ContractIntrospector(contract)
        cols = intr.get_columns(SOURCE_PROJECT, SOURCE_DATASET, SOURCE_TABLE)
        names = {c.name for c in cols}
        assert {
            "EVENT_ID",
            "USER_ID",
            "SIGNUP_METHOD",
            "RECEIVED_AT",
        }.issubset(names)

    def test_unique_key_always_present(self, contract):
        intr = ContractIntrospector(contract)
        cols = intr.get_columns(SOURCE_PROJECT, SOURCE_DATASET, SOURCE_TABLE)
        names = {c.name for c in cols}
        for k in contract.unique_key:
            assert k in names

    def test_loaded_at_always_present(self, contract):
        intr = ContractIntrospector(contract)
        cols = intr.get_columns(SOURCE_PROJECT, SOURCE_DATASET, SOURCE_TABLE)
        names = {c.name for c in cols}
        assert contract.loaded_at_field in names

    def test_type_comes_from_staging(self, contract):
        intr = ContractIntrospector(contract)
        cols = intr.get_columns(SOURCE_PROJECT, SOURCE_DATASET, SOURCE_TABLE)
        event_id = next(c for c in cols if c.name == "EVENT_ID")
        assert event_id.field_type == "STRING"

    def test_raw_generation_with_contract_introspector(
        self, contract, tmp_path
    ):
        intr = ContractIntrospector(contract)
        result = generate(contract, intr, tmp_path, Layer.RAW)
        assert len(result.written) == 2
        sql = (tmp_path / "raw__user_signed_up.sql").read_text()
        assert "select *" not in sql
        assert "EVENT_ID" in sql

    def test_staging_generation_with_contract_introspector(
        self, contract, tmp_path
    ):
        intr = ContractIntrospector(contract)
        result = generate(contract, intr, tmp_path, Layer.STAGING)
        assert len(result.written) == 2
        sql = (tmp_path / "stg__user_signed_up.sql").read_text()
        assert "cast(EVENT_ID as STRING) as event_id" in sql


class TestDryRun:
    def test_dry_run_writes_nothing(self, contract, introspector, tmp_path):
        result = generate(
            contract, introspector, tmp_path, Layer.RAW, dry_run=True
        )
        assert len(result.written) == 2
        for p in result.written:
            assert not p.exists()


class TestErrors:
    def test_missing_table_raises_introspection_error(
        self, contract, tmp_path
    ):
        empty = FakeIntrospector({})
        with pytest.raises(IntrospectionError):
            generate(contract, empty, tmp_path, Layer.RAW)

    def test_empty_staging_contract_fails(self, tmp_path, introspector):
        from modelgen.contract import RawConfig, SourceRef, StagingConfig

        contract = EventContract(
            name="empty_event",
            description="",
            unique_key=["EVENT_ID"],
            loaded_at_field="RECEIVED_AT",
            source=SourceRef("my-project", "landing", "user_signups_raw"),
            raw=RawConfig(),
            staging=StagingConfig(columns=[]),
        )
        with pytest.raises(GenerationError, match="no columns"):
            generate(contract, introspector, tmp_path, Layer.STAGING)
