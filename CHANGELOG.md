# Changelog

All notable changes to `wraith-modelgen` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---


Initial commit. Ghost Stack project scaffold. Nothing generated yet.

## v0.19.0 (2026-11-24)

### Bug Fixes

- share rule numbering between map_object and staging_unmapped' (#41) from feature/TJP-34111 into main
- share rule numbering between map_object and staging_unmapped
- reject empty glob keys in .modelgen.yml
- merge feature/TJP-34111
- report unmapped quality rules on event staging
- docs claim no more than contract-only introspection
- merge feature/TJP-34111
- merge feature/TJP-38111
- reject non-string .modelgen.yml values as config errors
- layer words in docs, README and example contract
- name the taxonomy fixtures for what they hold
- layer words in CLI help and module docstrings
- layer words in generated raw and staging output
- guard against the layer taxonomy
- warn on unknown and orphaned .modelgen.yml keys
- restamp golden and model headers to 0.18.0

### Features

- map both validValues forms in every mode
- warn when the dbt project lacks dbt_expectations

## v0.18.0 (2026-11-21)

### Bug Fixes

- refresh example model headers to 0.17.0' (#33) from feature/TJP-21111-duckdb-pseudonymise into main
- refresh example model headers to 0.17.0
- refresh golden version headers to 0.17.0

### Features

- pseudonymise classified columns on DuckDB

## v0.17.0 (2026-11-15)

### Breaking Changes

- validate --format json; diagnostics go to stderr (#27)
- each failure class has its own exit code (#25)

### Bug Fixes

- address review findings on the integration surface' (#32) from feature/integration-surface into main
- address review findings on the integration surface
- changelog patterns match commits with an emoji prefix (#30)

### Features

- compatibility policy; legacy ODCS versions deprecated (#31)
- export Issue, ExitCode and the project API from the package root (#29)
- --format json for gen, schema, sources, run and check (#28)
- run and check return a result object from one shared loop (#21)
- report every schema error with its location (#26)
- declare the public Python API (#29)
- treat contracts as untrusted input

## v0.16.0 (2026-11-14)

## v0.15.0 (2026-11-14)

## v0.14.1 (2026-10-27)

## v0.14.0 (2026-10-26)

## v0.13.0 (2026-07-28)

## v0.12.0 (2026-07-27)

## v0.11.0 (2026-06-21)

## v0.10.0 (2026-06-12)

## v0.9.0 (2026-05-20)

## v0.8.0 (2026-05-20)

## v0.7.0 (2026-05-20)

## v0.6.0 (2026-05-18)

## v0.5.0 (2026-05-17)

## v0.4.0 (2026-05-16)

## v0.3.0 (2026-05-16)

## v0.2.0 (2026-05-16)

## v0.1.0 (2026-05-16)
