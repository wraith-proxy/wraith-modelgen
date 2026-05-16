# Changelog

## 0.3.0

Architectural rebuild. Not backwards compatible with 0.2.x contracts.

### Changed

- **BigQuery-only.** Dropped SQLAlchemy in favour of `google-cloud-bigquery`. Removed warehouse-specific extras.
- **Introspection is now part of `gen`.** Removed the standalone `introspect` command. Contracts are now minimal; column metadata comes from the warehouse at generation time.
- **Layer responsibility swap.** Raw is now a `select *` pass-through. Casts and renames live in staging where they belong.
- **One layer per invocation.** `--raw` and `--staging` are mutually exclusive flags; the `--layer all` option is gone.
- **Contract format.** `unique_key` and `loaded_at_field` now name source-side columns. Staging columns declare both `source:` and `name:` explicitly.

### Added

- `partition_by` and `cluster_by` config per layer (BQ-specific).
- Composite `unique_key` support (single string or list).
- Schema drift validation: contract references missing from source fail loudly with column names.
- Schema evolution notification: new source columns reported in CLI output.
- `IntrospectedColumn.dbt_data_type` handles REPEATED mode (wraps in `ARRAY<>`) and RECORD type (renders as STRUCT).
- Contract version validation.
- Determinism guarantee: same contract + same source = byte-identical output.
- `FakeIntrospector` for tests and offline development.
- Structured error types: `ContractError`, `GenerationError`, `IntrospectionError`.

## 0.2.0

Three-layer generation (landing -> raw -> staging) with SQLAlchemy-based introspection. Superseded.

## 0.1.0

Initial release. Single-layer staging model from YAML contract.

## v0.2.0 (2026-05-16)

## v0.1.0 (2026-05-16)
