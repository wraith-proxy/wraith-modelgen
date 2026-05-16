# 👻 Usage

## Commands

### `modelgen gen`

Generate a dbt model layer from a YAML data contract.

```
modelgen gen CONTRACT [OPTIONS]
```

| Option | Description |
|---|---|
| `--raw` | Generate the raw (Origin Data Product) layer |
| `--staging` | Generate the staging (Consumption Data Product) layer |
| `-o`, `--output PATH` | Directory to write files into (default: `./models`) |
| `--dry-run` | Print what would be generated without writing |
| `-v`, `--verbose` | Enable debug logging |

Exactly one of `--raw` or `--staging` must be specified.

**Examples:**

```bash
modelgen gen contract.yml --raw -o ./models/raw
modelgen gen contract.yml --staging -o ./models/staging
modelgen gen contract.yml --raw --dry-run
modelgen gen contract.yml --raw --verbose
```

### `modelgen validate`

Validate a contract's YAML structure without connecting to BigQuery.

```
modelgen validate CONTRACT
```

**Example:**

```bash
modelgen validate contract.yml
```

## Contract file

See [`example_contract.yml`](https://git.thomaspeoples.com/thomaspeoples/wraith-modelgen/src/branch/main/example_contract.yml) for a fully-annotated example.

### Schema evolution

| Scenario | Behaviour |
|---|---|
| New column added to source | Flows through raw automatically (`select *`). Not visible in staging until added to the contract. |
| Source column renamed | Update `unique_key` (if applicable) and the staging `source:` field. Staging name stays stable downstream. |
| Source column retyped | Update staging `type:`. The `CAST` absorbs the change. |
| Source column dropped | Generation fails loudly if staging still references it. |
