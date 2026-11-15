# Python API

Every name below, importable from the top-level `modelgen` package, is public and covered by the compatibility policy; every other name in the package is internal and may change in any release.

`__version__` is the installed version of wraith-modelgen as a string (`0.0.0` when running from an uninstalled source tree).

### ModelgenError

The base class of every error modelgen raises on purpose. Catch it to handle any modelgen failure without naming each error type.

### ContractError

Raised when a contract is malformed or references invalid state. It is also a `ValueError`.

### ConfigError

Raised when `.modelgen.yml` is malformed. It is also a `ValueError`.

### GenerationError

Raised when generation fails for a reason other than contract parsing, such as a contract column the warehouse table does not have. It is also a `RuntimeError`.

### IntrospectionError

Raised when the warehouse cannot be queried or the source table does not exist. It is also a `RuntimeError`.

### EventContract

One table of an ODCS contract, parsed and validated. Load it with `EventContract.from_file` or `from_text` for a single-table contract, or `from_file_all` and `from_text_all` for every table in one.

### Finding

One result of linting a contract: its `severity`, rule `code`, `message`, and the staging `column` it concerns (empty for the whole contract).

### Severity

How serious a `Finding` is: `Severity.ERROR` or `Severity.WARNING`.

### lint_contract

Runs the semantic lint rules over an `EventContract` and returns a list of `Finding`s. See [Lint Rules](linting.md).

### Layer

The model layer to generate: `Layer.RAW` or `Layer.STAGING`.

### generate

Renders one layer of dbt files for an `EventContract`, using an `Introspector` for the source columns, and writes them under an output directory (or only renders them with `dry_run=True`).

### ModelgenConfig

The project settings read from `.modelgen.yml`. Load it with `ModelgenConfig.from_file` or find it upwards from a directory with `ModelgenConfig.discover`.

### Introspector

The protocol for reading a source table's columns from a warehouse: one method, `get_columns(project, dataset, table)`. Implement it to give `generate` columns from somewhere other than BigQuery.
