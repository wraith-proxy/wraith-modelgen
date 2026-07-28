# 👻 Tooling Decisions

Explicit decisions made about tooling, with reasoning. Exists so these don't get relitigated.

## `ds` snapshots are the default; SCD2 is the carve-out

**Decision:** Staging materialises as a datestamped daily snapshot: one
full copy of the dimension per `ds` partition, written with
`insert_overwrite`. SCD2 survives as a derived convenience built on top of
that base, for the regulated case where an auditor needs an immutable trail
of who changed what and when.

**Why:** SCD2 was a compression scheme for expensive storage. Storage is
cheap and engineering time is not, and the SCD2 tax is real: surrogate key
management, `BETWEEN` joins that silently return the wrong row when someone
writes only half the predicate, and sequential backfills because day N
depends on day N-1. A snapshot answers history with `WHERE ds = '<date>'`,
and any date backfills in parallel using the same SQL as the daily run,
which the orchestrator injects with `dbt run --vars '{ds: YYYY-MM-DD}'`.

**Consequence:** `merge_scd2` is gone (TJP-27071). It was the incremental
MERGE path, and it carried the failure modes (late-arriving data, dedup
edge cases, MERGE bugs that only surface on a specific event sequence) for
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

**CMEK does not cover this.** CMEK encrypts BigQuery storage at rest with a
whole-table blast radius. It protects the media rather than the subject, and
deleting a CMEK key destroys a table rather than a person. An estate can have
CMEK everywhere and still have no mechanism for Article 17.

**Two DEKs per subject, not one.** Tink keysets are primitive-specific:
`DETERMINISTIC_ENCRYPT` needs an AES-SIV keyset, `AEAD.ENCRYPT` needs
AES-GCM, and one keyset cannot serve both. Both are wrapped by the same KEK
and destroyed by the same delete, so it is still one row and one delete.

**Consequences that are design, not tuning:** the keystore lives in its own
dataset so its time-travel window can be set short (48h, the BigQuery
minimum) without crippling recovery elsewhere. Time travel is a dataset
property, and Terraform owns datasets. It is **never snapshotted**, which now
has teeth: staging defaults to `ds` snapshots, so the rule is that the
keystore is not a modelgen model and must never acquire a contract. And
decryption happens only in a view or at query time; any materialised table
holding decrypted PII is a second copy the shred never reaches, so erasure
fails silently with nothing to surface it.

## Terraform owns datasets, not tables

**Decision:** Terraform creates and manages BQ datasets. dbt / stored procedures create and manage tables.

**Why:** Dataset location is immutable after creation. Set to the wrong region once and you're dropping everything to fix it. Terraform enforces this as code, reviewed, version-controlled. Tables change constantly as models evolve, and Terraform cannot track that without perpetual state drift.

**What TF manages:** `google_bigquery_dataset` (location, IAM, default expiry, labels). Nothing else in BQ.


## Metrics tables over materialised views

**Decision:** the presentation layer uses tables, not BQ materialised views.

**Why:** a BQ materialised view that aggregates cannot apply scalar
transformations after `GROUP BY`, and `SAFE_DIVIDE` (used throughout for rate
calculations) is exactly that. Tables sidestep the constraint and give
explicit control over partitioning, clustering, and refresh cadence.
Materialised view refresh is also non-deterministic under load.

**Cost:** the refresh has to be orchestrated by hand. Acceptable at current
scale.

**Materialising these tables does not break the crypto-shred**, as long as a
classified column is carried forward as ciphertext. The table holds the same
encrypted bytes staging holds, so destroying the keyset makes the metrics
table's copy unrecoverable at the same instant, with no partition to rewrite.

The boundary is decryption, not materialisation. A table built by selecting
from a decrypting view holds plaintext that no key deletion can reach, and
erasure then fails silently with nothing to surface it. Decrypt in a view or
at query time, and materialise ciphertext only.

