"""
Scaffolding tests: draft_contract heuristics, init_project layout, and
the new/init CLI commands. The CLI tests reuse the monkeypatched
BigQueryIntrospector pattern from test_cli.
"""

import pytest
from typer.testing import CliRunner

from modelgen.cli import app
from modelgen.contract import EventContract
from modelgen.introspect import IntrospectedColumn
from modelgen.lint import errors, lint_contract
from modelgen.scaffold import (
    ScaffoldError,
    derive_event_name,
    draft_contract,
    guess_loaded_at,
    guess_unique_key,
    init_project,
)
from tests.fakes import FakeIntrospector

runner = CliRunner()

COLUMNS = [
    IntrospectedColumn("EVENT_ID", "STRING", "REQUIRED", "Primary key."),
    IntrospectedColumn("USER_ID", "STRING"),
    IntrospectedColumn("PAYLOAD", "RECORD"),
    IntrospectedColumn("TAGS", "STRING", "REPEATED"),
    IntrospectedColumn("RECEIVED_AT", "TIMESTAMP"),
]


class TestHeuristics:
    def test_unique_key_prefers_id_suffix(self):
        assert guess_unique_key(COLUMNS) == "EVENT_ID"

    def test_unique_key_falls_back_to_first_column(self):
        cols = [
            IntrospectedColumn("AMOUNT", "NUMERIC"),
            IntrospectedColumn("CURRENCY", "STRING"),
        ]
        assert guess_unique_key(cols) == "AMOUNT"

    def test_loaded_at_prefers_named_timestamp(self):
        cols = [
            IntrospectedColumn("CREATED_AT", "TIMESTAMP"),
            IntrospectedColumn("RECEIVED_AT", "TIMESTAMP"),
        ]
        assert guess_loaded_at(cols) == "CREATED_AT"

    def test_loaded_at_falls_back_to_any_timestamp(self):
        cols = [
            IntrospectedColumn("X", "STRING"),
            IntrospectedColumn("SOME_TS_COL", "TIMESTAMP"),
        ]
        assert guess_loaded_at(cols) == "SOME_TS_COL"

    def test_loaded_at_falls_back_to_first_column(self):
        cols = [IntrospectedColumn("A", "STRING")]
        assert guess_loaded_at(cols) == "A"

    def test_derive_event_name_sanitises(self):
        assert derive_event_name("User-Signups Raw") == "user_signups_raw"

    def test_derive_event_name_prefixes_leading_digit(self):
        assert derive_event_name("7days_table").startswith("e_")


class TestDraftContract:
    def draft(self, name=None) -> str:
        return draft_contract(
            "my-project", "landing", "user_signups", COLUMNS, name=name
        )

    def test_draft_parses_as_valid_contract(self):
        event = EventContract.from_text(self.draft())
        assert event.name == "user_signups"
        assert event.unique_key == ["EVENT_ID"]
        assert event.loaded_at_field == "RECEIVED_AT"

    def test_draft_passes_lint_without_errors(self):
        event = EventContract.from_text(self.draft())
        assert errors(lint_contract(event)) == []

    def test_all_columns_declared_in_staging(self):
        event = EventContract.from_text(self.draft())
        sources = {c.source for c in event.staging.columns}
        assert sources == {c.name for c in COLUMNS}

    def test_record_and_repeated_columns_skip_cast(self):
        event = EventContract.from_text(self.draft())
        by_source = {c.source: c for c in event.staging.columns}
        assert by_source["PAYLOAD"].type == ""
        assert by_source["TAGS"].type == ""
        assert by_source["EVENT_ID"].type == "STRING"

    def test_partition_derived_from_loaded_at(self):
        event = EventContract.from_text(self.draft())
        assert event.raw.partition_by is not None
        assert event.raw.partition_by.field == "RECEIVED_AT"
        assert event.raw.partition_by.data_type == "timestamp"

    def test_staging_drafts_as_ds_snapshot(self):
        event = EventContract.from_text(self.draft())
        assert event.staging.is_snapshot
        partition = event.staging.partition_by
        assert partition is not None
        assert partition.field == "ds"

    def test_no_raw_partition_when_loaded_at_not_temporal(self):
        cols = [IntrospectedColumn("ID", "STRING")]
        event = EventContract.from_text(
            draft_contract("p", "d", "t", cols, name="t")
        )
        assert event.raw.partition_by is None
        # Staging still snapshots — ds is its own column, not the source's.
        partition = event.staging.partition_by
        assert partition is not None
        assert partition.field == "ds"

    def test_unique_key_gets_tests(self):
        event = EventContract.from_text(self.draft())
        key_col = next(
            c for c in event.staging.columns if c.source == "EVENT_ID"
        )
        assert {t.name for t in key_col.tests} == {"not_null", "unique"}

    def test_descriptions_carried_over(self):
        event = EventContract.from_text(self.draft())
        key_col = next(
            c for c in event.staging.columns if c.source == "EVENT_ID"
        )
        assert key_col.description == "Primary key."

    def test_todo_markers_present(self):
        assert self.draft().count("TODO") >= 2

    def test_explicit_name_wins(self):
        event = EventContract.from_text(self.draft(name="signup"))
        assert event.name == "signup"

    def test_empty_schema_raises(self):
        with pytest.raises(ScaffoldError, match="no columns"):
            draft_contract("p", "d", "t", [])

    def test_description_quotes_escaped(self):
        cols = [
            IntrospectedColumn(
                "ID", "STRING", description='The "primary" key.'
            ),
            IntrospectedColumn("LOADED_AT", "TIMESTAMP"),
        ]
        event = EventContract.from_text(
            draft_contract("p", "d", "t", cols, name="t")
        )
        col = next(c for c in event.staging.columns if c.source == "ID")
        assert col.description == 'The "primary" key.'


