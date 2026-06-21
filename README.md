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
modelgen init                                  # scaffold .modelgen.yml + dirs (once)
modelgen new my-project.landing.user_signups   # draft a contract from a live table
modelgen validate contracts/user_signups.yml   # offline semantic lint, all findings at once
modelgen run                                   # regenerate every layer of every contract
modelgen check --diff                          # CI: fail if committed models drifted
```

Single layers when you need them — `--raw` and `--staging` are mutually exclusive:

```bash
modelgen gen contract.yml --raw     -o ./models/raw
modelgen gen contract.yml --staging -o ./models/staging
modelgen gen contract.yml --raw --dry-run  # preview without writing
```

| Command | What it does |
|---|---|
| `init` | Scaffold project layout: `.modelgen.yml`, `contracts/`, `models/` |
| `new` | Bootstrap a draft contract from a live BigQuery table, keys guessed and marked TODO |
| `validate` | Offline structural + semantic lint; `--strict` promotes warnings to failures |
| `gen` | Generate one layer from one contract |
| `run` | Regenerate every layer of every contract under `.modelgen.yml` |
| `check` | Render in memory, diff against disk, exit non-zero on drift. Writes nothing. |

---

## 📄 Contract anatomy

Contracts are [Open Data Contract Standard (ODCS) v3.0.2](https://github.com/bitol-io/open-data-contract-standard) documents, validated against the bundled ODCS JSON schema. modelgen's BigQuery/dbt specifics ride in `customProperties`.

```yaml
version: "1.0.0"
kind: DataContract
apiVersion: v3.0.2
id: 8b1f6c2e-5a3d-4e7a-9f21-3c4d5e6f7a8b
status: active
name: user_signed_up

servers:
  - server: landing                    # source project + dataset
    type: bigquery
    project: my-gcp-project
    dataset: landing

schema:
  - name: user_signed_up               # the entity → dbt model stem
    physicalName: user_signups_raw      # source table
    physicalType: table

    customProperties:
      - property: modelgenLoadedAtField  # SOURCE column for incremental loads
        value: RECEIVED_AT
      - property: modelgenRaw
        value:
          dataset: raw
          incrementalStrategy: merge
          dedup: true                    # row_number() partition by primaryKey
          partitionBy: { field: RECEIVED_AT, dataType: timestamp, granularity: day }
          clusterBy: [USER_ID]
      - property: modelgenStaging
        value:
          dataset: staging
          incrementalStrategy: merge
          partitionBy: { field: received_at, dataType: timestamp, granularity: day }
          clusterBy: [user_id]

    properties:
      - name: event_id                  # staging (consumer-facing) name
        physicalName: EVENT_ID          # source column name
        logicalType: string
        physicalType: STRING            # cast target
        description: "..."
        primaryKey: true                # contributes to dbt unique_key
        primaryKeyPosition: 1
        required: true                  # → not_null test
        unique: true                    # → unique test
      # accepted_values → quality: [{ type: library, rule: validValues, validValues: [...] }]
```

| dbt test | ODCS field |
|---|---|
| `not_null` | property `required: true` |
| `unique` | property `unique: true` |
| `accepted_values` | property `quality` with `rule: validValues` + `validValues: [...]` |

---

## 🔄 Schema evolution

| Source change | What wraith-modelgen does | What you do |
|---|---|---|
| Adds a column | Does not appear until you regenerate (`modelgen run`). Invisible in staging until declared in the contract. | Regenerate, then add to staging when consumers need it. |
| Renames a column | Validation fails: column not found in source. | Update the property's `physicalName`. The `modelgenLoadedAtField` too, if applicable. |
| Retypes a column | Existing CAST in staging absorbs it (or fails loudly at query time if values are incompatible). | Update the property's `physicalType` if the canonical type should change too. |
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

The test suite uses `FakeIntrospector` and runs without BigQuery credentials. Covers contract parsing, semantic linting, both layers, schema evolution scenarios, composite keys, REPEATED/RECORD types, determinism, drift detection, contract scaffolding, and error surfaces.

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
3. **Deterministic output.** Same contract + same source schema = byte-identical files. `modelgen check` turns this into a CI gate.
4. **Strict failure on missing columns.** No silent passes. If the contract references a column the source no longer has, generation fails with the column name.
5. **BigQuery only.** The introspection module is BQ-native. Add a different `Introspector` implementation if you need another warehouse.

---

*Part of the [Ghost Stack](https://git.thomaspeoples.com/thomaspeoples). Sovereign. Self-hosted. No nonsense.*
