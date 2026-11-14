# wraith-modelgen

Turns ODCS data contracts into dbt files. This glossary fixes the words the code, CLI and docs use.

## Language

### Contracts

**Contract**:
An ODCS document describing one or more tables. Everything modelgen generates comes from a contract.
_Avoid_: schema (for a whole contract), spec

**Event contract**:
A contract for a table the warehouse already receives. modelgen generates its raw and staging models. Requires `modelgenLoadedAtField`. In config: `contracts`.
_Avoid_: source contract

**Produced contract**:
A contract for a table the dbt project builds. modelgen generates the models yml and the tests mapped from its quality rules. CLI and config call it `schema` (`modelgen schema`, `schema_contracts`).
_Avoid_: schema contract (as the concept name)

**Extract contract**:
A contract for a table the dbt project reads but does not build, such as a Parquet extract. modelgen generates the sources yml. CLI and config call it `sources` (`modelgen sources`, `sources_contracts`).
_Avoid_: sources contract (as the concept name), source contract

### Rendering

**Layer**:
One tier of generated SQL models: raw or staging.
_Avoid_: mode, level

**Mode**:
Which kind of contract is being rendered: event, produced or extract. Event mode yields layers; produced and extract mode yield yml and tests only. The `--layer schema` and `--layer sources` flags select a mode; the flag name is historical.
_Avoid_: layer (for the kind of contract)

**Drift**:
A committed generated file that differs from what its contract renders now. `modelgen check` reports it.
_Avoid_: out of sync (as a noun)

### Warehouses

**Server**:
One `servers[]` entry in a contract: where a table lives.
_Avoid_: target, connection

**Dialect**:
The SQL and type system of a warehouse, picked by a server's `type`. BigQuery and DuckDB are the dialects.
_Avoid_: engine, adapter (a dialect is the thing that varies; "adapter" is reserved for code-structure talk)

**Contract-only introspection**:
Taking a source table's columns from the contract's declarations instead of querying the warehouse. Forced with `--from-contract`; automatic when no BigQuery credentials exist.
_Avoid_: offline mode, contract-only mode (clashes with **Mode**)

### Privacy

**Subject**:
The person a row is about, named by `modelgenSubjectField`. Its column is encrypted deterministically so joins survive erasure.
_Avoid_: user, customer

**Classification**:
A property marked `pii` or `psi`. A classified column is encrypted on the way into staging.
_Avoid_: sensitivity, tag
