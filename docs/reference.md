# 👻 Technical Reference

## Contract structure

A contract is an [Open Data Contract Standard (ODCS) v3.0.2](https://github.com/bitol-io/open-data-contract-standard) YAML document, validated against the bundled ODCS JSON schema. It describes exactly **one** table: a BigQuery `server` (source project + dataset), one `schema` object (the source table), and its `properties` (the staging columns). modelgen's BigQuery/dbt specifics live in `customProperties`.

```mermaid
classDiagram
    class DataContract {
        version: string
        kind: "DataContract"
        apiVersion: "v3.0.2"
        id: string
        status: string
        name: string
    }
    class Server {
        type: "bigquery"
        project: string
        dataset: string
    }
    class SchemaObject {
        name: string
        physicalName: string
        physicalType: "table"
        description: string
    }
    class Property {
        name: string
        physicalName: string
        physicalType: string
        primaryKey: bool
        primaryKeyPosition: int
        required: bool
        unique: bool
        quality: list
    }
    class modelgenCustomProps {
        modelgenLoadedAtField: string
        modelgenRaw: object
        modelgenStaging: object
    }

    DataContract "1" --> "1..*" Server : servers
    DataContract "1" --> "1" SchemaObject : schema
    SchemaObject "1" --> "1..*" Property : properties
    SchemaObject --> modelgenCustomProps : customProperties
```

---

## Contract schema

### Top-level (ODCS)

| Field | Type | Required | Description |
|---|---|---|---|
| `version` | string | yes | Contract version (semver) |
| `kind` | string | yes | Must be `DataContract` |
| `apiVersion` | string | yes | ODCS version; modelgen targets `v3.0.2` |
| `id` | string | yes | Unique contract id (e.g. a UUID) |
| `status` | string | yes | e.g. `active`, `draft` |
| `name` | string | no | Data product name |
| `servers` | list | yes | Must include one `type: bigquery` server |
| `schema` | list | yes | Exactly one schema object (the source table) |

### `servers[]` (the `bigquery` entry)

| Field | Required | Description |
|---|---|---|
| `type` | yes | Must be `bigquery` |
| `project` | yes | GCP project ID |
| `dataset` | yes | BigQuery dataset |

### `schema[0]` (the source table)

| Field | Required | Description |
|---|---|---|
| `name` | yes | Entity name → model filenames (`raw__<name>`, `stg__<name>`) |
| `physicalName` | yes | Source table name |
| `physicalType` | no | `table` |
| `description` | no | Human-readable description |
| `properties` | yes | Staging columns (see below) |
| `customProperties` | yes | modelgen dbt/layer config (see below) |

### `schema[0].properties[]` (staging columns)

| Field | Required | Description |
|---|---|---|
| `name` | yes | Column name in staging (consumer-facing) |
| `physicalName` | no | Source column name (defaults to `name`) |
| `physicalType` | no | BigQuery `CAST` target, e.g. `STRING`, `TIMESTAMP`, `INT64` |
| `description` | no | Column description (written to `.yml`) |
| `primaryKey` / `primaryKeyPosition` | no | Marks a `unique_key` column (uses source names, ordered by position) |
| `required` | no | → `not_null` test |
| `unique` | no | → `unique` test |
| `quality` | no | `rule: validValues` + `validValues: [...]` → `accepted_values` test |

### `schema[0].customProperties` (modelgen config)

| `property` | Value | Description |
|---|---|---|
| `modelgenLoadedAtField` | string | Source timestamp column for incremental filtering |
| `modelgenRaw` | object | Raw layer config: `dataset`, `incrementalStrategy`, `dedup`, `partitionBy {field, dataType, granularity}`, `clusterBy` |
| `modelgenStaging` | object | Staging layer config: same keys as `modelgenRaw` minus `dedup`, plus `generate: false` for a raw-only contract |

---

## Generated files

### `raw__<name>.sql`

```sql
{{ config(
    materialized='incremental',
    unique_key='EVENT_ID',
    incremental_strategy='merge',
    partition_by={...},
    cluster_by=[...]
) }}

with source as (
    select
        EVENT_ID,
        USER_ID,
        RECEIVED_AT
        -- ... all columns introspected at gen time
    from {{ source('landing', 'user_signups_raw') }}
    {% if is_incremental() %}
        where RECEIVED_AT > (select max(RECEIVED_AT) from {{ this }})
    {% endif %}
),

-- dedup CTE (when dedup: true)
deduplicated as (
    select
        EVENT_ID,
        USER_ID,
        RECEIVED_AT,
        -- ...
        row_number() over (
            partition by EVENT_ID
            order by RECEIVED_AT desc
        ) as _modelgen_rn
    from source
)

select
    EVENT_ID,
    USER_ID,
    RECEIVED_AT
    -- ...
from deduplicated
where _modelgen_rn = 1
```

Raw uses source-side column names throughout. Columns are listed explicitly — introspected from BigQuery (or the contract in `--from-contract` mode) at generation time. Regenerate when the source schema changes.

### `raw__<name>.yml`

dbt `sources` entry (all introspected columns) + model definition.

### `stg__<name>.sql`

```sql
{{ config(
    materialized='incremental',
    unique_key='event_id',
    incremental_strategy='merge',
    partition_by={...},
    cluster_by=[...]
) }}

with source as (
    select
        EVENT_ID,
        RECEIVED_AT
        -- ... source-side names declared in contract
    from {{ ref('raw__user_signed_up') }}
    {% if is_incremental() %}
        where RECEIVED_AT > (select max(received_at) from {{ this }})
    {% endif %}
)

select
    cast(EVENT_ID as STRING) as event_id,
    cast(RECEIVED_AT as TIMESTAMP) as received_at
    -- ...
from source
```

Staging uses staging-side names. Raw names never leak downstream.

### `stg__<name>.yml`

dbt model definition with declared columns and their tests.

---

## Type mapping

`IntrospectedColumn.dbt_data_type` maps BigQuery field modes and types to dbt-compatible strings:

| BigQuery mode | BigQuery type | dbt type |
|---|---|---|
| `REPEATED` | any | `ARRAY<type>` |
| any | `RECORD` | `STRUCT` |
| any | everything else | raw BQ type (e.g. `STRING`, `INT64`) |