class TestInitProject:
    def test_creates_layout(self, tmp_path):
        created = init_project(tmp_path)
        assert (tmp_path / ".modelgen.yml").exists()
        assert (tmp_path / "contracts").is_dir()
        assert (tmp_path / "models" / "raw").is_dir()
        assert (tmp_path / "models" / "staging").is_dir()
        assert len(created) == 4

    def test_config_contains_defaults(self, tmp_path):
        init_project(tmp_path)
        text = (tmp_path / ".modelgen.yml").read_text()
        assert "contracts/*.yml" in text
        assert "models/raw" in text

    def test_refuses_reinit(self, tmp_path):
        init_project(tmp_path)
        with pytest.raises(ScaffoldError, match="already"):
            init_project(tmp_path)

    def test_existing_dirs_tolerated(self, tmp_path):
        (tmp_path / "contracts").mkdir()
        created = init_project(tmp_path)
        assert (tmp_path / ".modelgen.yml").exists()
        assert all("contracts" not in str(p) for p in created)


@pytest.fixture
def patched_bq(monkeypatch):
    fake = FakeIntrospector(
        {("my-project", "landing", "user_signups"): COLUMNS}
    )
    monkeypatch.setattr(
        "modelgen.introspect.BigQueryIntrospector", lambda: fake
    )
    monkeypatch.setattr(
        "modelgen.introspect.has_default_credentials", lambda: True
    )
    return fake


class TestNewCommand:
    def test_writes_contract(self, patched_bq, tmp_path):
        result = runner.invoke(
            app,
            [
                "new",
                "my-project.landing.user_signups",
                "-o",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        dest = tmp_path / "user_signups.yml"
        assert dest.exists()
        event = EventContract.from_file(dest)
        assert event.unique_key == ["EVENT_ID"]

    def test_custom_name(self, patched_bq, tmp_path):
        result = runner.invoke(
            app,
            [
                "new",
                "my-project.landing.user_signups",
                "--name",
                "signup",
                "-o",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "signup.yml").exists()

    def test_refuses_overwrite_without_force(self, patched_bq, tmp_path):
        (tmp_path / "user_signups.yml").write_text("existing")
        result = runner.invoke(
            app,
            ["new", "my-project.landing.user_signups", "-o", str(tmp_path)],
        )
        assert result.exit_code == 2
        assert (tmp_path / "user_signups.yml").read_text() == "existing"

    def test_force_overwrites(self, patched_bq, tmp_path):
        (tmp_path / "user_signups.yml").write_text("existing")
        result = runner.invoke(
            app,
            [
                "new",
                "my-project.landing.user_signups",
                "-o",
                str(tmp_path),
                "--force",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "version" in (tmp_path / "user_signups.yml").read_text()

    def test_bad_fqn_exits_2(self, patched_bq, tmp_path):
        result = runner.invoke(app, ["new", "not-a-fqn", "-o", str(tmp_path)])
        assert result.exit_code == 2

    def test_no_credentials_exits_5(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "modelgen.introspect.has_default_credentials", lambda: False
        )
        result = runner.invoke(
            app,
            ["new", "my-project.landing.user_signups", "-o", str(tmp_path)],
        )
        assert result.exit_code == 5
        assert "gcloud auth" in result.output

    def test_unknown_table_exits_5(self, patched_bq, tmp_path):
        result = runner.invoke(
            app, ["new", "my-project.landing.nope", "-o", str(tmp_path)]
        )
        assert result.exit_code == 5

    def test_generated_contract_survives_full_pipeline(
        self, patched_bq, tmp_path, monkeypatch
    ):
        """new → run → check, end to end, on the draft alone."""
        monkeypatch.chdir(tmp_path)
        runner.invoke(app, ["init", str(tmp_path)])
        result = runner.invoke(app, ["new", "my-project.landing.user_signups"])
        assert result.exit_code == 0, result.output

        fake = FakeIntrospector(
            {("my-project", "landing", "user_signups"): COLUMNS}
        )
        monkeypatch.setattr(
            "modelgen.introspect.BigQueryIntrospector", lambda: fake
        )
        cfg = str(tmp_path / ".modelgen.yml")
        run = runner.invoke(app, ["run", "--config", cfg])
        assert run.exit_code == 0, run.output
        check = runner.invoke(app, ["check", "--config", cfg])
        assert check.exit_code == 0, check.output


class TestInitCommand:
    def test_init_creates_layout(self, tmp_path):
        result = runner.invoke(app, ["init", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert (tmp_path / ".modelgen.yml").exists()
        assert "created" in result.output

    def test_init_twice_exits_2(self, tmp_path):
        runner.invoke(app, ["init", str(tmp_path)])
        result = runner.invoke(app, ["init", str(tmp_path)])
        assert result.exit_code == 2


class TestDeterminism:
    def test_draft_is_deterministic(self):
        a = draft_contract("p", "d", "t", COLUMNS)
        b = draft_contract("p", "d", "t", COLUMNS)
        assert a == b