Aggregates computed before a shred keep their old values, which is the
intended behaviour: a historical position still reconstructs in aggregate
while subject-level attribution does not. One thing to watch is that
deterministic ciphertext used as a grouping key is a pseudonym rather than an
erasure, so a metrics table grouped to very small cells can still expose a
subject by linkage even after the key is gone. Aggregate to cells big enough
that a single subject is not identifiable, the same reasoning behind the
`classified-column-enumerated` lint rule.

## Incremental over full rebuild

**Decision:** Marts default to incremental, using MERGE keyed on the model's
grain. `CREATE OR REPLACE TABLE ... AS SELECT` is not the default for any
layer. Raw and staging already comply: raw merges, staging writes whole `ds`
partitions with `insert_overwrite`. Marts are still hand-written, so for now
this is forward guidance rather than something modelgen enforces.

**Why:** a full rebuild scans the whole table every run, so its cost tracks
table size rather than how much actually changed, and nothing tells you it
has become expensive until the bill does. Incremental keeps cost roughly
proportional to the change. MERGE does carry real failure modes (late
arriving data, dedup edge cases, bugs that only surface on a particular event
sequence), but raw and staging have carried those in production since
TJP-27071 and they have been manageable. There is no principled reason marts
get an easier standard than the layers feeding them.

**Exemptions, not defaults:** full rebuild is allowed as an explicit,
documented exception. The table has to rebuild from an already-partitioned,
cheap derived input rather than raw source volume, and correctness or
debuggability has to outweigh the extra cost for that specific table.
`utils.rebuild_scd2` is the one recorded exception, covered in
[SCD Type 2](scd2.md). Anything else claiming full rebuild needs the same
argument written down here, not a quiet default.

## STRUCT over repeated JSON strings

**Decision:** JSON payloads from source systems are promoted to typed STRUCT columns at the staging layer. Raw layer stores original JSON as STRING (`raw_payload`). Staging parses it.

**Why:** STRUCT gives compile-time schema enforcement, dot-notation access, ARRAY sub-field unnesting, and full coverage in `INFORMATION_SCHEMA.COLUMN_FIELD_PATHS` for data contracts. JSON strings stored in TEXT/CLOB (the legacy SAS/Oracle pattern) require runtime parsing at every query, have no schema enforcement, and are invisible to introspection tools.

**The boundary:** raw = STRING blob preserved exactly as received. Staging = typed, contracted, introspectable.

## COLUMN_FIELD_PATHS over COLUMNS for introspection

**Decision:** All schema introspection uses `INFORMATION_SCHEMA.COLUMN_FIELD_PATHS`, not `INFORMATION_SCHEMA.COLUMNS`.

**Why:** `COLUMNS` stops at the top-level column. A STRUCT column appears as one row with type `STRUCT<...>`. Sub-fields are invisible. `COLUMN_FIELD_PATHS` flattens every nested path (`enrichment.device.platform`) into its own row with its own type. Data contracts and drift detection need this, because you cannot validate STRUCT sub-field types from `COLUMNS` alone.

## SAFE_DIVIDE over COALESCE(x/y, 0)

**Decision:** All rate calculations use `SAFE_DIVIDE(numerator, denominator)`. Never `COALESCE(numerator/denominator, 0)`.

**Why:** a segment with no eligible users does not have a 0% rate, it has no rate at all. NULL says that; 0% asserts something false and will skew any average taken over it. `SAFE_DIVIDE` returns NULL on a zero denominator. Downstream aggregations can choose to exclude NULLs or treat them explicitly.

## BQ is OLAP. Not OLTP.

**Decision:** BQ is not used as a transactional system for any live customer-facing operation.

**Why:** BQ is columnar, optimised for full-partition analytical reads. It cannot do thousands of single-row writes per second with ACID guarantees and sub-millisecond latency. That is what Postgres and DB2/mainframe OLTP systems are for. BQ receives extracted copies of OLTP state as append-only event logs, after the fact. Regulatory submissions, dashboards, and risk models run against BQ. Core banking does not.

**Daily snapshots and SCD2 rebuilds are fine in BQ** because they run once per daily batch, not thousands of times per second. See [SCD Type 2](scd2.md).
