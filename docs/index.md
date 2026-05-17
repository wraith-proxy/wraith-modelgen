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

## Install

```bash
uv tool install wraith-modelgen
```

## Quick start

```bash
# Validate your contract
modelgen validate contract.yml

# Generate the raw layer
modelgen gen contract.yml --raw -o ./models/raw

# Generate the staging layer
modelgen gen contract.yml --staging -o ./models/staging

# Preview without writing
modelgen gen contract.yml --raw --dry-run
```

## Credentials

The `gen` command calls BigQuery to introspect the source table schema. It uses Application Default Credentials:

```bash
gcloud auth application-default login
```

Or set `GOOGLE_APPLICATION_CREDENTIALS` to a service account key file.

---

*Part of the [Ghost Stack](https://git.thomaspeoples.com/thomaspeoples). Sovereign. Self-hosted. No nonsense.*
