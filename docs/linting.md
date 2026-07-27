# 👻 Lint Rules

`modelgen validate` runs every semantic check in one pass and reports
all findings together. `gen`, `run`, and `check` refuse contracts with
lint **errors** before any generation happens; **warnings** never block
unless you pass `--strict`.

The point: catch the mistakes that would otherwise surface as broken
SQL in dbt hours later, while they are still one YAML edit away from
fixed.

Validation runs in two stages. First the contract is validated against
the bundled **ODCS v3.0.2 JSON schema** — a malformed document is
rejected before anything below runs. Then these semantic checks run on
the parsed contract. The field names below are the ODCS locations you
edit to fix each finding.

## Errors

| Code | What it catches |
|---|---|
| `invalid-event-name` | `schema[].name` must match `^[a-z][a-z0-9_]*$` — it becomes a dbt model name and a filename |
| `invalid-source-project` | `servers[bigquery].project` is not a plausible GCP project id |
| `invalid-source-dataset` / `invalid-source-table` | `servers[bigquery].dataset` / `schema[].physicalName` contains characters that don't survive SQL generation |
| `invalid-unique-key` | A `primaryKey` property's source name (`physicalName`) is not a valid identifier |
| `duplicate-unique-key` | The same column appears twice in a composite key (two `primaryKey` properties, same `physicalName`) |
| `invalid-loaded-at-field` | `modelgenLoadedAtField` is not a valid identifier |
| `invalid-raw-dataset` / `invalid-staging-dataset` | `modelgenRaw.dataset` / `modelgenStaging.dataset` is not a valid BigQuery identifier |
| `invalid-raw-strategy` / `invalid-staging-strategy` | `incrementalStrategy` is not one of `merge`, `insert_overwrite`, `microbatch` |
| `invalid-staging-materialization` | `modelgenStaging.materialization` is not `snapshot` or `incremental` |
| `snapshot-strategy-conflict` | A snapshot declares an `incrementalStrategy` other than `insert_overwrite`. A snapshot replaces whole `ds` partitions — drop the key and let it derive |
| `snapshot-partition-not-ds` | A snapshot declares a `partitionBy.field` that isn't `ds`. Drop `partitionBy` (it derives) or switch to `materialization: incremental` |
| `ds-column-reserved` | A property maps to the name `ds`, which a snapshot model generates for itself |
| `invalid-raw-partition-field` / `invalid-staging-partition-field` | `partitionBy.field` is not a valid identifier |
| `invalid-raw-partition-type` / `invalid-staging-partition-type` | `partitionBy.dataType` is not `timestamp`, `date`, `datetime`, or `int64` |
| `invalid-raw-partition-granularity` / `invalid-staging-partition-granularity` | Granularity is not `hour`, `day`, `month`, or `year` |
| `invalid-raw-cluster-column` / `invalid-staging-cluster-column` | A `clusterBy` entry is not a valid identifier |
| `invalid-column-source` / `invalid-column-name` | A property's `physicalName` / `name` is not a valid identifier |
| `duplicate-column-source` | The same source column (`physicalName`) is declared by two properties |
| `duplicate-column-name` | Two properties map to the same staging `name` |
| `invalid-column-type` | A property's `physicalType` cast target is not a BigQuery type (aliases like `INT` and `DECIMAL` are accepted; parameterised types and `ARRAY<>`/`STRUCT<>` too) |
| `unique-key-not-in-staging` | A `primaryKey` column is not among the selected staging properties — the generated dbt `unique_key` would point at nothing |
| `loaded-at-not-in-staging` | Same problem for `modelgenLoadedAtField` and the incremental filter |
| `partition-field-not-in-staging` | `modelgenStaging.partitionBy.field` is not a declared property `name` (staging config uses post-rename names) |
| `cluster-column-not-in-staging` | A `modelgenStaging.clusterBy` entry is not a declared property `name` |
| `invalid-classification` | A property's `classification` is not `pii` or `psi` |
| `classified-column-enumerated` | A classified property declares `validValues`. An enumerated column is a category, not a personal value: its ciphertext falls to frequency analysis, and `accepted_values` cannot match BYTES |
| `subject-field-not-declared` | Properties are classified but `modelgenSubjectField` is unset, so no keyset can be resolved |
| `subject-field-not-in-staging` | `modelgenSubjectField` is not a declared property, so the keystore join references a column the model never selects |
| `subject-field-unclassified` | The subject column carries no `classification`, so the join key would be materialised in plaintext — the one column that must not be |
| `cluster-column-classified` | `clusterBy` names a classified column. Encryption emits BYTES and BigQuery cannot cluster on BYTES |
| `partition-field-classified` | `partitionBy.field` is classified. BigQuery partitions on date, timestamp, datetime or int64 only |
| `loaded-at-classified` | `modelgenLoadedAtField` is classified while `materialization: incremental`. The high-water-mark filter would compare a timestamp against ciphertext. A load timestamp is pipeline metadata, not personal data |

## Warnings

| Code | What it catches |
|---|---|
| `missing-column-description` | A property has no `description`. Downstream consumers read these. |
| `unique-key-untested` | The `primaryKey` property carries no tests. Consider `required: true` + `unique: true`. |
| `subject-field-untested` | The subject column has no `not_null` test. A row whose subject has no keyset encrypts to NULL and nothing would surface it. Set `required: true`. |

## Strict mode

```bash
modelgen validate contract.yml --strict
```

Warnings become failures. Wire it into CI when the team has agreed that
undocumented columns are a defect, not a style preference.
