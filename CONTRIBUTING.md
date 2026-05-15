# Contributing to modelgen

## Prerequisites

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) for dependency management

## Setup

```bash
git clone https://github.com/thomaspeoples/modelgen
cd modelgen
uv sync --all-extras
uv run pre-commit install
```

## Running tests

```bash
uv run pytest
```

The suite runs without BigQuery credentials — a `FakeIntrospector` is used throughout. Coverage must stay above 80%.

## Code style

Ruff handles formatting and linting:

```bash
uv run ruff format .
uv run ruff check .
```

Line length is 79 characters.

## Commits

Commits follow [Conventional Commits](https://www.conventionalcommits.org/). Use the commitizen helper:

```bash
uv run cz commit
```

Do not bypass pre-commit hooks with `--no-verify`.

## Pull requests

- One concern per PR.
- Include or update tests for any behaviour change.
- If the change affects the CLI interface or generated output format, update the relevant docs under `docs/`.
- Run the full pre-commit suite before opening: `uv run pre-commit run --all-files`.

## Reporting issues

Open a GitHub issue with a minimal reproducible contract YAML and the exact command you ran. If introspection is involved, describe the source schema (column names and types) — you do not need to share real data.

## Scope

modelgen is BigQuery-specific by design. Issues or PRs that add multi-warehouse support should first open a discussion explaining the proposed `Introspector` abstraction — there is a deliberate tradeoff between generality and simplicity here.
