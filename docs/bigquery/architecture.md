# 👻 BigQuery Architecture

## Stack Layers

```
Source systems (OLTP)
    ↓  ETL / streaming
project.raw          ← append-only event logs, no transformation
    ↓  dbt
project.staging      ← typed, parsed, ds snapshots built here
    ↓  dbt
project.marts        ← dimensions, facts, presentation aggregates
    ↓
Dashboards / analysts / regulatory reporting
```

## Responsibility Split

| Layer | Owned by | What it does |
|---|---|---|
| GCP project | Terraform | Billing, IAM, APIs enabled |
| Datasets (schemas) | Terraform | Container, location (immutable), access controls |
| Tables | dbt / BQ stored procedures | Schema, data, partitioning, clustering |
| Schema migrations | dbt `--full-refresh` | A one-time full refresh applies a schema change to an otherwise incremental model. It is not the routine load strategy (see [Incremental over full rebuild](decisions.md#incremental-over-full-rebuild)) |

**Terraform owns datasets because dataset location is immutable after creation.** Set wrong once, you're dropping everything. Terraform enforces it as code.

**dbt owns tables**, including `CREATE OR REPLACE TABLE`. Terraform managing individual model tables creates state drift on every model change.

## OLTP vs OLAP

BQ is OLAP. Not suitable as an OLTP system.

| Characteristic | OLTP | OLAP (BQ) |
|---|---|---|
| Workload | Thousands of single-row writes/sec | Batch analytical reads |
| Latency requirement | Milliseconds | Seconds acceptable |
| DML | High frequency, row-targeted | Occasional, partition-scoped |
| Primary use | Serve live customer operations | Reporting, metrics, regulatory submissions |

**What lives in OLTP at a bank:**
- Core banking ledger (balance debits/credits, real-time)
- Payment processing (Faster Payments, BACS, CHAPS, card schemes)
- Account state management (open/freeze/close, immediately consistent)
- Fraud decisioning (accept/decline within milliseconds)
- Common OLTP stacks: Postgres, IBM mainframe (CICS + DB2).

**What lives in BQ (OLAP):**
- All of the above, after the fact, extracted and landed as append-only logs
- Analytical dimensions and facts built on top
- Regulatory submissions
- Risk and credit models consuming historical data
- Formerly in SAS/Teradata/Oracle, migrating to BQ/GCP

**Snapshot and SCD2 rebuilds are OLAP, not OLTP.** Staging replaces one `ds` partition per daily batch; the SCD2 carve-out rebuilds from that base on the same cadence. Neither runs thousands of times per second. BQ handles it. See [SCD Type 2](scd2.md).

## Dataset Naming Convention

```
{project}.raw       : source system logs, append-only, no transformation
{project}.staging   : typed, parsed, datestamped daily snapshots (ds)
{project}.marts     : dimensions, facts, presentation aggregates
{project}.utils     : shared stored procedures (project.utils.procedure_name)
{project}.snapshots : schema baseline snapshots for drift detection
{project}.keystore  : per-subject wrapped DEKs. Its own dataset so time
                      travel can be set short, and IAM'd separately
```

`keystore` is a dataset of its own rather than a table in `utils` because
time travel is a dataset property and erasure depends on it being short. See
[Keystore & Erasure](keystore.md).

## Partition and Cluster Strategy

**Partition** on the date the layer is keyed by: `ds` for staging snapshots, the event timestamp for raw, `valid_from` for a derived SCD2 dimension. BQ prunes by partition on a date range, which is what keeps daily loads affordable.

**Cluster** on the most common filter/join columns (entity key, status, type). BQ uses clustering for block-level pruning within partitions.

**Rule of thumb:** partition = "which days do I need?", cluster = "which accounts/users do I need within those days?"

## Related

- [Table Creation](table-creation.md)
- [SCD Type 2](scd2.md)
- [Tooling Decisions](decisions.md)
