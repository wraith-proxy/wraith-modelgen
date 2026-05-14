# modelgen

BigQuery dbt model scaffolder. Generates raw and staging dbt models from a YAML data contract.

## What it does

Given a YAML data contract describing a BigQuery source table, `modelgen` generates:

- **Raw model** (`raw__<event>.sql` + `.yml`) — a pass-through incremental model with optional dedup via `row_number()`. Schema mirrors source; new columns flow through automatically.
- **Staging model** (`stg__<event>.sql` + `.yml`) — a consumer-facing contract with explicit column selection, renames, and casts. Absorbs upstream schema drift so downstream never sees it.

## Install

```bash
uv tool install modelgen
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
