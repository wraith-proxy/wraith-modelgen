# 👻 Usage

## Workflow

Write a contract. Validate it. Generate. Commit. Repeat when the source changes.

```mermaid
flowchart LR
    A["Write\ncontract.yml"] --> B["modelgen validate"]
    B --> C{Valid?}
    C -->|no| A
    C -->|yes| D["modelgen gen --raw\n-o models/raw"]
    D --> E["modelgen gen --staging\n-o models/staging"]
    E --> F["Commit generated\nmodels to repo"]
    F --> G{Source schema\nchanged?}
    G -->|yes| H["Update contract\nregenerate"]
    G -->|no| F
    H --> D
```

Or, if you have `.modelgen.yml` set up, skip the per-layer invocations:

```bash
modelgen run
```

---

## Commands

### `modelgen validate`

Offline contract validation. No BigQuery credentials or network access needed.

```bash
modelgen validate contract.yml
```

Run this before `gen` to catch YAML errors without a round-trip to BigQuery.

---

### `modelgen gen`

Generate a single layer from a contract. Specify one of `--raw` or `--staging`.

```bash
modelgen gen CONTRACT [OPTIONS]
```

| Option | Description |
|---|---|
| `--raw` | Generate the raw (Origin Data Product) layer |
| `--staging` | Generate the staging (Consumption Data Product) layer |
| `-o`, `--output PATH` | Write files here (default: `./models`) |
| `--dry-run` | Preview without writing |
| `-v`, `--verbose` | Debug logging |

Exactly one of `--raw` or `--staging` is required.

**Examples:**

```bash
# Generate the raw layer
modelgen gen contract.yml --raw -o ./models/raw

# Generate the staging layer
modelgen gen contract.yml --staging -o ./models/staging

# Preview what would be written
modelgen gen contract.yml --raw --dry-run

# Verbose output
modelgen gen contract.yml --staging --verbose
```

---

### `modelgen run`

Find all contracts and regenerate every layer in one command.

```bash
modelgen run [OPTIONS]
```

| Option | Description |
|---|---|
| `-c`, `--config PATH` | Path to `.modelgen.yml`. Auto-discovered upward from cwd if not given. |
| `--dry-run` | Preview without writing |
| `-v`, `--verbose` | Debug logging |

**`.modelgen.yml` config:**

```yaml
contracts: contracts/*.yml    # glob relative to this file's directory
raw_output: models/raw
staging_output: models/staging
```

If `.modelgen.yml` is not found, these defaults apply:

| Setting | Default |
|---|---|
| `contracts` | `contracts/*.yml` |
| `raw_output` | `models/raw` |
| `staging_output` | `models/staging` |

Staging generation is skipped for contracts with no columns declared; raw-only contracts are supported.

**Examples:**

```bash
# Generate everything
modelgen run

# With explicit config
modelgen run --config path/to/.modelgen.yml

# Preview output
modelgen run --dry-run
```

---

## Contract file

A fully-annotated example: [`example_contract.yml`](https://git.thomaspeoples.com/thomaspeoples/wraith-modelgen/src/branch/main/example_contract.yml).

---

## Schema evolution

Source schemas change. wraith-modelgen validates the boundary and reports what broke.

| Scenario | Behaviour |
|---|---|
| New column added to source | Flows through raw automatically (`select *`). Invisible in staging until added to the contract. |
| Source column renamed | Update `unique_key` (if applicable) and the staging `source:` field. Staging name stays stable downstream. |
| Source column retyped | Update staging `type:`. The `CAST` absorbs it. If values are incompatible, BigQuery will fail at query time. |
| Source column dropped | Generation fails if staging still references it. Update the contract or restore the source column. |

!!! tip "The layering principle"
    Raw absorbs source drift. Staging absorbs raw drift. Downstream consumers see a stable interface.
