# 👻 wraith-modelgen

[![Documentation](https://img.shields.io/badge/docs-live-brightgreen)](https://www.thomaspeoples.com/gitea-repos/wraith-modelgen/)
![PyPI - Version](https://img.shields.io/pypi/v/wraith-modelgen)
![Coverage](https://www.thomaspeoples.com/gitea-repos/wraith-modelgen/coverage.svg)

*BigQuery dbt model scaffolder for the Ghost Stack. One YAML contract, two sovereign data products.*

---

## What it does

Given a YAML data contract describing a BigQuery source table, `wraith-modelgen` generates:

- **Raw model** (`raw__<event>.sql` + `.yml`): pass-through incremental model with optional dedup via `row_number()`. Schema mirrors source; new columns flow through automatically.
- **Staging model** (`stg__<event>.sql` + `.yml`): consumer-facing contract with explicit column selection, renames, and casts. Absorbs upstream schema drift so downstream never sees it.

Source schema changes are handled at the raw/staging boundary, not in your downstream models.

Staging defaults to a **datestamped daily snapshot**: `insert_overwrite` partitioned on a generated `ds` column, one full copy of the dimension per day. History is `where ds = '<date>'`, and any date backfills in parallel with the same SQL (`dbt run --vars '{ds: 2026-07-27}'`). Set `materialization: incremental` on `modelgenStaging` to opt back into a high-water-mark load.

A property marked `classification: pii` or `psi` is **encrypted into staging** and never materialised in plaintext. The subject column is encrypted deterministically so joins survive erasure; right to be forgotten is a single DELETE from the keystore. See [Keystore & Erasure](bigquery/keystore.md).

## Install

```bash
uv tool install wraith-modelgen
```

## Quick start

```bash
# Initialise the project layout (once)
modelgen init

# Draft a contract from a live table — every column declared,
# keys guessed and marked TODO
modelgen new my-project.landing.user_signups

# Lint it (offline, all findings in one pass)
modelgen validate contracts/user_signups.yml

# Generate both layers for every contract
modelgen run

# CI: prove committed models still match their contracts
modelgen check --diff
```

Single-layer generation is also available when you need it:

```bash
modelgen gen contract.yml --raw -o ./models/raw
modelgen gen contract.yml --staging -o ./models/staging
modelgen gen contract.yml --raw --dry-run
```

## Credentials

`new`, `gen`, `run`, and `check` call BigQuery to introspect source
table schemas, using Application Default Credentials:

```bash
gcloud auth application-default login
```

Or set `GOOGLE_APPLICATION_CREDENTIALS` to a service account key file.

All but `new` also run without credentials: pass `--from-contract` to
generate from the contract's declared columns alone, which is what CI does.
Without the flag they fall back to it automatically when no credentials are
found. `new` always needs BigQuery — it has nothing to work from but the
live table.

---

*Part of the [Ghost Stack](https://git.thomaspeoples.com/thomaspeoples). Sovereign. Self-hosted. No nonsense.*
