# 👻 Table Creation

**Script:** `models/utils/bq_table_ddl.sql`

Three procedures. Pick based on what you know at creation time.

## Decision Tree

```
Do you have a source table with the right types already?
  YES → Pattern A: create_table_like_source
  NO  → Do you need full control over STRUCT shape / NOT NULL?
          YES → Pattern B: create_table_explicit
          NO  → Promote schema from another managed table?
                  YES → Pattern C: create_table_clone_schema
```

## Pattern A — Schema Inference

`utils.create_table_like_source`

Copies column types from a source table using `AS SELECT * WHERE FALSE` (zero rows loaded). Inherits full schema including STRUCT sub-fields, ARRAY types, NOT NULL constraints, and column ordering.

**Use when:** source already has correct types. Avoids redeclaring STRUCT shape manually.

```sql
CALL `project.utils.create_table_like_source`(
    'src-project',  'raw',      'account_events',    -- source
    'tgt-project',  'staging',  'stg_account_events', -- target
    'event_ts',                                       -- partition column
    'account_id_hashed, event_type',                  -- cluster columns
    'Staged account events',                          -- description
    '("owner","data-engineering"),("layer","staging")'-- labels
);
```

## Pattern B — Explicit Schema

`utils.create_table_explicit`

Caller passes the full column block as a SQL string. Full control over types, STRUCT nesting, NOT NULL constraints.

**Use when:** you need to declare a STRUCT shape that doesn't exist in a source table yet, or when types differ from the source.

```sql
CALL `project.utils.create_table_explicit`(
    'my-project', 'staging', 'stg_account_events',
    '''
        event_id          STRING    NOT NULL,
        event_ts          TIMESTAMP NOT NULL,
        account_id_hashed STRING    NOT NULL,
        enrichment        STRUCT<
            channel       STRING,
            device        STRUCT<
                platform  STRING,
                os_version STRING
            >,
            flags         ARRAY<STRING>,
            attributes    ARRAY<STRUCT<key STRING, value STRING>>
        >,
        _batch_id         STRING    NOT NULL,
        _ingested_at      TIMESTAMP NOT NULL
    ''',
    'event_ts',
    'account_id_hashed, event_type',
    'Staged events with typed STRUCT enrichment',
    '("layer","staging"),("scd_type","type-2")'
);
```

## Pattern C — Clone Schema

`utils.create_table_clone_schema`

Promotes schema from one managed table to another. Uses `AS SELECT * WHERE FALSE` with optional column exclusions/additions.

**Use when:** promoting a staging schema to mart, or creating a parallel table for testing.

```sql
CALL `project.utils.create_table_clone_schema`(
    'my-project', 'staging', 'stg_account_scd2',  -- source
    'my-project', 'marts',   'dim_accounts',        -- target
    'valid_from',
    'account_id_hashed',
    'SCD Type 2 account dimension',
    '("layer","marts"),("scd_type","type-2")'
);
```

## Bootstrap Order

```
1. Terraform apply          → datasets exist (raw, staging, marts, utils)
2. create_table_like_source → table schema declared, zero rows
3. historical_backfill      → history loaded (see Historical Backfill)
4. Daily pipeline           → incremental load takes over
```

## Utility: Describe Dataset

Lists all tables in a dataset with partition column and cluster columns.

```sql
CALL `project.utils.describe_dataset`('my-project', 'staging');
```

## Notes

- All procedures use `CREATE OR REPLACE TABLE` — idempotent, safe to rerun.
- `%T` in FORMAT auto-quotes string parameters — injection-safe for identifiers passed as values.
- Dataset location is set by Terraform, not these procedures. Never set location in table DDL.
- Labels are BQ-level metadata queryable via `INFORMATION_SCHEMA.TABLE_OPTIONS` — use them for cost attribution and governance.
