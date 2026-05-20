# 👻 BigQuery Architecture

## Stack Layers

```
Source systems (OLTP)
    ↓  ETL / streaming
project.raw          ← append-only event logs, no transformation
    ↓  dbt
project.staging      ← typed, parsed, SCD2 built here
    ↓  dbt
project.marts        ← dimensions, facts, presentation aggregates
    ↓
Dashboards / analysts / RegRep / FinRep
```

## Responsibility Split

| Layer | Owned by | What it does |
|---|---|---|
| GCP project | Terraform | Billing, IAM, APIs enabled |
| Datasets (schemas) | Terraform | Container, location (immutable), access controls |
| Tables | dbt / BQ stored procedures | Schema, data, partitioning, clustering |
| Schema migrations | dbt `--full-refresh` | Handled by rebuild, not DDL migrations |

**Terraform owns datasets because dataset location is immutable after creation.** Set wrong once, you're dropping everything. Terraform enforces it as code.

**dbt owns tables** — including `CREATE OR REPLACE TABLE`. Not Terraform. TF managing individual model tables creates state drift on every model change.

**No Liquibase.** Liquibase solves surgical incremental DDL migration for OLTP (where you can't drop and rebuild). BQ warehouse pattern rebuilds from source. The problem Liquibase solves doesn't exist here.

## OLTP vs OLAP

BQ is OLAP. Not suitable as an OLTP system.

| Characteristic | OLTP | OLAP (BQ) |
|---|---|---|
| Workload | Thousands of single-row writes/sec | Batch analytical reads |
| Latency requirement | Milliseconds | Seconds acceptable |
| DML | High frequency, row-targeted | Occasional, partition-scoped |
| Primary use | Serve live customer operations | Reporting, metrics, FinRep, RegRep |

**What lives in OLTP at a bank:**
- Core banking ledger (balance debits/credits, real-time)
- Payment processing (Faster Payments, BACS, CHAPS, card schemes)
- Account state management (open/freeze/close, immediately consistent)
- Fraud decisioning (accept/decline within milliseconds)
- Common OLTP stacks: Postgres, IBM mainframe (CICS + DB2).

**What lives in BQ (OLAP):**
- All of the above, after the fact, extracted and landed as append-only logs
- Analytical dimensions and facts built on top
- FinRep / RegRep regulatory submissions
- Risk and credit models consuming historical data
- Formerly in SAS/Teradata/Oracle — migrating to BQ/GCP

**SCD2 is OLAP, not OLTP.** The MERGE statement closing a previous record runs once per daily batch, not thousands of times per second. BQ handles it. See [SCD Type 2](scd2.md).

## Dataset Naming Convention

```
{project}.raw       — source system logs, append-only, no transformation
{project}.staging   — typed, parsed, SCD2 applied
{project}.marts     — dimensions, facts, presentation aggregates
{project}.utils     — shared stored procedures (project.utils.procedure_name)
{project}.snapshots — schema baseline snapshots for drift detection
```

## Partition and Cluster Strategy

**Partition** on the primary timestamp column (event date, valid_from). BQ uses partitions for date-range pruning — critical for cost on daily incremental loads.

**Cluster** on the most common filter/join columns (entity key, status, type). BQ uses clustering for block-level pruning within partitions.

**Rule of thumb:** partition = "which days do I need?", cluster = "which accounts/users do I need within those days?"

## Related

- [Table Creation](table-creation.md)
- [SCD Type 2](scd2.md)
- [Tooling Decisions](decisions.md)
