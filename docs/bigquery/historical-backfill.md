# 👻 Historical Backfill

**Script:** `models/utils/bq_historical_backfill.sql`

## When to Use

One-off operation. Runs once after DDL bootstrap to load all historical records before the daily incremental pipeline takes over.

```
Table Creation       → tables exist, zero rows
THIS                 → all history loaded up to cutoff_date
Daily pipeline       → owns cutoff_date onward
```

Never run again unless the pipeline has been rebuilt from scratch. The idempotency guard prevents double-load but the intent is single execution.

## Procedure: `utils.historical_backfill`

```sql
CALL `project.utils.historical_backfill`(
    source_project  STRING,   -- source GCP project
    source_dataset  STRING,   -- source dataset
    source_table    STRING,   -- source table name
    target_project  STRING,   -- target GCP project
    target_dataset  STRING,   -- target dataset
    target_table    STRING,   -- target table name
    select_cols     STRING,   -- full SELECT column list as SQL string
    ts_column       STRING,   -- timestamp column used for cutoff filter
    cutoff_date     DATE,     -- backfill owns records BEFORE this date
    batch_id        STRING,   -- audit identifier, e.g. 'BACKFILL-2026-05-20'
    OUT rows_loaded INT64     -- output: row count inserted
);
```

## Idempotency Guard

Before inserting, the procedure checks whether `batch_id` already exists in the target table. If rows exist for that batch, it raises an error and aborts.

To safely rerun: either use a new `batch_id`, or delete existing rows for the old batch first.

```sql
DELETE FROM `project.raw.account_events`
WHERE _batch_id = 'BACKFILL-2026-05-20';
```

## select_cols Parameter

The SELECT column list is caller-supplied as a SQL string. This gives full control over:
- Column aliasing and ordering
- Type casting
- Computed columns (`GENERATE_UUID()`, `CURRENT_TIMESTAMP()`, `TO_JSON_STRING(...)`)
- Injecting run-level variables (`v_batch_id`, `v_target_dataset`)

The procedure wraps it in:
```sql
INSERT INTO `{target}`
SELECT {select_cols}
FROM `{source}`
WHERE DATE({ts_column}) < DATE({cutoff_date})
```

## Orchestration Script

Declare variables once at the top of the calling script. Pass them into each procedure call.

```sql
DECLARE v_source_project  STRING DEFAULT 'src-project';
DECLARE v_source_dataset  STRING DEFAULT 'source_datawarehouse';
DECLARE v_target_project  STRING DEFAULT 'tgt-project';
DECLARE v_target_dataset  STRING DEFAULT 'raw';
DECLARE v_cutoff_date     DATE   DEFAULT DATE('2026-01-01');
DECLARE v_batch_id        STRING DEFAULT CONCAT('BACKFILL-', CAST(CURRENT_DATE() AS STRING));
DECLARE v_rows_loaded     INT64;

-- Load each source table separately
CALL `project.utils.historical_backfill`(
    v_source_project, v_source_dataset, 'account_created',
    v_target_project, v_target_dataset, 'account_events',
    '...select_cols...',
    'created_ts', v_cutoff_date, v_batch_id, v_rows_loaded
);

-- Verify
CALL `project.utils.verify_backfill`(
    v_target_project, v_target_dataset, 'account_events',
    v_batch_id, 'event_ts'
);
```

## Verification: `utils.verify_backfill`

Runs two checks after load:

1. **Row counts by day**: partition date, row count, earliest/latest timestamp. Spot gaps in coverage.
2. **Overlap check**: any rows with `batch_id = BACKFILL-*` that fall inside the incremental window (should be zero).

```sql
CALL `project.utils.verify_backfill`(
    'my-project', 'raw', 'account_events',
    'BACKFILL-2026-05-20', 'event_ts'
);
```

## Cutoff Date

The cutoff date is the boundary between backfill territory and incremental territory.

- Backfill: `DATE(ts_column) < cutoff_date`
- Incremental pipeline: `DATE(ts_column) >= cutoff_date`

No overlap. Set cutoff_date to the first date your incremental pipeline will process. If unsure, use the date the pipeline will first run in production.

## After Backfill

Next step is rebuilding derived layers from the now-populated raw table:

```
1. Run staging snapshots, one ds per historical date:
     dbt run --select stg__event --vars '{ds: YYYY-MM-DD}'
   Dates are independent, so run them in parallel, in any order.
2. Run SCD2 rebuild    (rebuild_scd2, regulated carve-out only)
3. Run mart models     (full refresh)
4. Verify row counts and date coverage at each layer
5. Hand to daily incremental pipeline
```

Step 1 is where the `ds` pattern earns its keep. An SCD2 staging model had to
be backfilled sequentially, because day N's `valid_to` depends on day N-1.
Snapshots have no such dependency: the same SQL that runs tomorrow runs for
any date you hand it.
