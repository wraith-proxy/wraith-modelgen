[![Documentation](https://img.shields.io/badge/docs-live-brightgreen)](https://www.thomaspeoples.com/gitea-repos/wraith-modelgen/)
![PyPI - Version](https://img.shields.io/pypi/v/wraith-modelgen)
![PyPI - License](https://img.shields.io/pypi/l/wraith-modelgen)
![PyPI - Python Version](https://img.shields.io/pypi/pyversions/wraith-modelgen)
![Coverage](https://www.thomaspeoples.com/gitea-repos/wraith-modelgen/coverage.svg)

# 👻 wraith-modelgen
### *Sovereign dbt Scaffolding for the Ghost Stack*

**wraith-modelgen** is a dbt scaffolder. Feed it a YAML data contract (Open Data Contract Standard, ODCS) and it writes dbt models for BigQuery or DuckDB: an Origin (raw) layer that passes everything through, and a Consumption (staging) layer that holds the stable line for downstream consumers. It also writes the schema and sources yml for the tables your project produces and reads.

Source schemas change. Columns get renamed, types get tightened, and new fields appear without warning. In most analytics codebases that change lands downstream: dashboards break, FDPs fail, and the analytics team finds out from a support ticket.

wraith-modelgen makes the data contract the change-management mechanism. The source team updates it as part of their release. wraith-modelgen regenerates the models. Downstream sees what it sees. Nothing breaks unless something it actually depended on disappears, which triggers a generation failure with the column name.

---

## 🚀 Installation

```bash
uv tool install wraith-modelgen
```

BigQuery introspection needs Application Default Credentials:

```bash
gcloud auth application-default login
```

Everything except `new` also runs without them. `--from-contract` generates from the contract's declared columns alone, and `gen`, `run` and `check` fall back to it automatically when no credentials are found. That's how CI runs it. DuckDB contracts have the same escape hatch, but read the gotcha in the Targets and dialects section first.

---

## ⚙️ Workflow

```bash
modelgen init                                              # scaffold .modelgen.yml + dirs (once)
modelgen new my-project.landing.user_signups               # draft a contract from a live table
modelgen validate contracts/user_signups.yml               # offline semantic lint, all findings at once
modelgen run                                               # regenerate raw and staging for every contract
modelgen schema contracts/marts.odcs.yaml -o models/marts  # models yml for tables this project produces
modelgen sources contracts/extract.odcs.yaml -o models/staging  # sources yml for tables this project reads
modelgen check --diff                                      # CI: fail if committed models drifted
```

`run` does not write the schema or sources yml. Generate those with `schema` and `sources`, commit the output, and let `check` gate it.

Single layers when you need them. `--raw` and `--staging` are mutually exclusive:

```bash
modelgen gen contract.yml --raw     -o ./models/raw
modelgen gen contract.yml --staging -o ./models/staging
modelgen gen contract.yml --raw --dry-run  # preview without writing
```

| Command | What it does |
|---|---|
| `init` | Scaffold project layout: `.modelgen.yml`, `contracts/`, `models/` |
| `new` | Bootstrap a draft contract from a live BigQuery table, keys guessed and marked TODO |
| `validate` | Offline structural + semantic lint over every schema object; `--strict` promotes warnings to failures. A rule that's wrong for one contract can be exempted with `modelgenExempt`; a reason is mandatory, and exempted findings still print |
| `gen` | Generate one layer (`--raw`, `--staging`, or `--layer schema` / `--layer sources`) from one contract |
| `run` | Regenerate raw and staging for every contract under `.modelgen.yml` |
| `schema` | Write `_<contract-name>__models.yml` for tables the project produces |
| `sources` | Write `_<contract-name>__sources.yml` for tables the project reads (extract contracts) |
| `check` | Render in memory, diff against disk, exit non-zero on drift. Writes nothing. |

Full flags and exit codes are in the CLI reference section.

---

## ⌨️ CLI reference

Every command takes `--help`:

```bash
modelgen --help
modelgen check --help
```

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success. |
| `1` | A contract, config or generation error, drift, or a `--strict` failure. |
| `2` | Usage error: bad or conflicting flags, or a contract path that does not exist. |

### `gen`

Generates one layer from one contract. Specify exactly one of `--raw`, `--staging` or `--layer`.

```bash
modelgen gen contracts/bus-positions.odcs.yaml --staging -o models/staging
```

The command prints a panel per event with its target model, then a File and Status table with `written`, or `dry-run` with `--dry-run`. Exit `0`.

Generating the yml for produced or read tables uses `--layer`:

```bash
modelgen gen contracts/bus-marts.odcs.yaml --layer schema -o models/marts --dry-run
```

It prints one `<path> dry-run` line per file and exits `0`.

| Flag | Meaning |
|------|---------|
| `--raw` | Raw (Origin Data Product) layer. Everything in, nothing filtered. |
| `--staging` | Staging (Consumption Data Product) layer. |
| `--layer schema` | Models yml for produced tables. Same output as `modelgen schema`. |
| `--layer sources` | Sources yml for extract contracts. Same output as `modelgen sources`. |
| `-o, --output DIR` | Output directory. Default `./models`. |
| `--dry-run` | Preview without writing. |
| `--from-contract` | Skip BigQuery introspection and use the contract's staging declarations. Ignored under `--layer`. |
| `-t, --table NAME` | Generate only the named schema object. Repeatable. Default: every object. |
| `-s, --server ID` | Pick the server by its `server:` id. Not accepted with `--layer sources`. |
| `-v, --verbose` | Debug logging. |

`--layer` takes exactly `schema` or `sources`. It cannot be combined with `--raw` or `--staging`.

Exit codes for `gen`:

- `2`: neither or both of `--raw` and `--staging` given; `--layer` with `--raw` or `--staging`; an unknown `--layer` value; `--server` with `--layer sources`.
- `1`: the contract is invalid (including an unknown `--table` or `--server`); the contract has lint errors; generation or introspection failed.

Gotchas:

- `gen` has no `--strict`. Use `modelgen schema --strict` or `modelgen sources --strict` for that.
- `--from-contract` does nothing under `--layer`.

### `schema`

Writes one `_<contract-name>__models.yml` for tables the project produces. Quality rules that map to dbt tests land under `tests/generated/` inside the output directory. A property-level `rule: validValues` is not one of them. It is reported as unmapped here and becomes `accepted_values` only in the staging yml.

```bash
modelgen schema contracts/bus-marts.odcs.yaml -o models/marts --strict
```

Exit `0` when every quality rule maps to a dbt test. Unmapped rules are reported on stderr as `modelgen: N quality rule(s) not mapped: <model>.<rule>`.

| Flag | Meaning |
|------|---------|
| `-o, --output DIR` | Output directory. Default `./models`. |
| `--dry-run` | Preview without writing. |
| `-t, --table NAME` | Repeatable. Unknown names exit `1`. |
| `-s, --server ID` | Pick the server by its `server:` id. |
| `--strict` | Exit `1` when any quality rule cannot be mapped. |

Exit codes for `schema`:

- `1`: the contract is invalid (no schema objects, unknown `--table` or `--server`), or `--strict` is set and a quality rule is unmapped.

Gotchas:

- With `--strict`, files are still written before the exit `1`. With `--dry-run`, nothing is written but the exit `1` still applies.

### `sources`

Writes one `_<contract-name>__sources.yml` for tables the project reads (extract contracts). See the Sources layer section.

```bash
modelgen sources contracts/bus-positions-extract.odcs.yaml -o models/staging
```

Exit `0` on success. The flags match `schema`, with two differences: there is no `--server`, and `--strict` behaves the same way.

Exit codes for `sources`:

- `1`: the contract is invalid, or `--strict` is set and a quality rule is unmapped.
- `2`: `--server` was passed. The command does not accept it.

### `check`

Renders every contract that `.modelgen.yml` knows about and compares the result to the files on disk. It writes nothing. Run it in CI next to the linter.

```bash
modelgen check --diff
```

Clean repo, last line:

```
Everything in sync. 12 file(s) match their contracts.
```

Drifted repo: a `--- <path>` header and a unified diff for each drifted file, then `Drift detected: N file(s) out of sync, M error(s). Run modelgen run and commit the result.` and exit `1`. The count is illustrative. Run the command to see your own numbers.

`check` takes no contract path and no `--layer`, `--table` or `--strict`. It reads `.modelgen.yml`, found by walking up from the current directory, or the file named by `--config`.

| Flag | Meaning |
|------|---------|
| `-c, --config FILE` | Path to `.modelgen.yml`. Default: discovered upward from cwd. |
| `--diff` | Print a unified diff for every drifted file. |
| `--from-contract` | Skip BigQuery introspection for all contracts. |
| `-s, --server ID` | Pick the server by its `server:` id. Applies to schema and event contracts. Sources contracts ignore it. |
| `-v, --verbose` | Debug logging. |

Exit codes for `check`:

- `0`: everything is in sync, or no contracts matched the `contracts` glob and no `schema_contracts` or `sources_contracts` pattern is set (`No contracts found.`).
- `1`: drift or missing files; a contract fails to parse, lint or introspect; `.modelgen.yml` is malformed or not a YAML mapping; a `schema_contracts` or `sources_contracts` pattern matches no files. The last case fails even when other contracts exist.

Gotcha: `schema_contracts` and `sources_contracts` are only checked when `schema_output` and `sources_output` are set. Without them, matched contracts are skipped by the drift comparison and do not fail the run.

### Python API

`modelgen.generator` exposes `generate()`, `render_raw()` and `render_staging()`. Each takes a `contract_path` argument. It is accepted for compatibility with 0.14.1, ignored, and deprecated. Passing a non-`None` value emits a `DeprecationWarning` ("contract_path is ignored and will be removed"). The generated header no longer embeds the contract path, so drop the argument.

Gotchas:

- `contract_path` sits between `layer` and `dry_run` in `generate()`. Pass `dry_run` by keyword, or a positional `True` lands in `contract_path`.
- `render_schema()` in `modelgen.schema_mode` has no `contract_path` parameter.

---

## ⚙️ Configuration: `.modelgen.yml`

Put the file at the repo root. Every path in it is relative to the directory that holds the file. `run` and `check` find it by walking up from the current directory, or take `--config PATH` (`-c`). With no file found, the defaults apply and the root is the current directory. An empty file means defaults. A file whose top level is not a mapping is a config error (exit 1). Unknown keys are ignored without a warning, so a typo in a key name fails quietly.

| Key | Default | Read by | What it does |
|-----|---------|---------|--------------|
| `contracts` | `contracts/*.yml` | `run`, `check` | Glob of event contracts. Each one yields a raw model and, when it declares staging columns, a staging model. |
| `raw_output` | `models/raw` | `run`, `check` | Directory for raw models. |
| `staging_output` | `models/staging` | `run`, `check` | Directory for staging models. |
| `schema_contracts` | unset | `check` | Glob of contracts for tables this project produces. Rendered as `modelgen schema` would. |
| `schema_output` | unset | `check` | Directory the schema yml is compared against. |
| `sources_contracts` | unset | `check` | Glob of extract contracts for tables this project reads. Rendered as `modelgen sources` would. |
| `sources_output` | unset | `check` | Directory the sources yml is compared against. |
| `tests_output` | unset | `check` | Directory that replaces the leading `tests` of generated singular-test paths, so `tests/generated/x.sql` is compared at `<tests_output>/generated/x.sql` instead of under `schema_output`/`sources_output`. Pair it with `--tests-output` on `schema`/`sources`. Use it when the yml lives under `models/` but dbt must find the tests in its `tests/` directory. |

Complete example:

```yaml
# .modelgen.yml at the repo root. Paths are relative to this file.
contracts: contracts/events/*.yml
raw_output: models/raw
staging_output: models/staging

schema_contracts: contracts/produced/*.odcs.yaml
schema_output: models/marts

sources_contracts: contracts/extract/*.odcs.yaml
sources_output: models/staging
```

Gotchas, all from the code:

- `run` never writes schema or sources yml. Generate them with `modelgen schema` and `modelgen sources`, commit the output, and let `check` gate it.
- `schema_contracts` or `sources_contracts` set to a glob that matches nothing makes `check` exit 1 and name the key.
- A `schema_contracts` or `sources_contracts` glob without its `*_output` key is not checked, and `check` does not say so. Set both or neither.
- The default `contracts` glob is `*.yml`. ODCS files named `*.odcs.yaml` need their own glob.
- Keep extract (sources) contracts out of the `contracts` glob. Event contracts require `modelgenLoadedAtField` on each schema object. `run` and `check` report its absence as a `parse` error. Sources contracts do not carry that property.
- `gen`, `schema`, `sources` and `validate` never read this file. Pass their paths and flags explicitly.

---

## 📄 Contract anatomy

Contracts are [Open Data Contract Standard (ODCS)](https://github.com/bitol-io/open-data-contract-standard) documents, validated against the bundled ODCS JSON schema for their `apiVersion`. modelgen's BigQuery/dbt specifics ride in `customProperties`.

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
          freshness:                     # -> dbt source freshness
            warnAfter:  { count: 12, period: hour }
            errorAfter: { count: 24, period: hour }
      - property: modelgenStaging
        value:
          dataset: staging
          materialization: snapshot   # ds snapshot; derives partitionBy: ds
          clusterBy: [user_id]

    properties:
      - name: event_id                  # staging (consumer-facing) name
        physicalName: EVENT_ID          # source column name
        logicalType: string
        physicalType: STRING            # cast target
        description: "..."              # >= 50 chars, and not just the name
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
| `accepted_values` | property `quality` with `rule: validValues` + `validValues: [...]`. Staging yml only. |

`modelgen schema` and `modelgen sources` do not read that form. They report it as unmapped, so `--strict` exits 1 for it. Those layers map the ODCS library form instead: `type: library`, `metric: invalidValues`, `arguments.validValues` and `mustBe: 0`. `gen --staging` ignores the library form. A list that must reach both layers has to be written in both forms, and `schema --strict` fails on the `rule: validValues` one.

### Supported ODCS versions

The top-level `kind` must be `DataContract`. The `apiVersion` picks the bundled JSON Schema in `src/modelgen/schema/`.

| `apiVersion` | Result |
|---|---|
| `v3.2.0` | Supported. Validated against the v3.2.0 schema. |
| `v3.1.0` | Supported. Validated against the v3.1.0 schema. |
| `v3.0.2` | Supported. Validated against the v3.0.2 schema. This is also the default. |
| `v3.0.1`, `v3.0.0`, `v2.2.2`, `v2.2.1`, `v2.2.0` | Accepted. Validated against the v3.0.2 schema, whose `apiVersion` enum lists them. There is no separate schema for these, so they get v3.0.2 rules. |
| missing, or not a string | Validated against v3.0.2, which reports the problem. |
| anything else (for example `v2.3.0` or `v9.9.9`) | Rejected: `unsupported apiVersion`, followed by the list of supported versions (`v3.0.2, v3.1.0, v3.2.0`). |

Gotcha: `modelgen new` writes `apiVersion: v3.0.2`. Edit it if the contract should be newer.

---

## 🧭 Targets and dialects

A contract picks its dialect from the `type` of each `servers[]` entry. The dialect decides which keys locate the source and how SQL is rendered.

| `servers[].type` | Dialect | Where the source lives |
|---|---|---|
| `bigquery` | BigQuery | `project` and `dataset` keys on the server. |
| `duckdb` | DuckDB | First set of `database`, `path`, `location`, `server`. Schema is the `schema` key, default `main`. |
| `local` | DuckDB | Same keys as `duckdb`. With `format: parquet`, the location is a Parquet landing file that dbt-duckdb reads through the source's `external_location` meta. |

Any other type, such as `custom`, has no dialect. It is skipped when auto-selecting a server and rejected when named with `--server`.

```yaml
servers:
  - server: prod
    type: bigquery
    project: my-gcp-project
    dataset: landing
  - server: build-warehouse
    type: duckdb
    database: ./dbt/build.duckdb
    schema: main
  - server: extract-local
    type: local
    format: parquet
    path: dbt/data/bus_positions_*.parquet
```

DuckDB limits, enforced by lint or generation:

- Models are views unless the contract says `materialization: table`. Staging `incremental` passes lint but renders as a view.
- No partitions or clustering. Declaring `partitionBy` or `clusterBy` is a lint error. The derived `ds` partition of a snapshot is the one exception, and DuckDB never adds a `ds` column.
- No encryption. Lint reports classified columns as `dialect-unsupported-config`, and generation refuses them.

### Choosing a server

`--server ID` (short form `-s ID`) matches the `server:` value exactly. It works on `gen`, `schema`, `validate`, `run` and `check`. `sources` has no such flag, and `gen --layer sources` exits 2 when given one.

- Unknown ID: error that lists the server IDs in the contract.
- Known ID with an unsupported type: error that lists the supported types.
- No `--server`: the first `type: bigquery` server wins, even when it comes after a DuckDB one. If there is none, the first server whose type a dialect supports is used, in contract order. If there is none at all, the error lists the supported types: `bigquery, duckdb, local`.

Gotcha: when Application Default Credentials are found, `gen`, `run` and `check` build a BigQuery introspector whatever the dialect. `gen` asks it for the source columns before the dialect is applied (`render_raw` and `render_staging` in `src/modelgen/generator.py`). A DuckDB contract on a machine with GCP credentials therefore calls BigQuery unless you pass `--from-contract`. Whether that call succeeds depends on the BigQuery table and your access.

```bash
modelgen gen contracts/events/build_summary.yml --staging --from-contract -o models/staging
```

On success, `gen` prints a File and Status table of written files. The status column reads `written`, or `dry-run` with `--dry-run`.

---

## 📦 Sources layer

`modelgen sources` renders one `_<contract-name>__sources.yml` per extract contract. The filename uses the contract's `name`, not `dbtSourceName`. The file holds one `sources:` entry with `loaded_at_field`, `freshness`, a `tables:` list, and a column-set test (`dbt_expectations.expect_table_columns_to_match_set`). The consuming dbt project therefore needs the `dbt_expectations` package. Quality rules that map to singular tests go under `tests/generated/` of the output directory, or of `tests_output` when that is set (see Configuration).

Read the customProperties below from the contract's top level unless the table says otherwise.

| Property | Level | Effect |
|---|---|---|
| `dbtSourceName` | contract | Name of the dbt source. Default: the contract `name`. Must be a single value, not a list. |
| `dbtColumnSetTest` | contract | Name of the column-set test, replacing `dbt_expectations.expect_table_columns_to_match_set`. For projects whose sources are bare table functions (for example DuckDB `read_parquet(...)`), where the dbt_expectations test errors. The macro must take the same `column_list` argument. Single value. |
| `loadedAtField` | contract | Written as `loaded_at_field`. Optional. Passed through as written. |
| `freshnessWarnAfter` | contract | `freshness.warn_after`. Format `N minutes`, `N hours` or `N days`. |
| `freshnessErrorAfter` | contract | `freshness.error_after`. Same format. |
| `externalLocation` | schema object | `meta.external_location` on that table. Wins over the contract-level value. |
| `externalLocation` | contract | Used for the table when the contract has exactly one object. With more objects it is a contract error, even when `--table` narrows the output, so set it on each object instead. |

The freshness format is one whole number, at least one space, then a unit. Accepted units are `minute`, `minutes`, `hour`, `hours`, `day` and `days`, in any case. The count must be above zero. `2 days` is valid, `2days` and `0 days` are errors that name the property. A freshness block is written only when at least one bound is set.

Example extract contract, adapted from `tests/fixtures/real_contracts/bus-positions-extract.odcs.yaml` and trimmed. `dbtSourceName: bods` is added here so the source has a short name. The fixture omits it, and the source then takes the contract name.

```yaml
apiVersion: v3.2.0
kind: DataContract
id: 7c3f9a52-1e84-4b6d-a0c7-5d2e8f419b63
name: wraith_bus_extract_positions
version: 1.0.0
status: active

servers:
  - server: extract-local
    type: local
    format: parquet
    path: dbt/data/bus_positions_*.parquet

customProperties:
  - property: dbtSourceName
    value: bods
  - property: externalLocation
    value: "read_parquet('data/bus_positions_*.parquet', union_by_name=true)"
  - property: loadedAtField
    value: '"timestamp"'
  - property: freshnessWarnAfter
    value: 2 days
  - property: freshnessErrorAfter
    value: 5 days

schema:
  - id: bus_positions_extract_obj
    name: bus_positions
    physicalName: bus_positions_*.parquet
    logicalType: object
    physicalType: table
    description: Downsampled bus positions, one row per (timestamp, bus_id, operator, line).
    properties:
      - id: ex_timestamp
        name: timestamp
        logicalType: date
        physicalType: TIMESTAMP WITH TIME ZONE
        required: true
        description: End of the 3-minute bucket, UTC.
      - id: ex_bus_id
        name: bus_id
        logicalType: string
        physicalType: VARCHAR
        required: true
        description: Vehicle identifier.
```

Two things the example shows. The `format: parquet` line is the server's `format` key, which selects the DuckDB Parquet path. It is not the freshness format. The `loadedAtField` value includes its double quotes, because the code writes the string out unchanged.

```bash
modelgen sources contracts/extract/bus-positions-extract.odcs.yaml -o models/staging
```

Expected output, with the file name the code derives from the contract name:

```text
models/staging/_wraith_bus_extract_positions__sources.yml written
```

Add `--dry-run` to see the same line with `dry-run` in place of `written`. Add `--strict` to exit 1 when a quality rule cannot be mapped to a dbt test.

---

## 🔐 Classification and erasure

A property marked `classification: pii` or `psi` is **encrypted on the way
into staging** and never materialised in plaintext. Same seam as
`required: true` → `not_null`: the contract says it, the generator does it,
no human in the loop.

```yaml
      - property: modelgenSubjectField
        value: USER_ID                 # who the row is about

      - name: user_id
        physicalName: USER_ID
        classification: pii            # subject → DETERMINISTIC_ENCRYPT
      - name: email
        physicalName: EMAIL
        classification: psi            # payload → AEAD.ENCRYPT
```

The subject column is encrypted deterministically, so its ciphertext is
stable per keyset and joins, row counts and aggregates all survive erasure.
Everything else is randomised. Both resolve their keyset from a keystore
holding one wrapped key pair per subject.

This is BigQuery only. DuckDB has no encryption, so lint reports classified columns as `dialect-unsupported-config` and generation refuses them.

**Right to erasure is one DELETE.** Destroy the subject's keystore row and
every ciphertext written under those keys becomes unrecoverable, across
every table, without rewriting a single partition. A historical regulatory
position still reconstructs in aggregate; the individual does not.

Full design, key hierarchy (CMEK vs KEK vs DEK), erasure timeline and
citations: **[Keystore & Erasure](docs/bigquery/keystore.md)**.

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

## ✅ Contract standards

`modelgen validate --strict` is the gate. Beyond the structural checks, it
holds contracts to a few standards that only matter once more than one team
reads them:

| Standard | Rule |
|---|---|
| Every column is documented | `description` of at least 50 characters that says something the column name does not |
| Every source declares staleness | `modelgenRaw.freshness` with `warnAfter` / `errorAfter`, which becomes the dbt `freshness:` block |
| Every key is tested | `primaryKey` properties carry `required` and `unique` |
| Exceptions are written down | `modelgenExempt` opts out of one rule, and a `reason` is mandatory |

An exemption is scoped either to the whole contract or to a single column,
and the two never cross:

```yaml
      - name: internal_note
        customProperties:
          - property: modelgenExempt
            value:
              - rule: low-quality-column-description
                reason: "deprecated in TJP-00042, drops next quarter"
```

Exempted findings still print on every `validate` run, labelled `exempt` with
their reason, and the summary counts them. They stop blocking, they do not
stop being visible. Full rule list: **[Lint Rules](docs/linting.md)**.

---

## 🏗️ What gets generated

**For `--raw`:**
- `raw__event.sql`: dbt incremental model with `{{ source(...) }}`, optional dedup window, partition and cluster config.
- `raw__event.yml`: dbt sources entry plus model definition. Columns mirror the introspected source schema.

**For `--staging`:**
- `stg__event.sql`: dbt model reading `{{ ref('raw__event') }}`. Casts and renames applied.
- `stg__event.yml`: model definition with column tests from the contract.

**For `--layer schema` and `--layer sources`:** the yml described in the CLI reference section. Neither writes SQL.

The layouts above are for BigQuery. DuckDB models are views unless the contract says `materialization: table`, and they have no partitions. See the Targets and dialects section.

Staging defaults to a **datestamped daily snapshot**: `insert_overwrite`
partitioned on a generated `ds` column, one full copy of the dimension per
day. History is `where ds = '<date>'`, and any date backfills in parallel
with the same SQL. The orchestrator injects it:

```bash
dbt run --select stg__event --vars '{ds: 2026-07-27}'
```

The grain is (key, `ds`), so the generated `unique` test is scoped to the
partition the run writes. `materialization: incremental` on
`modelgenStaging` opts back into a high-water-mark load, and then
`incrementalStrategy` and `partitionBy` are yours to declare.

On BigQuery, both layers load incrementally. Raw merges on the unique key,
staging replaces one `ds` partition per run. Neither rebuilds the whole table,
and no generated BigQuery model emits `CREATE OR REPLACE TABLE ... AS SELECT`.
Full rebuild is available as a documented exception rather than a default,
which is covered in **[Tooling Decisions](docs/bigquery/decisions.md)**. DuckDB
models follow `materialization` instead: views by default, tables when the
contract says `materialization: table` (see Targets and dialects).

---

## ⬆️ Upgrading from 0.14.1

Read this before you bump the modelgen pin. The first `modelgen check` after the upgrade fails on every committed generated file until you regenerate them.

### What you will see

`modelgen check` reports drift on each file whose bytes changed. The final line has this shape (counts depend on your project):

```
Drift detected: <n> file(s) out of sync, <m> error(s). Run modelgen run and commit the result.
```

That advice is only partly right. `modelgen run` covers the raw and staging layers. It does not regenerate the schema or sources layers.

### Why every file drifts

The generated header changed.

- 0.14.1: the header carried a `Contract: <path>` line.
- Now: the first line of every generated file is `-- GENERATED by modelgen <version> from <name> <ver>. Do not edit.` in `.sql` files, and `# GENERATED by modelgen <version> from <name> <ver>. Do not edit.` in `.yml` files.

`<name>` is the contract name and `<ver>` is the contract version. When the contract declares no version, the ` <ver>` part is dropped. The contract path is gone, so moving a contract no longer changes its output. The header line cannot be turned off.

The header embeds the modelgen version, and `modelgen check` compares bytes. Each release that changes the version string makes every committed file drift again. Plan a regeneration in every upgrade PR.

The version comes from installed package metadata. If the distribution is not installed, for example when you run from an uninstalled source checkout, the version falls back to `0.0.0`. Files generated that way carry `modelgen 0.0.0` and will drift against a released install. There is no override flag. Do not commit output generated from an uninstalled tree.

### Regenerate

Step 1: raw and staging.

```bash
modelgen run
```

`run` introspects BigQuery when Application Default Credentials resolve. Otherwise it builds columns from the contract's staging declarations. Use the same mode that produced the committed files. The raw layer differs between modes because BigQuery returns every source column and the contract declares only some of them. Regenerating in the other mode commits a different raw file. Force contract-only mode with `modelgen run --from-contract`.

Step 2: schema and sources. `run` does not touch these layers. Regenerate them per contract:

```bash
modelgen schema contracts/bus-marts.odcs.yaml -o models/marts
modelgen sources contracts/extract.odcs.yaml -o models/staging
```

Equivalent forms: `modelgen gen <contract> --layer schema -o <dir>` and `modelgen gen <contract> --layer sources -o <dir>`.

Set `schema_contracts` and `schema_output` together, and `sources_contracts` and `sources_output` together. See the Configuration section.

Step 3: verify.

```bash
modelgen check --diff
```

Clean output ends with `Everything in sync. <n> file(s) match their contracts.` and exits 0. Drift, missing files or contract errors exit 1.

### Upgrade notes

- Output is byte-identical for the same inputs. No timestamps, no paths. That is tested for `gen --raw` and `gen --staging` with `--from-contract`. The BigQuery path is not covered by that test.
- ODCS quality rules are mapped per layer. `schema` and `sources` (and `gen --layer schema|sources`) map them through `src/modelgen/quality.py`: `invalidValues` with `arguments.validValues` and `mustBe: 0` becomes `accepted_values`, zero `nullValues` and `duplicateValues` become `not_null` and `unique`, and `rowCount` with a bound becomes a row-count test. Other thresholds and `type: sql` rules become singular tests under `tests/generated/`. Anything unmapped is reported, never dropped. `gen --staging` maps only property-level `rule: validValues` to `accepted_values` (`src/modelgen/contract.py`) and reports nothing it skips. `gen --raw` emits no quality tests. Review the generated tests after regeneration.
- `modelgen check` also checks the schema and sources layers when their config keys are set.
- Several schema objects per contract: `gen` writes one model per object, in contract order. Check model names if a contract has several objects.
- Pin `--server` if a contract lists several servers. Without it, the first `type: bigquery` server wins, then the first server of a supported type.
- `contract_path` on `render_raw`, `render_staging` and `generate` is ignored and warns. Stop passing it.

### Limitations

- The version falls back to `0.0.0` when the package is not installed.
- Generated quality tests call `dbt_expectations.*` macros. The consuming dbt project must install the dbt-expectations package. modelgen does not check for it.
- `modelgen run` does not regenerate the schema or sources layers.

---

## 🧪 Developer Quality Gate

```bash
# Clone and set up
git clone https://git.thomaspeoples.com/thomaspeoples/wraith-modelgen
cd wraith-modelgen
uv run poe setup        # creates .env if missing, syncs deps, installs pre-commit hooks

# The quality gate
uv run poe test         # pytest (interactive)
uv run poe test-ci      # pytest with coverage enforcement (≥80%)
uv run poe lint         # ruff check
uv run poe format       # ruff format
```

`poe setup` runs `tasks.py`. It writes a placeholder `.env` (`APP_NAME=ghost-template`, `DEBUG=true`) only when none exists, runs `uv sync --all-extras`, and installs pre-commit hooks only when a `.git` directory is present.

The test suite uses `FakeIntrospector` and runs without BigQuery credentials. Covers contract parsing, semantic linting, both layers, schema evolution scenarios, composite keys, REPEATED/RECORD types, determinism, drift detection, contract scaffolding, and error surfaces.

Golden files in `tests/golden` embed the package version as a literal. A version bump in this repo fails the golden tests until they are regenerated.

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
2. **Introspection at gen time.** The warehouse is the source of truth for column names and types. Drift is impossible because nothing is duplicated. Under `--from-contract`, the contract's declarations are the source of truth instead.
3. **Deterministic output.** Same contract + same source schema = byte-identical files. `modelgen check` turns this into a CI gate.
4. **Strict failure on missing columns.** No silent passes. If the contract references a column the source no longer has, generation fails with the column name.
5. **Standards are enforced, exceptions are written down.** A rule that is wrong for one contract is exempted in the contract itself, with a reason, and the exemption stays visible on every run.
6. **Dialects are explicit.** BigQuery and DuckDB ship in the box. A contract names its warehouse through `servers[].type`, and a new warehouse is a new dialect behind `get_dialect`.

---

*Part of the [Ghost Stack](https://git.thomaspeoples.com/thomaspeoples). Sovereign. Self-hosted. No nonsense.*
