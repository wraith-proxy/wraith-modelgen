# Architecture

## Pipeline

```
contract.yml
    │
    ▼
EventContract.from_file()          # contract.py — parse + validate YAML
    │
    ▼
BigQueryIntrospector.get_columns() # introspect.py — fetch live schema from BQ
    │
    ▼
render_raw() / render_staging()    # generator.py — Jinja2 → SQL + YAML strings
    │
    ▼
generate()                         # generator.py — write files or dry-run
```

## Key design decisions

**Deterministic output.** Given the same contract and source schema, modelgen produces byte-identical files. CI can diff generated files against committed ones to catch contract drift.

**Two-layer model.** Raw is owned by the source team and passes through everything. Staging is the stable consumer contract. Schema drift is absorbed at the boundary between them, not propagated downstream.

**Introspection at gen time.** The contract only declares what the consumer cares about. The raw layer's column list comes from the live source schema, not from the contract. This means new source columns appear in raw automatically and don't require a contract update.

**Fail fast on staging drift.** If the source drops a column that staging references, generation fails immediately with a clear error. Silent breakage is worse than a failed build.

## Module map

| Module | Responsibility |
|---|---|
| `contract.py` | YAML parsing, dataclasses, validation |
| `introspect.py` | BigQuery schema fetch; `FakeIntrospector` for tests |
| `generator.py` | Jinja2 rendering, file writing, schema evolution logic |
| `cli.py` | Typer commands, Rich output, error handling |
| `src/templates/` | Jinja2 templates for SQL and YAML artefacts |
