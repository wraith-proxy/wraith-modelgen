# 👻 Lint Rules

`modelgen validate` runs every semantic check in one pass and reports
all findings together. `gen`, `run`, and `check` refuse contracts with
lint **errors** before any generation happens; **warnings** never block
unless you pass `--strict`.

The aim is to catch the mistakes that would otherwise surface as broken
SQL in dbt hours later, while they are still one YAML edit away from
being fixed.

Validation runs in two stages. First the contract is validated against
the bundled **ODCS v3.0.2 JSON schema**. A malformed document is
rejected before anything below runs. Then these semantic checks run on
the parsed contract. The field names below are the ODCS locations you
edit to fix each finding.

## Errors

| Code | What it catches |
|---|---|
| `invalid-event-name` | `schema[].name` must match `^[a-z][a-z0-9_]*$`, because it becomes a dbt model name and a filename |
| `invalid-source-project` | `servers[bigquery].project` is not a plausible GCP project id |
| `invalid-source-dataset` / `invalid-source-table` | `servers[bigquery].dataset` / `schema[].physicalName` contains characters that don't survive SQL generation |
| `invalid-unique-key` | A `primaryKey` property's source name (`physicalName`) is not a valid identifier |
| `duplicate-unique-key` | The same column appears twice in a composite key (two `primaryKey` properties, same `physicalName`) |
| `invalid-loaded-at-field` | `modelgenLoadedAtField` is not a valid identifier |
| `invalid-raw-dataset` / `invalid-staging-dataset` | `modelgenRaw.dataset` / `modelgenStaging.dataset` is not a valid BigQuery identifier |
| `invalid-raw-strategy` / `invalid-staging-strategy` | `incrementalStrategy` is not one of `merge`, `insert_overwrite`, `microbatch` |
| `invalid-staging-materialization` | `modelgenStaging.materialization` is not `snapshot` or `incremental` |
| `snapshot-strategy-conflict` | A snapshot declares an `incrementalStrategy` other than `insert_overwrite`. A snapshot replaces whole `ds` partitions, so drop the key and let it derive |
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
| `unique-key-not-in-staging` | A `primaryKey` column is not among the selected staging properties, so the generated dbt `unique_key` would point at nothing |
| `loaded-at-not-in-staging` | Same problem for `modelgenLoadedAtField` and the incremental filter |
| `partition-field-not-in-staging` | `modelgenStaging.partitionBy.field` is not a declared property `name` (staging config uses post-rename names) |
| `cluster-column-not-in-staging` | A `modelgenStaging.clusterBy` entry is not a declared property `name` |
| `invalid-classification` | A property's `classification` is not `pii` or `psi` |
| `classified-column-enumerated` | A classified property declares `validValues`. An enumerated column is a category, not a personal value: its ciphertext falls to frequency analysis, and `accepted_values` cannot match BYTES |
| `subject-field-not-declared` | Properties are classified but `modelgenSubjectField` is unset, so no keyset can be resolved |
| `subject-field-not-in-staging` | `modelgenSubjectField` is not a declared property, so the keystore join references a column the model never selects |
| `subject-field-unclassified` | The subject column carries no `classification`, so the join key would be materialised in plaintext, and it is the one column that must not be |
| `cluster-column-classified` | `clusterBy` names a classified column. Encryption emits BYTES and BigQuery cannot cluster on BYTES |
| `partition-field-classified` | `partitionBy.field` is classified. BigQuery partitions on date, timestamp, datetime or int64 only |
| `loaded-at-classified` | `modelgenLoadedAtField` is classified while `materialization: incremental`. The high-water-mark filter would compare a timestamp against ciphertext. A load timestamp is pipeline metadata, not personal data |
| `invalid-exemption-reason` | A `modelgenExempt` entry declares no `reason`. Every exemption has to say why the rule does not apply |
| `invalid-freshness-period` | `modelgenRaw.freshness` uses a period other than `minute`, `hour`, or `day` |
| `invalid-freshness-count` | `modelgenRaw.freshness` declares a count that is not a positive integer |

## Warnings

| Code | What it catches |
|---|---|
| `missing-column-description` | A property has no `description`. Downstream consumers read these. |
| `low-quality-column-description` | A property has a `description`, but it is under 50 characters or it only restates the column name. |
| `missing-freshness` | `modelgenRaw` declares no `freshness` block, so `dbt source freshness` has nothing to check. |
| `unique-key-untested` | The `primaryKey` property carries no tests. Consider `required: true` + `unique: true`. |
| `subject-field-untested` | The subject column has no `not_null` test. A row whose subject has no keyset encrypts to NULL and nothing would surface it. Set `required: true`. |

## Column descriptions

A description has to be at least **50 characters** and has to say something the
column name does not. Anything shorter, or anything that just repeats the name,
raises `low-quality-column-description`.

Fifty is deliberately more than it takes to name the column again. The reader
these are written for did not build the model and has no context beyond what
the contract gives them, so a description is worth writing only if it covers
what the value means and when it is null:

```yaml
# raises low-quality-column-description
description: "The user ID."

# passes
description:
  "Account that completed signup. Encrypted deterministically, so joins
   and distinct counts still work on the ciphertext."
```

`missing-column-description` and `low-quality-column-description` never fire
together. The first covers an absent description, the second a present one.

## Source freshness

`modelgenRaw.freshness` becomes the dbt `freshness:` block on the generated
source, which is what `dbt source freshness` reads:

```yaml
- property: modelgenRaw
  value:
    dataset: raw
    freshness:
      warnAfter: {count: 12, period: hour}
      errorAfter: {count: 24, period: hour}
```

`period` is `minute`, `hour`, or `day`. `count` is a positive integer. Either
bound can be given on its own. Leaving the block out raises `missing-freshness`,
because a source nobody checks for staleness fails quietly: the pipeline stops
and the tables keep serving yesterday's numbers. A table that genuinely has no
schedule, such as a reference list someone updates by hand, should carry an
exemption saying so.

## Exemptions

A rule that is wrong for one specific contract can be exempted, but only
in writing. `modelgenExempt` opts out of one rule and demands a reason:

```yaml
schema:
  - name: user_signed_up
    customProperties:
      # Contract-scoped: suppresses findings that aren't about a column.
      - property: modelgenExempt
        value:
          - rule: invalid-raw-strategy
            reason: "strategy pinned by the upstream loader"
    properties:
      - name: internal_note
        # Column-scoped: suppresses findings about this column only.
        customProperties:
          - property: modelgenExempt
            value:
              - rule: missing-column-description
                reason: "deprecated in TJP-00042, drops next quarter"
```

The two scopes never cross. A contract-scoped exemption cannot silence a
finding about a column, and a column-scoped one cannot silence a
contract-scoped finding of the same code, nor a finding about a different
column.

An exemption **hides nothing**. `modelgen validate` still prints every
exempted finding, labelled `exempt` and followed by its reason, so a
standing exemption stays visible on every run instead of quietly becoming
permanent. What changes is that the finding no longer counts: it does not
block `gen`, `run`, or `check`, and it does not fail `--strict`.

A `rule` that matches nothing is inert. No error is raised, and the check it
names keeps firing, so a typo announces itself. An empty or whitespace `reason`
is an `invalid-exemption-reason` error and the exemption does not apply.
That error cannot itself be exempted.

## Strict mode

```bash
modelgen validate contract.yml --strict
```

Warnings become failures. Wire it into CI once the team has agreed that
things like undocumented columns should block a merge. Anything genuinely
exceptional can carry an exemption, so strict mode does not become a reason
to leave warnings switched off.
