# wraith-modelgen

BigQuery dbt model scaffolder. Generates Origin and Consumption Data Products from a single YAML contract.

## Why this exists

Source schemas change. Columns get renamed. Types get tightened. New fields appear without ceremony. In most analytics codebases this is a downstream catastrophe: dashboards break, FDPs fail, the analytics team gets paged at 03:00, and someone files a ticket asking whether dbt itself is broken.

modelgen makes the data contract the change-management mechanism:

- **Origin Data Product** (`raw__event`): `select *` from landing with optional dedup. New columns flow through automatically.
- **Consumption Data Product** (`stg__event`): explicit renames, casts, and tests. The stable interface downstream consumes.

When source changes, the source team updates the contract as part of their release. modelgen regenerates the dbt models. Downstream sees what it sees. Nothing breaks unless something it actually depended on disappeared, at which point you want to know.

## Install

```bash
uv pip install -e .
```

You'll also want Application Default Credentials configured for BigQuery:

```bash
gcloud auth application-default login
```

## Workflow

```bash
modelgen gen contract.yml --raw     -o ./models/raw
modelgen gen contract.yml --staging -o ./models/staging
```

One layer per invocation. `--raw` and `--staging` are mutually exclusive.

## Contract anatomy

```yaml
version: "1"

event:
  name: user_signed_up                 # the entity
  unique_key: EVENT_ID                 # SOURCE column name (composite: [a, b])
  loaded_at_field: RECEIVED_AT         # SOURCE column name

  source:
    project: my-gcp-project
    dataset: landing
    table: user_signups_raw

  raw:
    dataset: raw
    incremental_strategy: merge
    dedup: true                        # row_number() partition by unique_key
    partition_by:
      field: RECEIVED_AT
      data_type: timestamp
      granularity: day
    cluster_by: [USER_ID]

  staging:
    dataset: staging
    incremental_strategy: merge
    partition_by:
      field: received_at               # staging-side name (post-rename)
      data_type: timestamp
      granularity: day
    cluster_by: [user_id]

    columns:
      - source: EVENT_ID               # name in raw (== name in source)
        name: event_id                 # name in staging
        type: STRING                   # cast target
        description: "..."
        tests: [not_null, unique]
```

## Schema evolution

| Source change                | What modelgen does                         | What you do                          |
|------------------------------|--------------------------------------------|--------------------------------------|
| Adds a column                | Flows into raw via `select *`. Invisible in staging. | Add it to staging when consumers need it. |
| Renames a column             | Validation fails: column not found in source. | Update the `source:` field on that column. Also `unique_key` / `loaded_at_field` if applicable. |
| Retypes a column             | Existing CAST in staging absorbs it (or fails loudly at query time if values are incompatible). | Update `type:` if the canonical type should change too. |
| Drops a column staging uses  | Validation fails: column not found.        | Either restore upstream or remove from staging contract. |

The validation runs as part of `modelgen gen --staging`. If it passes, your generated staging model still presents the same contract to downstream consumers.

## CLI

```text
modelgen gen CONTRACT [--raw|--staging] [--output DIR] [--dry-run] [--verbose]
modelgen validate CONTRACT
```

`validate` checks contract YAML without hitting BigQuery. `gen --dry-run` runs introspection and templating but writes nothing.

## What gets generated

For `--raw`:
- `raw__event.sql`: dbt incremental model with `{{ source(...) }}`, optional dedup window, partition_by and cluster_by config.
- `raw__event.yml`: dbt sources entry plus model definition. Columns mirror the introspected source schema.

For `--staging`:
- `stg__event.sql`: dbt incremental model with `{{ ref('raw__event') }}`. Casts and renames applied. Filter uses the source-side column name against upstream and staging-side name against `this`.
- `stg__event.yml`: model definition with column tests from the contract.

## Architecture notes

- **One layer per invocation.** Layers have different lifecycles; conflating them in one command made things harder to reason about.
- **Introspection at gen time.** The contract is minimal; the warehouse is the source of truth for column names and types in source. Drift is impossible because nothing is duplicated.
- **Deterministic output.** Same contract + same source schema = byte-identical files. CI can diff against committed output to catch drift between contract and source.
- **Strict failure on missing columns.** No silent passes. If the contract references a column the source no longer has, generation fails with the column name.
- **No silent ignores of new source columns.** They appear in raw and are surfaced in the CLI output so you know they exist, but they don't enter staging until you opt them in.

## Testing

```bash
uv run poe test-ci
```

The test suite uses `FakeIntrospector` and runs without BigQuery credentials. Covers contract parsing, both layers, the schema evolution scenarios, composite keys, REPEATED/RECORD types, determinism, and the error surfaces.

## Limitations

- BigQuery only. The introspection module is BQ-native (uses `google-cloud-bigquery`, not SQLAlchemy). Add a different `Introspector` implementation if you need another warehouse.
- Staging transformations are limited to `cast(x as Y) as z` and `x as z`. Arbitrary SQL expressions in staging columns are not supported. The constraint is deliberate: the staging contract should be readable as a contract, not as code.
