"""
BigQuery schema introspection. Pulled at `gen` time so contracts stay minimal
and never drift from the source.

The Introspector protocol exists so tests can inject a fake without needing
a live BQ connection or service account.

Handles BigQuery type quirks:
- REPEATED mode wraps the field_type in ARRAY<>
- RECORD type is rendered as STRUCT (best-effort; nested fields are flattened
  in the description for now)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from modelgen.dialect import get_dialect

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntrospectedColumn:
    """A column as described by the warehouse."""

    name: str
    field_type: str  # BQ type: STRING, INT64, NUMERIC, TIMESTAMP, RECORD, etc.
    mode: str = "NULLABLE"  # NULLABLE | REQUIRED | REPEATED
    description: str = ""
    fields: tuple = field(
        default_factory=tuple
    )  # For RECORD types: nested columns

    @property
    def dbt_data_type(self) -> str:
        """Render the column type as it should appear in a dbt schema.yml."""
        base = self.field_type
        if self.field_type == "RECORD":
            base = "STRUCT"
        if self.mode == "REPEATED":
            return f"ARRAY<{base}>"
        return base


class IntrospectionError(RuntimeError):
    """Raised when the warehouse cannot be queried or target does not exist."""


class Introspector(Protocol):
    """A thing that can read column metadata from a warehouse."""

    def get_columns(
        self, project: str, dataset: str, table: str
    ) -> list[IntrospectedColumn]: ...


class BigQueryIntrospector:
    """
    The production introspector. Uses Application Default Credentials.

    Credentials resolution order (handled by google-auth):
      1. GOOGLE_APPLICATION_CREDENTIALS env var
      2. gcloud auth application-default login
      3. Workload Identity / metadata server (when running on GCP)
    """

    def __init__(self, client=None):
        self._client = client
        # run/check hit the same table once per layer; one API call is
        # enough because generation is read-only within a process.
        self._cache: dict[tuple[str, str, str], list[IntrospectedColumn]] = {}

    @property
    def client(self):
        if self._client is None:
            try:
                from google.cloud import bigquery
            except ImportError as e:
                raise IntrospectionError(
                    "google-cloud-bigquery is not installed. "
                    "Install with: uv pip install 'wraith-modelgen'"
                ) from e
            self._client = bigquery.Client()
        return self._client

    def get_columns(
        self, project: str, dataset: str, table: str
    ) -> list[IntrospectedColumn]:
        key = (project, dataset, table)
        if key in self._cache:
            return self._cache[key]

        fqn = f"{project}.{dataset}.{table}"
        logger.info("Introspecting %s", fqn)

        try:
            t = self.client.get_table(fqn)
        except Exception as e:
            raise IntrospectionError(
                f"Failed to fetch table {fqn}. "
                "Check the table exists and credentials"
                " have bigquery.tables.get on it. "
                f"Underlying: {type(e).__name__}: {e}"
            ) from e

        columns = [_field_to_column(f) for f in t.schema]
        logger.info("Found %d columns in %s", len(columns), fqn)
        self._cache[key] = columns
        return columns


def _field_to_column(f) -> IntrospectedColumn:
    """Convert a google.cloud.bigquery.SchemaField to our internal type."""
    nested = tuple(_field_to_column(nf) for nf in (f.fields or ()))
    return IntrospectedColumn(
        name=f.name,
        field_type=f.field_type,
        mode=f.mode or "NULLABLE",
        description=f.description or "",
        fields=nested,
    )


def has_default_credentials() -> bool:
    """True if Application Default Credentials can be resolved."""
    try:
        import google.auth

        google.auth.default()
        return True
    except Exception:
        return False


class ContractIntrospector:
    """Derives column metadata from the contract when BQ is unavailable.

    Column list comes from staging declarations plus unique_key and
    loaded_at_field. Types come from the staging contract; unmapped
    key columns default to STRING. Validations still run but pass
    because the introspected set is built from the contract itself.
    """

    def __init__(self, contract) -> None:
        self._contract = contract

    def get_columns(
        self, project: str, dataset: str, table: str
    ) -> list[IntrospectedColumn]:
        default_type = get_dialect(
            self._contract.source.server_type
        ).string_type()
        seen: dict[str, IntrospectedColumn] = {}
        for col in self._contract.staging.columns:
            seen[col.source] = IntrospectedColumn(
                name=col.source,
                field_type=col.type if col.type else default_type,
                description=col.description,
            )
        for name in list(self._contract.unique_key) + [
            self._contract.loaded_at_field
        ]:
            if name not in seen:
                seen[name] = IntrospectedColumn(
                    name=name, field_type=default_type
                )
        return list(seen.values())


class FakeIntrospector:
    """For tests and offline development. Pre-populated with schemas."""

    def __init__(
        self, schemas: dict[tuple[str, str, str], list[IntrospectedColumn]]
    ):
        self._schemas = schemas

    def get_columns(
        self, project: str, dataset: str, table: str
    ) -> list[IntrospectedColumn]:
        key = (project, dataset, table)
        if key not in self._schemas:
            raise IntrospectionError(
                f"No fake schema registered for {project}.{dataset}.{table}. "
                f"Registered: {list(self._schemas.keys())}"
            )
        return self._schemas[key]


class IntrospectorChooser:
    """Pick an Introspector per event contract by its dialect.

    BigQuery is queried only for a BigQuery-dialect event when
    credentials exist and --from-contract is absent. Everything else
    is contract-only introspection. The shared BigQueryIntrospector is
    built lazily; the missing-credentials notice prints once, and only
    when a BigQuery-dialect event needs it.
    """

    def __init__(self, from_contract: bool, notice: str, print_fn):
        self._from_contract = from_contract
        self._notice = notice
        self._print = print_fn
        self._bq = None
        self._noticed = False
        self._creds: bool | None = None

    def _has_creds(self) -> bool:
        if self._creds is None:
            self._creds = has_default_credentials()
        return self._creds

    def queries_bigquery(self, event) -> bool:
        return (
            not self._from_contract
            and event.source.server_type == "bigquery"
            and self._has_creds()
        )

    def for_event(self, event):
        if self.queries_bigquery(event):
            if self._bq is None:
                self._bq = BigQueryIntrospector()
            return self._bq
        if (
            not self._from_contract
            and not self._noticed
            and event.source.server_type == "bigquery"
        ):
            self._noticed = True
            self._print(self._notice)
        return ContractIntrospector(event)
