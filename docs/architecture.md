# 👻 Architecture

wraith-modelgen is a pipeline: contract in, sovereign dbt models out. Two layers. One direction. No feedback loops, no hidden state, no surprises at 3am.

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

    subgraph contract.py ["contract.py — parse + validate"]
        B
    end
    subgraph introspect.py ["introspect.py — live schema from BQ"]
        C
    end
    subgraph generator.py ["generator.py — Jinja2 rendering + file write"]
        D
        E
    end
```

---

## Two-layer model

The layers have different owners and different jobs. Conflating them is how you end up with a 3am page.

```mermaid
flowchart LR
    SRC[("BigQuery\nsource table")] -->|"select *"| RAW["raw__event\n──────────────\nOrigin Data Product\nowned by source team"]
    RAW -->|"{{ ref(...) }}"| STG["stg__event\n──────────────\nConsumption Data Product\nowned by analytics"]
    STG --> DOWN["Downstream\nconsumers"]

    CTR["📄 contract.yml"] -. "introspects schema\nat gen time" .-> SRC
    CTR -. "declares columns,\ncasts, renames" .-> STG
```

**Raw** is a `select *` pass-through. It mirrors the source schema exactly. New source columns flow in automatically. The source team can add columns without telling anyone.

**Staging** is the consumer contract. Explicit column selection, renames, and casts. Schema drift is absorbed here — downstream consumers never see source column names, only the stable contract names.

---

## Schema evolution

Source schemas change. This is not a problem wraith-modelgen solves — it's a problem wraith-modelgen makes visible before it becomes your problem.

```mermaid
flowchart TD
    GEN["modelgen gen --staging"] --> VAL["Validate: all staging contract columns\npresent in source schema?"]
    VAL -->|"yes"| RENDER["Render staging model\ncast + rename all declared columns"]
    VAL -->|"no — column missing"| FAIL["❌ GenerationError\n'column X not found in source'\nFix the contract or fix the source"]
    RENDER --> NOTIFY{"New source columns\nnot in staging contract?"}
    NOTIFY -->|"yes"| WARN["⚠ Schema evolution notice\ncolumn names printed to output\nnot a failure — raw absorbs them"]
    NOTIFY -->|"no"| DONE["✅ Files written"]
    WARN --> DONE
```

| Source change | What happens | What you do |
|---|---|---|
| Adds a column | Flows into raw automatically. Invisible in staging. | Add to staging when consumers need it. |
| Renames a column | Validation fails: column not found in source. | Update the `source:` field. Also `unique_key` / `loaded_at_field` if applicable. |
| Retypes a column | Existing `CAST` absorbs it or fails loudly at query time. | Update `type:` if the canonical type changes. |
| Drops a column staging uses | Validation fails: column not found. | Restore upstream or remove from staging contract. |

---

## Module map

| Module | Responsibility |
|---|---|
| `contract.py` | YAML parsing, dataclasses, validation. The contract is law. |
| `introspect.py` | BigQuery schema fetch. `FakeIntrospector` for tests and offline use. |
| `generator.py` | Jinja2 rendering, file writing, schema evolution logic. |
| `cli.py` | Typer commands, Rich output, error handling. The face. |
| `src/templates/` | Jinja2 templates for the four generated artefacts. |

---

## Design decisions

**Deterministic output.** Same contract + same source schema = byte-identical files. CI can diff generated output against committed output to catch contract drift without running a BigQuery query.

**Introspection at gen time, not contract time.** The contract only declares what the consumer cares about. Raw column metadata comes from the live source schema. The warehouse is the source of truth for column names and types — the contract never needs to duplicate that.

**Strict failure on missing columns.** No silent passes. If the contract references a column the source no longer has, generation fails with the column name. A failed build is better than a broken pipeline nobody noticed.

**BigQuery only, by design.** The introspection module is BQ-native. Not a limitation — a choice. Generic warehouse support adds complexity for no benefit in this stack. Add a different `Introspector` implementation if you need another warehouse.
