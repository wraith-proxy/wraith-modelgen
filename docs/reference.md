# Technical Reference

## Contract schema

### Top-level

| Field | Type | Required | Description |
|---|---|---|---|
| `version` | string | yes | Must be `"1"` |
| `event` | mapping | yes | The event/entity definition |

### `event`

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Event name, used in model filenames (`raw__<name>`, `stg__<name>`) |
| `description` | string | no | Human-readable description |
| `unique_key` | string or list | yes | Source column(s) that uniquely identify a row |
| `loaded_at_field` | string | yes | Source timestamp column used for incremental filtering |
| `source` | mapping | yes | BigQuery source reference |
| `raw` | mapping | no | Raw layer config (defaults apply) |
| `staging` | mapping | yes (for staging gen) | Staging layer config |

### `source`

| Field | Required | Description |
|---|---|---|
| `project` | yes | GCP project ID |
| `dataset` | yes | BigQuery dataset |
| `table` | yes | BigQuery table |

### `raw`

| Field | Default | Description |
|---|---|---|
| `dataset` | `raw` | Target dataset |
| `incremental_strategy` | `merge` | dbt incremental strategy |
| `dedup` | `false` | Deduplicate via `row_number()` on `unique_key` |
| `partition_by` | none | Partition config (`field`, `data_type`, `granularity`) |
| `cluster_by` | `[]` | Cluster columns |

### `staging.columns`

| Field | Required | Description |
|---|---|---|
| `source` | yes | Column name in raw/source |
| `name` | no | Column name in staging (defaults to `source.lower()`) |
| `type` | no | BigQuery type to cast to |
| `description` | no | Column description |
| `tests` | no | dbt tests (`not_null`, `unique`, `accepted_values`, etc.) |

## Generated files

### Raw: `raw__<name>.sql`

- `{{ config(...) }}` block with `materialized='incremental'`
- `select *` from `{{ source(...) }}`
- Optional dedup CTE using `row_number() over (partition by <unique_key>)`
- `{% if is_incremental() %}` filter on `loaded_at_field`

### Raw: `raw__<name>.yml`

- dbt `sources` block listing all introspected columns
- dbt `models` block with model config

### Staging: `stg__<name>.sql`

- `{{ config(...) }}` block
- CTE selecting only declared columns from `{{ ref('raw__<name>') }}`
- Explicit `cast()` and rename expressions
- `{% if is_incremental() %}` filter

### Staging: `stg__<name>.yml`

- dbt `models` block with declared columns and their tests
