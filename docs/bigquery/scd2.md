# 👻 SCD Type 2

**Script:** `models/utils/bq_stg_scd2.sql`

## Read This First: `ds` Is the Default

Staging materialises as a **datestamped daily snapshot**. One full copy of
the dimension per `ds` partition:

```
ds          account_id  status
2024-06-19  ACC001      open
2024-06-20  ACC001      closed
2024-06-21  ACC001      closed
```

Historical query is `WHERE ds = '2024-06-20'`. No surrogate keys, no
`BETWEEN` join that fails silently when someone forgets one half of it, and
backfills run in parallel because day N does not depend on day N-1.

SCD2 was a compression scheme for expensive storage. Storage is cheap;
engineering time is not. **SCD2 is now the carve-out**, built on top of the
`ds` base when a regulated consumer needs an immutable record of who changed
what and when. See [Tooling Decisions](decisions.md).

## What SCD2 Is

Slowly Changing Dimension Type 2. One row per entity per state period.
History is preserved by appending a new row and closing the previous one
with a `valid_to` timestamp.

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

## `utils.rebuild_scd2` — Derive SCD2 From the Snapshots

`CREATE OR REPLACE TABLE ... AS SELECT` with a `LEAD()` window for
`valid_to`. Full rebuild, every run. Always correct, simple to debug, and
cheap because the `ds` base it reads is already partitioned.

There is no incremental MERGE variant. `merge_scd2` was removed in
TJP-27071: it carried the failure modes — late-arriving data, dedup edge
cases, MERGE bugs that only surface on a specific event sequence — and its
own documentation already said not to reach for it until you had a measured
cost problem you almost certainly never had.

```sql
CALL `project.utils.rebuild_scd2`(
    'my-project', 'staging',  'stg__account_events',  -- source (ds base)
    'my-project', 'marts',    'dim_accounts',          -- target
    'account_id_hashed',      -- unique key (PARTITION BY in window)
    'event_ts',               -- event timestamp (ORDER BY in window)
    'event_type',             -- tiebreak for simultaneous events
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
| Multiple current rows | Rebuild ran against a dirty base, or dedup logic failed |
| Period gaps | `valid_to` on row N ≠ `valid_from` on row N+1 |
| Overlapping periods | Two rows whose periods intersect |

## Where SCD2 Sits in the Stack

```
raw.account_events        ← append-only, as received
    ↓  modelgen
staging.stg__account_events ← ds snapshot, one full copy per day
    ↓  rebuild_scd2         (regulated carve-out only)
marts.dim_accounts        ← SCD2 dimension
```

The `ds` snapshot is the base every consumer reads. SCD2 is a derived
convenience on top of it, not a layer everything must pass through.

## SCD2 is OLAP, Not OLTP

The rebuild runs once per daily batch. Not thousands of times per second.
BQ handles it. See [Architecture](architecture.md) for the OLTP vs OLAP
distinction.

## Related

- [Architecture](architecture.md) — OLTP vs OLAP, why SCD2 belongs in BQ
- [Tooling Decisions](decisions.md) — why `ds` replaced SCD2 as the default
- `models/utils/bq_struct_contract.sql` — contract validation after rebuild
- `models/utils/bq_stg_scd2.sql` — procedure implementations
