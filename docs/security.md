# 👻 Security Statement

## Credentials

wraith-modelgen uses Google Application Default Credentials (ADC) to connect to BigQuery. It never stores, logs, or transmits credentials. The credential resolution order is:

1. `GOOGLE_APPLICATION_CREDENTIALS` env var (service account key file)
2. `gcloud auth application-default login` (user credentials)
3. Workload Identity / GCE metadata server (when running on GCP)

The minimum BigQuery permission required is `bigquery.tables.get` on the source table.

## No data access

wraith-modelgen only reads table **schema metadata** (column names and types). It never reads row data from your BigQuery tables.

## Contract files

Contract YAML files may contain GCP project IDs and dataset/table names. Treat them as you would any infrastructure configuration — they should be version-controlled but not contain credentials.

## Secrets detection

The pre-commit config includes `detect-secrets` to prevent accidental credential commits.
