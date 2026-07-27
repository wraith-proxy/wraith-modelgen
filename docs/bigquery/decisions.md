# 👻 Tooling Decisions

Explicit decisions made about tooling, with reasoning. Exists so these don't get relitigated.

## `ds` snapshots are the default; SCD2 is the carve-out

**Decision:** Staging materialises as a datestamped daily snapshot — one
full copy of the dimension per `ds` partition, written with
`insert_overwrite`. SCD2 survives as a derived convenience built on top of
that base, for the regulated case where an auditor needs an immutable trail
of who changed what and when.

**Why:** SCD2 was a compression scheme for expensive storage. Storage is
cheap and engineering time is not, and the SCD2 tax is real: surrogate key
management, `BETWEEN` joins that silently return the wrong row when someone
writes only half the predicate, and sequential backfills because day N
depends on day N-1. A snapshot answers history with `WHERE ds = '<date>'`,
and any date backfills in parallel using the same SQL as the daily run —
the orchestrator injects it with `dbt run --vars '{ds: YYYY-MM-DD}'`.

**Consequence:** `merge_scd2` is gone (TJP-27071). It was the incremental
MERGE path and it carried the failure modes — late-arriving data, dedup
edge cases, MERGE bugs that only surface on a specific event sequence — for
a cost problem that a partitioned base makes even less likely to arrive.
`rebuild_scd2` and `validate_scd2` stay.

**The grain changed, so the tests changed.** A snapshot's grain is
(key, `ds`), not key. modelgen scopes the generated `unique` test to the
partition the run writes; an unscoped one would pass on day one and fail on
day two.

**Opting out:** `materialization: incremental` on `modelgenStaging` restores
the high-water-mark load. Do it when the model genuinely is an append-only
event stream rather than a dimension.

## Right to be forgotten is a crypto-shred, not a delete

**Decision:** Every subject gets a pair of data encryption keys held only in
the keystore, wrapped by a KEK in Cloud KMS. Everything else in the warehouse
holds ciphertext. Erasure under Article 17 is the deletion of one keystore
row. See [Keystore & Crypto-Shred Erasure](keystore.md).

**Why:** An immutable warehouse cannot honour a delete without rewriting
history, and rewriting history is the one thing it exists not to do. Crypto-
shredding moves the erasure from the data to the key: destroy the key and
every ciphertext written under it is unrecoverable, in every table, without
touching a partition. NIST SP 800-88 calls this Cryptographic Erase and
treats it as a Purge-level technique.

**The load-bearing property:** the join key is encrypted **deterministically**,
so its ciphertext is stable per keyset. Joins, row counts and aggregates all
survive the shred. A historical regulatory position still reconstructs in
aggregate; subject-level attestation does not. That is exactly the line
Article 17 draws.

**CMEK is not this.** CMEK encrypts BigQuery storage at rest with a
whole-table blast radius. It protects the media, not the subject, and
deleting one destroys a table rather than a person. Estates that "have CMEK"
routinely have no answer to Article 17 at all.

**Two DEKs per subject, not one.** Tink keysets are primitive-specific:
`DETERMINISTIC_ENCRYPT` needs an AES-SIV keyset, `AEAD.ENCRYPT` needs
AES-GCM, and one keyset cannot serve both. Both are wrapped by the same KEK
and destroyed by the same delete, so it is still one row and one delete.

**Consequences that are design, not tuning:** the keystore lives in its own
dataset so its time-travel window can be set short (48h, the BigQuery
minimum) without crippling recovery elsewhere — time travel is a dataset
property, and Terraform owns datasets. It is **never snapshotted**, which now
has teeth: staging defaults to `ds` snapshots, so the rule is that the
keystore is not a modelgen model and must never acquire a contract. And
decryption happens only in a view or at query time; any materialised table
holding decrypted PII is a second copy the shred never reaches, so erasure
fails silently with nothing to surface it.

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

**Decision:** Default to `CREATE OR REPLACE TABLE ... AS SELECT` (full rebuild) for marts. Switch to incremental MERGE only when scan cost is measurably unacceptable. Staging is the settled case: it snapshots, see above.

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

**Daily snapshots and SCD2 rebuilds are fine in BQ** because they run once per daily batch, not thousands of times per second. See [SCD Type 2](scd2.md).
