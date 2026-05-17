# Changelog

All notable changes to `wraith-modelgen` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [0.5.0] - 2026-05-17

### Added

- Coverage badge in `README.md` and `docs/index.md`; rendered from `docs/coverage.svg` via the pages CI pipeline.
- `mkdocs.yml`: `pymdownx.superfences` with Mermaid custom fence, `admonition`, and `pymdownx.details` extensions enabled.
- `docs/architecture.md`: Three Mermaid diagrams (generation pipeline with module subgraphs, two-layer model, schema evolution decision tree). ASCII art removed.
- `docs/usage.md`: Full workflow Mermaid flowchart. `modelgen run` was undocumented; added with options table, `.modelgen.yml` reference, and examples.
- `docs/reference.md`: Contract structure `classDiagram`. Generated file sections expanded with real example SQL. Type mapping table for REPEATED/RECORD modes.
- `docs/security.md`: Credential resolution replaced with Mermaid flowchart. Second diagram showing what wraith-modelgen accesses vs. never accesses (row data). Vulnerability reporting section added.
- `CHANGELOG.md`: Reconstructed from git history with accurate per-version content, no duplicate stubs, no empty commitizen entries.

### Changed

- CLI help text updated throughout. Commands now describe what they actually do.
- `modelgen gen`: flags and docstring clarified; error message updated.
- `modelgen validate`: docstring rewritten to explain offline behaviour.
- `modelgen run`: docstring updated; inline comments cleaned up.
- Version output: `👻 modelgen 0.x.x`.
- Rich panels upgraded with subtitles and aligned keys. `⚠` added to schema evolution warnings.
- Error messages include remediation hints.
- `.pre-commit-config.yaml`: `check-yaml` gets `--unsafe` flag to allow `!!python/name:` Python YAML tags in `mkdocs.yml`.
- All em-dashes replaced with standard punctuation throughout docs, CLI, and CHANGELOG. Contrarian and AI-pattern phrasing removed from help text, docstrings, and prose.

---

## [0.4.0] - 2026-05-16

### Changed

- `pyproject.toml` project URLs corrected: Repository gets `.git` suffix, Documentation points to live MkDocs site, Changelog URL uses Gitea's `src/branch/main/` path.

---

## [0.3.0] - 2026-05-16

Ghost Stack branding pass. The tool already worked.

### Changed

- `README.md` rewritten with Ghost Stack voice, sovereign principles section, schema evolution table, and contract anatomy reference.
- All docs pages (`architecture.md`, `index.md`, `reference.md`, `security.md`, `usage.md`) updated to match.
- `pyproject.toml` description, keywords, and classifiers tightened.

---

## [0.2.0] - 2026-05-16

### Added

- `genbadge[coverage]` dependency; CI now generates `docs/coverage.svg` from `coverage.xml` on every pages deploy.
- Coverage badge SVG committed to `docs/` so it is available in the built site immediately.

---

## [0.1.0] - 2026-05-16

First proper release. Not backwards compatible with any pre-release state.

### Added

- **BigQuery-native introspection.** `google-cloud-bigquery` replaces SQLAlchemy. Introspection runs at `gen` time; no standalone `introspect` command, no warehouse-specific extras.
- **Two-layer generation.** `--raw` and `--staging` are mutually exclusive flags. One layer per invocation; `--layer all` is gone.
  - Raw: `select *` pass-through incremental model. New source columns flow through automatically.
  - Staging: explicit column selection with renames, casts, and `not_null`/`unique` tests from the contract.
- **Contract format.** `unique_key` and `loaded_at_field` name source-side columns. Staging columns declare both `source:` and `name:` explicitly.
- **`modelgen run` command.** Discovers all contracts via `.modelgen.yml` (or opinionated defaults) and regenerates every layer.
- **`modelgen validate` command.** Offline contract validation (no BigQuery credentials needed).
- **`partition_by` / `cluster_by`** config per layer (BigQuery-specific).
- **Composite `unique_key`** support: single string or YAML list.
- **Schema drift validation.** Contract references a column not found in source; generation fails with the column name. No silent passes.
- **Schema evolution notifications.** New source columns not yet in staging are reported in CLI output.
- **`IntrospectedColumn.dbt_data_type`** handles REPEATED mode (wraps in `ARRAY<>`) and RECORD type (renders as `STRUCT`).
- **Determinism guarantee.** Same contract + same source schema = byte-identical output files. CI can diff committed output to catch drift.
- **`FakeIntrospector`** for tests and offline development. Full test suite runs without BigQuery credentials.
- **Structured error types:** `ContractError`, `GenerationError`, `IntrospectionError`.
- **Rich CLI output.** Panels, tables, and colour via `rich`. `--verbose` / `-v` for debug logging.
- **CI/CD.** Gitea Actions for test-and-verify, pages deploy (MkDocs + coverage badge), and sovereign release (commitizen version bump).
- **`CONTRIBUTING.md`**, `LICENSE` (MIT), CODEOWNERS, PR template.

---

## [0.0.1] - 2026-05-16

Initial commit. Ghost Stack project scaffold. Nothing generated yet.
