"""
BigQueryIntrospector tests with a stub client.

The BigQuery client is the system boundary, so a stub stands in for it:
no network, no credentials, and we can count API calls to prove caching.
"""

from types import SimpleNamespace

import pytest

from modelgen.introspect import (
    BigQueryIntrospector,
    IntrospectionError,
    has_default_credentials,
)


def _field(name, field_type, mode="NULLABLE", description="", fields=()):
    return SimpleNamespace(
        name=name,
        field_type=field_type,
        mode=mode,
        description=description,
        fields=fields,
    )


class StubClient:
    def __init__(self, schema):
        self.schema = schema
        self.calls = 0

    def get_table(self, fqn):
        self.calls += 1
        return SimpleNamespace(schema=self.schema)


class FailingClient:
    def get_table(self, fqn):
        raise ValueError("permission denied or some such")


SCHEMA = [
    _field("EVENT_ID", "STRING", "REQUIRED", "Primary key."),
    _field("TAGS", "STRING", "REPEATED"),
    _field(
        "PAYLOAD",
        "RECORD",
        fields=(_field("inner", "INT64"),),
    ),
    _field("RECEIVED_AT", "TIMESTAMP", mode=None, description=None),
]


class TestGetColumns:
    def test_maps_schema_fields(self):
        intr = BigQueryIntrospector(client=StubClient(SCHEMA))
        cols = intr.get_columns("p", "d", "t")
        assert [c.name for c in cols] == [
            "EVENT_ID",
            "TAGS",
            "PAYLOAD",
            "RECEIVED_AT",
        ]
        assert cols[0].mode == "REQUIRED"
        assert cols[0].description == "Primary key."

    def test_nested_record_fields_mapped(self):
        intr = BigQueryIntrospector(client=StubClient(SCHEMA))
        payload = intr.get_columns("p", "d", "t")[2]
        assert payload.field_type == "RECORD"
        assert payload.fields[0].name == "inner"
        assert payload.fields[0].field_type == "INT64"

    def test_none_mode_defaults_to_nullable(self):
        intr = BigQueryIntrospector(client=StubClient(SCHEMA))
        received = intr.get_columns("p", "d", "t")[3]
        assert received.mode == "NULLABLE"
        assert received.description == ""

    def test_failure_wrapped_in_introspection_error(self):
        intr = BigQueryIntrospector(client=FailingClient())
        with pytest.raises(IntrospectionError, match="p.d.t"):
            intr.get_columns("p", "d", "t")

    def test_underlying_error_preserved_in_message(self):
        intr = BigQueryIntrospector(client=FailingClient())
        with pytest.raises(IntrospectionError, match="permission denied"):
            intr.get_columns("p", "d", "t")


class TestCaching:
    def test_repeat_lookups_hit_api_once(self):
        client = StubClient(SCHEMA)
        intr = BigQueryIntrospector(client=client)
        first = intr.get_columns("p", "d", "t")
        second = intr.get_columns("p", "d", "t")
        assert client.calls == 1
        assert first == second

    def test_distinct_tables_fetched_separately(self):
        client = StubClient(SCHEMA)
        intr = BigQueryIntrospector(client=client)
        intr.get_columns("p", "d", "t1")
        intr.get_columns("p", "d", "t2")
        assert client.calls == 2

    def test_injected_client_used_directly(self):
        client = StubClient(SCHEMA)
        intr = BigQueryIntrospector(client=client)
        assert intr.client is client


class TestHasDefaultCredentials:
    def test_true_when_resolvable(self, monkeypatch):
        monkeypatch.setattr(
            "google.auth.default", lambda: (object(), "project")
        )
        assert has_default_credentials() is True

    def test_false_when_unresolvable(self, monkeypatch):
        def boom():
            raise RuntimeError("no creds")

        monkeypatch.setattr("google.auth.default", boom)
        assert has_default_credentials() is False
