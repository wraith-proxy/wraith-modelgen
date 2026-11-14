# 👻 wraith-modelgen

[![Documentation](https://img.shields.io/badge/docs-live-brightgreen)](https://www.thomaspeoples.com/gitea-repos/wraith-modelgen/)
![PyPI - Version](https://img.shields.io/pypi/v/wraith-modelgen)
![Coverage](https://www.thomaspeoples.com/gitea-repos/wraith-modelgen/coverage.svg)

*dbt model scaffolder for the Ghost Stack (BigQuery and DuckDB). One YAML contract, sovereign data products.*

---

## What it does

Given a YAML data contract (ODCS v3.0.2, v3.1.0 or v3.2.0) describing a source table, `wraith-modelgen` generates:

- **Raw model** (`raw__<event>.sql` + `.yml`): pass-through incremental model with optional dedup via `row_number()`. Schema mirrors source; new columns flow through automatically.
- **Staging model** (`stg__<event>.sql` + `.yml`): consumer-facing contract with explicit column selection, renames, and casts. Absorbs upstream schema drift so downstream never sees it.

- **Models yml** (`modelgen schema`): `_<contract>__models.yml` plus dbt tests mapped from the contract's quality rules, for tables your project produces.
- **Sources yml** (`modelgen sources`): `_<contract>__sources.yml` with freshness and a column-set test, for tables your project reads (for example a Parquet extract).

Source schema changes are handled at the raw/staging boundary, not in your downstream models. `modelgen run` writes raw and staging, plus the produced and extract yml and tests when `schema_*` and `sources_*` are set in `.modelgen.yml`; `schema` and `sources` stay available per contract, and `check` gates all of it.

The target dialect comes from the contract's `servers[].type`: `bigquery`, `duckdb` or `local`. The partitioning, clustering and encryption described below are BigQuery only; DuckDB models are views unless the contract says `materialization: table`. See [Targets and dialects](reference.md#targets-and-dialects).

Staging defaults to a **datestamped daily snapshot**: `insert_overwrite` partitioned on a generated `ds` column, one full copy of the dimension per day. Each run replaces that day's partition rather than the whole table, so the load is incremental even though each partition holds a complete daily copy. History is `where ds = '<date>'`, and any date backfills in parallel with the same SQL (`dbt run --vars '{ds: 2026-07-27}'`). Set `materialization: incremental` on `modelgenStaging` to opt back into a high-water-mark load.

Both layers load incrementally. No generated model emits `CREATE OR REPLACE TABLE ... AS SELECT`, and full rebuild is a documented exception rather than a default. See [Tooling Decisions](bigquery/decisions.md).

A property marked `classification: pii` or `psi` is **encrypted into staging** and never materialised in plaintext. The subject column is encrypted deterministically so joins survive erasure; right to be forgotten is a single DELETE from the keystore. See [Keystore & Erasure](bigquery/keystore.md).

## Contract standards

`modelgen validate --strict` is the CI gate. Alongside the structural checks it
holds contracts to a few standards that start to matter once more than one team
reads them:

| Standard | Rule |
|---|---|
| Every column is documented | `description` of at least 50 characters that says something the column name does not |
| Every source declares staleness | `modelgenRaw.freshness` with `warnAfter` / `errorAfter`, which becomes the dbt `freshness:` block |
| Every key is tested | `primaryKey` properties carry `required` and `unique` |
| Exceptions are written down | `modelgenExempt` opts out of one rule, and a `reason` is mandatory |

An exemption is scoped either to the whole contract or to a single column, and
the two never cross. Exempted findings still print on every `validate` run,
labelled `exempt` with their reason, and the summary counts them. They stop
blocking, they do not stop being visible. See [Lint Rules](linting.md).

## Install

```bash
uv tool install wraith-modelgen
```

## Quick start

```bash
# Initialise the project layout (once)
modelgen init

# Draft a contract from a live table: every column declared,
# keys guessed and marked TODO
modelgen new my-project.landing.user_signups

# Lint it (offline, all findings in one pass)
modelgen validate contracts/user_signups.yml

# Generate raw and staging for every contract
modelgen run

# Models yml for tables this project produces, sources yml for tables it reads
modelgen schema contracts/marts.odcs.yaml -o models/marts
modelgen sources contracts/extract.odcs.yaml -o models/staging

# CI: prove committed models still match their contracts
modelgen check --diff
```

Single-layer generation is also available when you need it:

```bash
modelgen gen contract.yml --raw -o ./models/raw
modelgen gen contract.yml --staging -o ./models/staging
modelgen gen contract.yml --raw --dry-run
modelgen gen contract.yml --layer schema -o ./models/marts
```

## Credentials

`new`, and `gen`, `run` and `check` on contracts whose selected server uses the BigQuery dialect,
call BigQuery to introspect source table schemas, using Application Default Credentials. `schema`, `sources` and `validate` never do:

```bash
gcloud auth application-default login
```

Or set `GOOGLE_APPLICATION_CREDENTIALS` to a service account key file.

All but `new` also run without credentials: pass `--from-contract` to
generate from the contract's declared columns alone, which is what CI does.
Without the flag they use contract-only introspection automatically when no credentials are
found, and always for a DuckDB or local contract. `new` always needs BigQuery, since it has nothing to work from but the
live table.

---

*Part of the [Ghost Stack](https://git.thomaspeoples.com/thomaspeoples). Sovereign. Self-hosted. No nonsense.*
