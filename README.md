[![Documentation](https://img.shields.io/badge/docs-live-brightgreen)](https://www.thomaspeoples.com/gitea-repos/wraith-modelgen/)
![PyPI - Version](https://img.shields.io/pypi/v/wraith-modelgen)
![PyPI - License](https://img.shields.io/pypi/l/wraith-modelgen)
![PyPI - Python Version](https://img.shields.io/pypi/pyversions/wraith-modelgen)
![Coverage](https://www.thomaspeoples.com/gitea-repos/wraith-modelgen/coverage.svg)

# 👻 wraith-modelgen
### *Sovereign dbt Scaffolding for the Ghost Stack*

**wraith-modelgen** is a BigQuery dbt model scaffolder. Feed it a YAML data contract, get back a pair of sovereign data products: an Origin (raw) layer that passes everything through, and a Consumption (staging) layer that holds the stable line for downstream consumers.

Source schemas change. Columns get renamed. Types get tightened. New fields appear without ceremony. In most analytics codebases this is a downstream catastrophe: dashboards break, FDPs fail, the analytics team gets paged at 03:00, and someone files a ticket asking whether dbt itself is broken.

wraith-modelgen makes the data contract the change-management mechanism. The source team updates it as part of their release. wraith-modelgen regenerates the models. Downstream sees what it sees. Nothing breaks unless something it actually depended on disappears, which triggers a generation failure with the column name.

---

## 🚀 Installation

```bash
uv tool install wraith-modelgen
```

You'll also need Application Default Credentials for BigQuery introspection:

```bash
gcloud auth application-default login
```

---

## ⚙️ Workflow

```bash
modelgen gen contract.yml --raw     -o ./models/raw
modelgen gen contract.yml --staging -o ./models/staging
```

One layer per invocation. `--raw` and `--staging` are mutually exclusive.

```bash
modelgen validate contract.yml      # validate YAML without hitting BigQuery
modelgen gen contract.yml --raw --dry-run  # preview without writing
```

---

## 📄 Contract anatomy

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

---

## 🔄 Schema evolution

| Source change | What wraith-modelgen does | What you do |
|---|---|---|
| Adds a column | Flows into raw via `select *`. Invisible in staging. | Add it to staging when consumers need it. |
| Renames a column | Validation fails: column not found in source. | Update the `source:` field on that column. Also `unique_key` / `loaded_at_field` if applicable. |
| Retypes a column | Existing CAST in staging absorbs it (or fails loudly at query time if values are incompatible). | Update `type:` if the canonical type should change too. |
| Drops a column staging uses | Validation fails: column not found. | Either restore upstream or remove from staging contract. |

Validation runs as part of `modelgen gen --staging`. If it passes, the generated staging model still presents the same contract to downstream consumers.

---

## 🏗️ What gets generated

**For `--raw`:**
- `raw__event.sql`: dbt incremental model with `{{ source(...) }}`, optional dedup window, partition and cluster config.
- `raw__event.yml`: dbt sources entry plus model definition. Columns mirror the introspected source schema.

**For `--staging`:**
- `stg__event.sql`: dbt incremental model with `{{ ref('raw__event') }}`. Casts and renames applied.
- `stg__event.yml`: model definition with column tests from the contract.

---

## 🧪 Developer Quality Gate

```bash
# Clone and set up
git clone https://git.thomaspeoples.com/thomaspeoples/wraith-modelgen
cd wraith-modelgen
uv run poe setup        # syncs deps + installs pre-commit hooks

# The quality gate
uv run poe test         # pytest (interactive)
uv run poe test-ci      # pytest with coverage enforcement (≥80%)
uv run poe lint         # ruff check
uv run poe format       # ruff format
```

The test suite uses `FakeIntrospector` and runs without BigQuery credentials. Covers contract parsing, both layers, schema evolution scenarios, composite keys, REPEATED/RECORD types, determinism, and error surfaces.

### Committing

All commits go through `commitizen` with the Ghost Stack convention:

```
👻 <type>/<ticket>: <message>
```

```bash
uv run cz commit
```

---

## 📜 Sovereign Principles

1. **One layer per invocation.** Layers have different lifecycles; conflating them makes things harder to reason about.
2. **Introspection at gen time.** The warehouse is the source of truth for column names and types. Drift is impossible because nothing is duplicated.
3. **Deterministic output.** Same contract + same source schema = byte-identical files. CI can diff against committed output to catch drift.
4. **Strict failure on missing columns.** No silent passes. If the contract references a column the source no longer has, generation fails with the column name.
5. **BigQuery only.** The introspection module is BQ-native. Add a different `Introspector` implementation if you need another warehouse.

---

*Part of the [Ghost Stack](https://git.thomaspeoples.com/thomaspeoples). Sovereign. Self-hosted. No nonsense.*
