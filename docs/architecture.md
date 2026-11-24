# 👻 Architecture

wraith-modelgen is a pipeline: contract in, dbt files out. Two model layers (raw, staging) plus two yml-only modes (schema, sources). One direction.

---

## Generation pipeline

```mermaid
flowchart TD
    A["📄 contract.yml"] --> B["EventContract.from_file()"]
    B --> C["BigQueryIntrospector.get_columns()"]
    C --> D["render_raw() / render_staging()"]
    D --> E["generate()"]
    E --> F["raw__event.sql\nraw__event.yml"]
    E --> G["stg__event.sql\nstg__event.yml"]

    A --> S["select_schema_objects()\ncontract.py"]
    S --> H["render_schema()\nschema_mode.py"]
    S --> I["render_sources()\nsources_mode.py"]
    H --> E["emit()\nyml_mode.py"]
    I --> E
    E --> J["_name__models.yml\n+ tests/generated"]
    E --> K["_name__sources.yml\n+ tests/generated"]
    L["quality.py"] -.-> H
    L -.-> I

    subgraph contract.py ["contract.py: parse + validate"]
        B
    end
    subgraph introspect.py ["introspect.py: live schema from BQ"]
        C
    end
    subgraph generator.py ["generator.py: Jinja2 rendering + file write"]
        D
        E
    end
```

---

## Two-layer model

The layers have different owners and different jobs.

```mermaid
flowchart LR
    SRC[("BigQuery\nsource table")] -->|"explicit columns\n(introspected)"| RAW["raw__event\nRaw layer\nowned by source team"]
    RAW -->|"{{ ref(...) }}"| STG["stg__event\nStaging layer\nowned by analytics"]
    STG --> DOWN["Downstream\nconsumers"]

    CTR["📄 contract.yml"] -. "introspects schema\nat gen time" .-> SRC
    CTR -. "declares columns,\ncasts, renames" .-> STG
```

**Raw** mirrors the source schema, with columns introspected explicitly at generation time. New source columns appear after the next `modelgen run`, not automatically. The source team can add columns without coordinating with analytics, but a regeneration is required to pick them up.

**Staging** is the consumer contract. Explicit column selection, renames, and casts. Schema drift is absorbed here; downstream consumers only see the stable contract names. It materialises as a datestamped daily snapshot by default, one full copy of the dimension per `ds` partition, so history is a partition filter and backfills are independent of each other.

---

## Schema evolution

wraith-modelgen surfaces schema changes at generation time, before they affect production.

```mermaid
flowchart TD
    GEN["modelgen gen --staging"] --> VAL["Validate: all staging contract columns\npresent in source schema?"]
    VAL -->|"yes"| RENDER["Render staging model\ncast + rename all declared columns"]
    VAL -->|"no: column missing"| FAIL["GenerationError\n'column X not found in source'\nUpdate the contract or restore the source column"]
    RENDER --> NOTIFY{"New source columns\nnot in staging contract?"}
    NOTIFY -->|"yes"| WARN["Schema evolution notice\ncolumn names printed to output\nnot a failure; raw absorbs them"]
    NOTIFY -->|"no"| DONE["Files written"]
    WARN --> DONE
```

| Source change | What happens | What you do |
|---|---|---|
| Adds a column | Flows into raw automatically. Invisible in staging. | Add to staging when consumers need it. |
| Renames a column | Validation fails: column not found in source. | Update the property's `physicalName`. Also `modelgenLoadedAtField` if applicable. |
| Retypes a column | Existing `CAST` absorbs it or fails loudly at query time. | Update the property's `physicalType` if the canonical type changes. |
| Drops a column staging uses | Validation fails: column not found. | Restore upstream or remove from staging contract. |

---

## Module map

| Module | Responsibility |
|---|---|
| `contract.py` | YAML parsing, dataclasses, validation. |
| `introspect.py` | BigQuery schema fetch, and the contract-only introspector for offline use. |
| `generator.py` | Jinja2 rendering, file writing, schema evolution logic. |
| `dialect.py` | Warehouse seam: types, casts, `config()` rendering, partition support. BigQuery and DuckDB. Chosen by `servers[].type`. |
| `schema_mode.py` | Produced mode: models yml for tables the project produces (`modelgen schema`). |
| `sources_mode.py` | Extract mode: sources yml for tables the project reads (`modelgen sources`). |
| `yml_mode.py` | What the two yml modes share: the filename slug, `customProperties` as a mapping, and `emit()`, which dumps the yml and merges singular tests and unmapped rules. Object selection (`select_schema_objects`) lives in `contract.py`, which `EventContract` shares. |
| `quality.py` | Maps ODCS quality rules to dbt tests for the schema and sources modes. Reports what it cannot map. |
| `config.py` | `.modelgen.yml` loading for `run` and `check`. |
| `scaffold.py` | `init` project layout and the `new` draft contract. |
| `cli.py` | Typer commands, Rich output, error handling. |
| `lint.py` | Semantic checks on the parsed contract. All findings in one pass. |
| `src/modelgen/templates/` | Jinja2 templates for the four generated artefacts. Must live inside the package or the wheel ships without them. |

---

## Design decisions

**Deterministic output.** Same contract + same source schema = byte-identical files. No timestamps, no paths. CI can diff generated output against committed output (`modelgen check`) to catch contract drift without running a BigQuery query. The header embeds the modelgen version, so an upgrade drifts every file until regenerated.

**Introspection at gen time, not contract time.** The contract only declares what the consumer cares about. Raw column metadata comes from the live source schema. The warehouse is the source of truth for column names and types; the contract never duplicates them.

**Strict failure on missing columns.** If the contract references a column the source no longer has, generation fails with the column name. A failed build is better than a broken pipeline that nobody noticed.

**BigQuery introspection only, by design.** The introspection module is BQ-native. Unless `--introspect` is passed and credentials exist, columns come from the contract (`ContractIntrospector`) for any dialect. Warehouse SQL lives behind the dialect layer (`dialect.py`), which ships BigQuery and DuckDB. A new warehouse needs a dialect, and a live-schema `Introspector` if you want it to read columns.
