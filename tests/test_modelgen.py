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
          clusterBy: [USER_ID]
      - property: modelgenStaging
        value:
          dataset: staging
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
