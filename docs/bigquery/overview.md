# 👻 BigQuery Engineering

Technical reference for BigQuery data engineering patterns. Companion to `models/utils/bq_*.sql`.

## Documents

| Doc | What it covers |
|---|---|
| [Architecture](architecture.md) | Stack overview — TF vs dbt, OLTP vs OLAP, layer responsibilities |
| [Table Creation](table-creation.md) | DDL patterns — schema inference, explicit schema, clone |
| [Historical Backfill](historical-backfill.md) | One-off historical load procedure and orchestration |
| [SCD Type 2](scd2.md) | `ds` snapshots as the default; SCD2 as the regulated carve-out |
| [Keystore](keystore.md) | CMEK / KEK / DEK, and crypto-shred erasure for RTBF |
| [Tooling Decisions](decisions.md) | Why `ds` over SCD2, why crypto-shred over delete, why not Liquibase, why not Terraform for tables, why not materialised views |

## Scripts

All SQL lives in `models/utils/`:

```
models/utils/
├── bq_table_ddl.sql           # Stored procedures: create_table_like_source, create_table_explicit, create_table_clone_schema
├── bq_historical_backfill.sql # Stored procedures: historical_backfill, verify_backfill
├── bq_stg_scd2.sql            # Stored procedures: rebuild_scd2, validate_scd2
├── bq_keystore.sql            # Keystore DDL + procedures: issue_keyset, shred_subject, verify_shred
└── bq_struct_contract.sql     # Stored procedures: introspect_schema, validate_data_contract, snapshot_schema, detect_schema_drift
```

## Procedure Registry

| Procedure | Script | Purpose |
|---|---|---|
| `utils.create_table_like_source` | bq_table_ddl.sql | Create table copying schema from source |
| `utils.create_table_explicit` | bq_table_ddl.sql | Create table from caller-supplied schema SQL |
| `utils.create_table_clone_schema` | bq_table_ddl.sql | Clone schema between layers |
| `utils.describe_dataset` | bq_table_ddl.sql | List tables with partition/cluster info |
| `utils.historical_backfill` | bq_historical_backfill.sql | Load historical records with idempotency guard |
| `utils.verify_backfill` | bq_historical_backfill.sql | Post-load row count and overlap checks |
| `utils.rebuild_scd2` | bq_stg_scd2.sql | Full CREATE OR REPLACE SCD2 over the `ds` base |
| `utils.validate_scd2` | bq_stg_scd2.sql | Three SCD2 quality checks |
| `keystore.issue_keyset` | bq_keystore.sql | Mint a subject's wrapped DEKs (idempotent) |
| `keystore.shred_subject` | bq_keystore.sql | Crypto-shred one subject — the one delete |
| `keystore.verify_shred` | bq_keystore.sql | Assert the keyset is gone |
| `utils.introspect_schema` | bq_struct_contract.sql | Full schema including STRUCT sub-fields |
| `utils.validate_data_contract` | bq_struct_contract.sql | Assert field paths and types match contract |
| `utils.snapshot_schema` | bq_struct_contract.sql | Save today's schema to baseline table |
| `utils.detect_schema_drift` | bq_struct_contract.sql | Compare today vs yesterday's schema |
