# 👻 SCD Type 2

**Script:** `models/utils/bq_stg_scd2.sql`

## What SCD2 Is

Slowly Changing Dimension Type 2. One row per entity per state period. History is preserved by appending a new row and closing the previous one with a `valid_to` timestamp.

```
account_id  status  valid_from           valid_to             is_current
ACC001      open    2024-01-15 09:00:00  2024-06-20 14:30:00  FALSE
ACC001      closed  2024-06-20 14:30:00  2024-09-01 11:00:00  FALSE
ACC001      open    2024-09-01 11:00:00  NULL                 TRUE
```

Point-in-time query:
```sql
WHERE valid_from <= TIMESTAMP('2024-08-01')
  AND (valid_to > TIMESTAMP('2024-08-01') OR valid_to IS NULL)
```

## SCD2 is OLAP, Not OLTP

The MERGE that closes the previous record runs once per daily batch. Not thousands of times per second. BQ handles it. See [Architecture](architecture.md) for the OLTP vs OLAP distinction.

## Two Procedures

### Pattern A: `utils.rebuild_scd2` — Full Rebuild

`CREATE OR REPLACE TABLE ... AS SELECT` with `LEAD()` window for `valid_to`.

**When to use:** default. Always correct. Simple to debug. Expensive only when table is very large (hundreds of millions of rows across years of history).

```sql
CALL `project.utils.rebuild_scd2`(
    'my-project', 'staging',  'stg_account_events',  -- source
    'my-project', 'marts',    'dim_accounts',          -- target
    'account_id_hashed',      -- unique key (PARTITION BY in window)
    'event_ts',               -- event timestamp (ORDER BY in window)
    'valid_from',             -- partition column on target table
    'account_id_hashed',      -- cluster column on target table
    '''                       -- select_cols: caller-owned projection
        account_id_hashed,
        user_id_hashed,
        account_type,
        CASE
            WHEN event_type IN (''created'', ''reopened'') THEN ''open''
            WHEN event_type = ''closed''                   THEN ''closed''
        END  AS status,
        event_ts AS valid_from
    ''',
    'SCD Type 2 account dimension',
    '("layer","marts"),("scd_type","type-2")'
);
```

**How it works:**
1. `source_events` CTE: applies `select_cols` projection to source table
2. `scd2` CTE: `LEAD(event_ts)` over `PARTITION BY unique_key ORDER BY event_ts` → `valid_to`
3. `QUALIFY ROW_NUMBER() = 1` deduplicates simultaneous events using caller-supplied `tiebreak_col` (pass `event_type` for alphabetical ordering — arbitrary but stable)
4. `CREATE OR REPLACE TABLE` atomically replaces target

### Pattern B: `utils.merge_scd2` — Incremental MERGE

Two-step: close previous open record, insert new current record.

**When to use:** when Pattern A full-scan cost is measurably unacceptable. Don't switch until you have a cost problem.

```sql
CALL `project.utils.merge_scd2`(
    'my-project', 'staging',  'stg_account_events',
    'my-project', 'marts',    'dim_accounts',
    'account_id_hashed',      -- unique key
    'event_ts',               -- event timestamp
    'event_ts',               -- incremental filter column
    DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY),  -- process yesterday's events
    '''
        account_id_hashed,
        user_id_hashed,
        account_type,
        CASE
            WHEN event_type IN (''created'', ''reopened'') THEN ''open''
            WHEN event_type = ''closed''                   THEN ''closed''
        END  AS status,
        event_ts AS valid_from
    '''
);
```

**How it works:**
1. MERGE: finds entities with new events, sets `valid_to = new_event_ts`, `is_current = FALSE` on their current row
2. INSERT: adds new row per arriving event, `valid_to = NULL`, `is_current = TRUE`, `record_version = prev + 1`

**Caveat:** MERGE on BQ is not cheap. Full rebuild is often faster than MERGE on partitioned tables if the partition is well-defined. Measure before committing.

## Validation: `utils.validate_scd2`

Three checks. Zero rows returned = clean.

```sql
CALL `project.utils.validate_scd2`(
    'my-project', 'marts', 'dim_accounts',
    'account_id_hashed',
    'valid_from',
    'valid_to'
);
```

| Check | What it catches |
|---|---|
| Multiple current rows | MERGE ran twice, or dedup logic failed |
| Period gaps | `valid_to` on row N ≠ `valid_from` on row N+1 |
| Overlapping periods | Two rows whose periods intersect |

## Where SCD2 Sits in the Stack

```
raw.account_events        ← append-only, no SCD2
    ↓  rebuild_scd2
staging.stg_account_scd2 ← SCD2 built here
    ↓  passthrough
marts.dim_accounts        ← final SCD2 dimension
```

SCD2 is defined in staging. Marts consume it. This keeps the mart layer thin — it doesn't need to know how the SCD2 was built.

## Related

- [Architecture](architecture.md) — OLTP vs OLAP, why SCD2 belongs in BQ
- `models/utils/bq_struct_contract.sql` — contract validation after rebuild
- `models/utils/bq_stg_scd2.sql` — procedure implementations
