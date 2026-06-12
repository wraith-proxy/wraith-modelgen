# 👻 Lint Rules

`modelgen validate` runs every semantic check in one pass and reports
all findings together. `gen`, `run`, and `check` refuse contracts with
lint **errors** before any generation happens; **warnings** never block
unless you pass `--strict`.

The point: catch the mistakes that would otherwise surface as broken
SQL in dbt hours later, while they are still one YAML edit away from
fixed.

## Errors

| Code | What it catches |
|---|---|
| `invalid-event-name` | `event.name` must match `^[a-z][a-z0-9_]*$` — it becomes a dbt model name and a filename |
| `invalid-source-project` | `source.project` is not a plausible GCP project id |
| `invalid-source-dataset` / `invalid-source-table` | Source reference contains characters that don't survive SQL generation |
| `invalid-unique-key` | A `unique_key` column is not a valid identifier |
| `duplicate-unique-key` | The same column appears twice in a composite key |
| `invalid-loaded-at-field` | `loaded_at_field` is not a valid identifier |
| `invalid-raw-dataset` / `invalid-staging-dataset` | Target dataset is not a valid BigQuery identifier |
| `invalid-raw-strategy` / `invalid-staging-strategy` | `incremental_strategy` is not one of `merge`, `insert_overwrite`, `microbatch` |
| `invalid-raw-partition-field` / `invalid-staging-partition-field` | `partition_by.field` is not a valid identifier |
| `invalid-raw-partition-type` / `invalid-staging-partition-type` | `partition_by.data_type` is not `timestamp`, `date`, `datetime`, or `int64` |
| `invalid-raw-partition-granularity` / `invalid-staging-partition-granularity` | Granularity is not `hour`, `day`, `month`, or `year` |
| `invalid-raw-cluster-column` / `invalid-staging-cluster-column` | A `cluster_by` entry is not a valid identifier |
| `invalid-column-source` / `invalid-column-name` | A staging column declaration is not a valid identifier |
| `duplicate-column-source` | The same source column is declared twice in staging |
| `duplicate-column-name` | Two staging columns map to the same output name |
| `invalid-column-type` | A `type:` cast target is not a BigQuery type (aliases like `INT` and `DECIMAL` are accepted; parameterised types and `ARRAY<>`/`STRUCT<>` too) |
| `unique-key-not-in-staging` | `unique_key` references a column staging never selects — the generated dbt `unique_key` would point at nothing |
| `loaded-at-not-in-staging` | Same problem for `loaded_at_field` and the incremental filter |
| `partition-field-not-in-staging` | `staging.partition_by.field` is not a declared staging column **name** (staging config uses post-rename names) |
| `cluster-column-not-in-staging` | A staging `cluster_by` entry is not a declared staging column name |

## Warnings

| Code | What it catches |
|---|---|
| `missing-column-description` | A staging column has no description. Downstream consumers read these. |
| `unique-key-untested` | The column backing the unique key declares no dbt tests. Consider `[not_null, unique]`. |

## Strict mode

```bash
modelgen validate contract.yml --strict
```

Warnings become failures. Wire it into CI when the team has agreed that
undocumented columns are a defect, not a style preference.
