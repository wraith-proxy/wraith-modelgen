# 👻 Tooling Decisions

Explicit decisions made about tooling, with reasoning. Exists so these don't get relitigated.

## Terraform owns datasets, not tables

**Decision:** Terraform creates and manages BQ datasets. dbt / stored procedures create and manage tables.

**Why:** Dataset location is immutable after creation. Set to the wrong region once and you're dropping everything to fix it. Terraform enforces this as code, reviewed, version-controlled. Tables change constantly as models evolve — Terraform can't track that without perpetual state drift.

**What TF manages:** `google_bigquery_dataset` (location, IAM, default expiry, labels). Nothing else in BQ.

## No Liquibase

**Decision:** Liquibase is not used for BQ schema management.

**Why:** Liquibase solves surgical incremental DDL for OLTP systems where data lives permanently in rows and you cannot drop and rebuild. BQ warehouse pattern rebuilds derived tables from source. The problem Liquibase solves doesn't exist here. `dbt run --full-refresh` is the equivalent operation and it's already in the pipeline.

**If you're reaching for Liquibase:** ask whether the table in question can be rebuilt from source. If yes, use full refresh. If no (persistent state with no source of truth upstream), you may have a design problem.

## No materialised views for metrics

**Decision:** Presentation layer uses tables, not BQ materialised views.

**Why:** BQ materialised views with aggregation cannot include scalar transformations after `GROUP BY`. `SAFE_DIVIDE` — used throughout for active rate calculations — is a scalar transformation. Tables avoid this constraint entirely and give explicit control over partitioning, clustering, and refresh cadence. Materialised view refresh is also non-deterministic under load.

**Cost:** manual orchestration of the refresh. Acceptable at current scale.

## Full rebuild over incremental (until it hurts)

**Decision:** Default to `CREATE OR REPLACE TABLE ... AS SELECT` (full rebuild) for all models. Switch to incremental MERGE only when scan cost is measurably unacceptable.

**Why:** Full rebuild is always correct. Incremental is faster but introduces failure modes — late-arriving data, dedup edge cases, MERGE logic bugs that don't surface until a specific event sequence occurs. The cost of debugging an incremental SCD2 bug in production is higher than the cost of a full scan.

**Threshold:** move to incremental when daily full rebuild cost exceeds ~£50/day per model, or when scan takes longer than the pipeline window. Not before.

## STRUCT over repeated JSON strings

**Decision:** JSON payloads from source systems are promoted to typed STRUCT columns at the staging layer. Raw layer stores original JSON as STRING (`raw_payload`). Staging parses it.

**Why:** STRUCT gives compile-time schema enforcement, dot-notation access, ARRAY sub-field unnesting, and full coverage in `INFORMATION_SCHEMA.COLUMN_FIELD_PATHS` for data contracts. JSON strings stored in TEXT/CLOB (the legacy SAS/Oracle pattern) require runtime parsing at every query, have no schema enforcement, and are invisible to introspection tools.

**The boundary:** raw = STRING blob preserved exactly as received. Staging = typed, contracted, introspectable.

## COLUMN_FIELD_PATHS over COLUMNS for introspection

**Decision:** All schema introspection uses `INFORMATION_SCHEMA.COLUMN_FIELD_PATHS`, not `INFORMATION_SCHEMA.COLUMNS`.

**Why:** `COLUMNS` stops at the top-level column. A STRUCT column appears as one row with type `STRUCT<...>`. Sub-fields are invisible. `COLUMN_FIELD_PATHS` flattens every nested path (`enrichment.device.platform`) into its own row with its own type. Data contracts and drift detection require this — you cannot validate STRUCT sub-field types from `COLUMNS` alone.

## SAFE_DIVIDE over COALESCE(x/y, 0)

**Decision:** All rate calculations use `SAFE_DIVIDE(numerator, denominator)`. Never `COALESCE(numerator/denominator, 0)`.

**Why:** A segment with no eligible users didn't have a 0% rate — it had no meaningful rate. NULL is honest. 0% is a false assertion that will mislead analysis. `SAFE_DIVIDE` returns NULL on zero denominator. Downstream aggregations can choose to exclude NULLs or treat them explicitly.

## BQ is OLAP. Not OLTP.

**Decision:** BQ is not used as a transactional system for any live customer-facing operation.

**Why:** BQ is columnar, optimised for full-partition analytical reads. It cannot do thousands of single-row writes per second with ACID guarantees and sub-millisecond latency. That's what Postgres and DB2/mainframe OLTP systems are for. BQ receives extracted copies of OLTP state as append-only event logs, after the fact. RegRep, FinRep, dashboards, and risk models run against BQ. Core banking does not.

**SCD2 is fine in BQ** because it runs once per daily batch, not thousands of times per second. See [SCD Type 2](scd2.md).
