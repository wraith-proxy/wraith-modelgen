from __future__ import annotations

import logging
import traceback
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
import typer
import typer.core
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from modelgen import __version__
from modelgen.contract import (
    ContractError,
    EventContract,
    Issue,
    node_positions,
    read_contract,
)
from modelgen.exit_codes import ExitCode
from modelgen.generator import (
    GenerationError,
    GenerationResult,
    Layer,
    generate,
    write_generated,
)
from modelgen.introspect import IntrospectionError
from modelgen.lint import Finding, lint_contract
from modelgen.lint import errors as lint_errors
from modelgen.lint import warnings as lint_warnings
from modelgen.project import (
    FileStatus,
    Problem,
    ProjectResult,
    check_project,
    dest_path,
    failure_class,
    run_project,
)
from modelgen.report import (
    build_report,
    contract_issue,
    emit,
    file_entry,
    finding_issue,
    plain_issue,
    project_report,
    unmapped_issues,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from modelgen.config import ModelgenConfig


def _wants_json(argv: list[str]) -> bool:
    for i, arg in enumerate(argv):
        if arg.lower() == "--format=json":
            return True
        if arg == "--format" and argv[i + 1 : i + 2] == ["json"]:
            return True
    return False


class _Group(typer.core.TyperGroup):
    def invoke(self, ctx: click.Context) -> Any:
        argv = [*ctx._protected_args, *ctx.args]
        try:
            return super().invoke(ctx)
        except (
            click.exceptions.Exit,
            click.exceptions.Abort,
            click.ClickException,
            SystemExit,
            KeyboardInterrupt,
        ):
            raise
        except Exception as e:
            typer.echo(
                f"modelgen: internal error: {type(e).__name__}: {e}."
                " This is a bug; run with --verbose for the traceback.",
                err=True,
            )
            if logging.getLogger().isEnabledFor(logging.DEBUG):
                typer.echo(traceback.format_exc(), err=True, nl=False)
            if _wants_json(argv):
                message = f"internal error: {type(e).__name__}: {e}"
                emit(
                    build_report(
                        argv[0],
                        ExitCode.INTERNAL,
                        [],
                        [plain_issue(".", "internal", message)],
                        [],
                    )
                )
            raise typer.Exit(ExitCode.INTERNAL) from e


app = typer.Typer(
    cls=_Group,
    name="modelgen",
    help=(
        "[bold cyan]👻[/bold cyan] dbt model scaffolder for BigQuery and"
        " DuckDB."
        " Feed it a YAML data contract, get back generated raw and staging"
        " models. Introspects the contract's server (BigQuery or DuckDB)"
        " at generation time."
        " One layer per run."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)
console = Console()
err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        console.print(
            f"[bold cyan]👻[/bold cyan] modelgen [white]{__version__}[/white]"
        )
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    pass


def _finish(
    command: str,
    code: ExitCode,
    contract: Path,
    issues: list[dict],
    entries: list[dict],
    as_json: bool,
) -> None:
    if as_json:
        emit(build_report(command, code, [contract], issues, entries))
    if code:
        raise typer.Exit(code)


def _contract_invalid(
    command: str,
    contract: Path,
    error: ContractError,
    issues: list[dict],
    entries: list[dict],
    as_json: bool,
) -> None:
    err_console.print(f"[red]Invalid contract:[/red] {escape(str(error))}")
    issues.extend(contract_issue(contract, i) for i in error.issues)
    _finish(command, ExitCode.INVALID, contract, issues, entries, as_json)


def _emit_project(command: str, result: ProjectResult) -> None:
    _print_problems(result.problems)
    emit(project_report(command, result))
    raise typer.Exit(result.exit_code)


def _print_findings(findings: list[Finding]) -> None:
    for f in findings:
        if f.exempted:
            colour, label = "cyan", "exempt "
        elif f.is_error:
            colour, label = "red", "error  "
        else:
            colour, label = "yellow", "warning"
        why = (
            f" [dim](exempt: {escape(f.exempt_reason)})[/dim]"
            if f.exempted
            else ""
        )
        err_console.print(
            f"[{colour}]{label}[/{colour}] [dim]{f.code}[/dim]"
            f" {f.message}{why}"
        )


def _print_notices(notices: Iterable[Issue]) -> None:
    for notice in notices:
        err_console.print(
            f"[yellow]Warning:[/yellow] {escape(notice.message)}",
            soft_wrap=True,
        )


def _notify(message: str) -> None:
    err_console.print(f"[yellow]{escape(message)}[/yellow]")


def _print_problems(problems: list[Problem]) -> None:
    for p in problems:
        err_console.print(
            f"[red]Error[/red] {escape(f'[{p.layer}] {p.label}: {p.message}')}"
        )


_CHECK_STATUS = {
    FileStatus.MISSING: "[red]missing[/red]",
    FileStatus.DRIFT: "[red]drift[/red]",
    FileStatus.IN_SYNC: "[green]in sync[/green]",
}


_TABLE_OPTION = typer.Option(
    None,
    "--table",
    "-t",
    help=(
        "Only generate the named schema object. Repeatable. Output keeps"
        " contract order. Default: every object in the contract."
    ),
)
_SERVER_OPTION = typer.Option(
    None,
    "--server",
    "-s",
    help=(
        "Select the contract server by its `server:` id. Default: the"
        " first BigQuery server, else the first server whose type modelgen"
        " supports."
    ),
)


class OutputFormat(str, Enum):
    TEXT = "text"
    JSON = "json"


_FORMAT_OPTION = typer.Option(
    OutputFormat.TEXT,
    "--format",
    case_sensitive=False,
    help=(
        "'text' for people, or 'json' for one machine-readable report on"
        " stdout."
    ),
)
_INTROSPECT_OPTION = typer.Option(
    False,
    "--introspect",
    help=(
        "Query BigQuery for each source table's columns, using Application"
        " Default Credentials. Off by default, so output depends on the"
        " contract alone."
    ),
)


def _configure_logging(verbose: bool, out: Console = console) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(console=out, rich_tracebacks=False, show_path=False)
        ],
        force=True,
    )


@app.command()
def gen(
    contract: Path = typer.Argument(
        ...,
        help="Path to the YAML data contract.",
        exists=True,
        readable=True,
    ),
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Generate the raw (Origin Data Product) layer."
        " Everything in, nothing filtered.",
    ),
    staging: bool = typer.Option(
        False,
        "--staging",
        help="Generate the staging (Consumption Data Product) layer."
        " Source schema changes stay upstream.",
    ),
    output: Path = typer.Option(
        Path("./models"),
        "--output",
        "-o",
        help="Directory to write generated files into. Default: ./models.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview without writing. Commitment issues? Understandable.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Log everything. For the anxious."
    ),
    from_contract: bool = typer.Option(
        False,
        "--from-contract",
        help=(
            "The default: types and column list come from the contract's"
            " staging declarations. Kept so existing scripts keep working."
        ),
    ),
    introspect: bool = _INTROSPECT_OPTION,
    table: list[str] | None = _TABLE_OPTION,
    server: str | None = _SERVER_OPTION,
    layer_opt: str | None = typer.Option(
        None,
        "--layer",
        help="'schema' (models yml for produced tables, same as"
        " [bold]modelgen schema[/bold]) or 'sources' (sources yml for"
        " extract contracts, same as [bold]modelgen sources[/bold]).",
    ),
    output_format: OutputFormat = _FORMAT_OPTION,
):
    """
    Generate a dbt model layer from a YAML data contract.

    Specify [bold]--raw[/bold] or [bold]--staging[/bold] or
    [bold]--layer[/bold] (schema or sources). One layer per run.
    [bold]--server[/bold] is not accepted with [bold]--layer sources[/bold].

    Output comes from the contract alone. Pass [bold]--introspect[/bold]
    to read the source table's columns from BigQuery instead; without GCP
    Application Default Credentials that falls back to the contract.

    \b
    Examples:
        modelgen gen contract.yml --raw -o ./models/raw
        modelgen gen contract.yml --staging -o ./models/staging
        modelgen gen contract.yml --raw --dry-run
        modelgen gen contract.yml --raw --introspect
        modelgen gen contract.yml --raw --table eta_models --server prod

    A contract with several schema objects generates one model per object,
    in contract order.
    """
    _configure_logging(verbose, err_console)

    if layer_opt is not None:
        if layer_opt not in ("schema", "sources") or raw or staging:
            err_console.print(
                "[red]--layer only accepts 'schema' or 'sources', and not"
                " together with --raw or --staging.[/red]"
            )
            raise typer.Exit(ExitCode.USAGE)
        if layer_opt == "sources" and server is not None:
            err_console.print(
                "[red]--server does not apply to --layer sources: sources"
                " yml is dialect-independent.[/red]"
            )
            raise typer.Exit(ExitCode.USAGE)
        _write_schema(
            "gen", contract, output, dry_run, table, server,
            sources=layer_opt == "sources",
            as_json=output_format is OutputFormat.JSON,
        )  # fmt: skip
        return

    if raw == staging:
        err_console.print(
            "[red]Specify exactly one of --raw or --staging.[/red]"
        )
        raise typer.Exit(ExitCode.USAGE)

    layer = Layer.RAW if raw else Layer.STAGING
    from_contract = from_contract or not introspect
    as_json = output_format is OutputFormat.JSON
    issues: list[dict] = []
    entries: list[dict] = []

    def finish(code: ExitCode) -> None:
        _finish("gen", code, contract, issues, entries, as_json)

    try:
        events = EventContract.from_file_all(
            contract, server=server, tables=table
        )
    except ContractError as e:
        _contract_invalid("gen", contract, e, issues, entries, as_json)

    _print_notices(events[0].notices)
    issues.extend(contract_issue(contract, i) for i in events[0].notices)
    failed = 0
    positions = node_positions(read_contract(contract)) if as_json else {}
    for event in events:
        lint_errs = lint_errors(lint_contract(event))
        if lint_errs:
            err_console.print(f"[bold]{event.name}[/bold]")
            _print_findings(lint_errs)
            failed += len(lint_errs)
            issues.extend(
                finding_issue(contract, f, event.name, positions)
                for f in lint_errs
            )
    if failed:
        err_console.print(
            f"[red]Contract failed lint with {failed}"
            " error(s).[/red] Run [bold]modelgen validate[/bold]"
            " for the full report."
        )
        finish(ExitCode.LINT)

    from modelgen.introspect import IntrospectorChooser

    chooser = IntrospectorChooser(
        from_contract,
        "[yellow]No GCP credentials found."
        " Generating from contract only.[/yellow]",
        err_console.print,
    )
    mode_subtitle = (
        "introspecting BigQuery…"
        if any(chooser.queries_bigquery(e) for e in events)
        else "contract-only introspection"
    )

    results: list[tuple[EventContract, GenerationResult]] = []
    for event in events:
        introspector = chooser.for_event(event)

        target_dataset = (
            event.raw.dataset if layer == Layer.RAW else event.staging.dataset
        )
        target_model = (
            event.raw_model_name
            if layer == Layer.RAW
            else event.staging_model_name
        )

        if not as_json:
            console.print(
                Panel(
                    f"[bold]Event:[/bold]      {event.name}\n"
                    f"[bold]Layer:[/bold]      {layer.value}\n"
                    f"[bold]Source:[/bold]     {event.source.fqn}\n"
                    f"[bold]Target:[/bold]     {target_dataset}."
                    f"{target_model}\n"
                    f"[bold]Unique key:[/bold] {', '.join(event.unique_key)}"
                    f"  [bold]Loaded at:[/bold] {event.loaded_at_field}",
                    title="[bold green]👻 modelgen[/bold green]",
                    subtitle=f"[dim]{mode_subtitle}[/dim]",
                    expand=False,
                )
            )

        try:
            result = generate(
                contract=event,
                introspector=introspector,
                output_dir=output,
                layer=layer,
                dry_run=dry_run,
            )
        except (GenerationError, IntrospectionError) as e:
            err_console.print(
                f"[red]Generation failed ({event.name}):[/red] {e}"
            )
            err_console.print(
                "[dim]Fix the contract or the source. Then try again.[/dim]"
            )
            exit_code, issue_code = failure_class(e)
            issues.append(
                plain_issue(
                    contract, issue_code, str(e), object_name=event.name
                )
            )
            finish(exit_code)
        results.append((event, result))

    table_out = Table(show_header=True, header_style="bold cyan")
    table_out.add_column("File")
    table_out.add_column("Status")
    for _, result in results:
        for path, content in zip(
            result.written, result.artefacts.values(), strict=True
        ):
            status = (
                "[yellow]dry-run[/yellow]"
                if dry_run
                else "[green]written[/green]"
            )
            table_out.add_row(str(path), status)
            entries.append(
                file_entry(
                    path,
                    layer.value,
                    content,
                    "dry_run" if dry_run else "written",
                )
            )
    if not as_json:
        console.print(table_out)

    for event, result in results:
        if result.new_source_columns:
            err_console.print(
                Panel(
                    f"[bold]{event.name}:"
                    f" {len(result.new_source_columns)} new source"
                    " column(s) not yet in staging:[/bold]\n"
                    f"  [cyan]{', '.join(result.new_source_columns)}"
                    "[/cyan]\n\n"
                    "[dim]Flowing through raw automatically."
                    " Add to the staging contract when consumers need"
                    " them.[/dim]",
                    title="[yellow]⚠ Schema evolution detected[/yellow]",
                    border_style="yellow",
                )
            )
    finish(ExitCode.OK)


def _stop(
    command: str,
    code: ExitCode,
    output_format: OutputFormat,
    cfg: ModelgenConfig | None = None,
    message: str = "",
) -> None:
    notices = cfg.notices if cfg else []
    _print_notices(n for _, n in notices)
    if output_format is OutputFormat.JSON:
        issues = (
            [plain_issue(cfg.root if cfg else ".", "contract", message)]
            if message
            else []
        )
        issues += [contract_issue(p, n) for p, n in notices]
        emit(build_report(command, code, [], issues, []))
    raise typer.Exit(code)


def _require_patterns(
    cfg: ModelgenConfig, command: str, output_format: OutputFormat
) -> None:
    """Exit INVALID when a schema or sources pattern matches nothing."""
    for key, pattern, files in (
        (
            "schema_contracts",
            cfg.schema_contracts,
            cfg.schema_contract_files(),
        ),
        (
            "sources_contracts",
            cfg.sources_contracts,
            cfg.sources_contract_files(),
        ),
    ):
        if pattern and not files:
            err_console.print(
                f"[red]Error:[/red] {key} pattern"
                f" [dim]{pattern!r}[/dim] matched nothing in {cfg.root}"
            )
            _stop(
                command,
                ExitCode.INVALID,
                output_format,
                cfg,
                f"{key} pattern {pattern!r} matched nothing in {cfg.root}",
            )


def _write_schema(
    command: str,
    contract: Path,
    output: Path,
    dry_run: bool,
    table: list[str] | None,
    server: str | None,
    strict: bool = False,
    sources: bool = False,
    tests_output: Path | None = None,
    as_json: bool = False,
) -> None:
    from modelgen.schema_mode import render_schema
    from modelgen.sources_mode import render_sources

    layer = "sources" if sources else "schema"
    issues: list[dict] = []
    entries: list[dict] = []

    def finish(code: ExitCode) -> None:
        _finish(command, code, contract, issues, entries, as_json)

    unmapped: list[str] = []
    notices: list[Issue] = []
    try:
        if sources:
            files = render_sources(
                contract, tables=table, unmapped=unmapped, notices=notices
            )
        else:
            files = render_schema(
                contract,
                server=server,
                tables=table,
                unmapped=unmapped,
                notices=notices,
            )
    except ContractError as e:
        _contract_invalid(command, contract, e, issues, entries, as_json)
    _print_notices(notices)
    issues.extend(contract_issue(contract, i) for i in notices)
    status = "dry_run" if dry_run else "written"
    for filename, content in files.items():
        dest = dest_path(output, filename, tests_output)
        if not dry_run:
            try:
                write_generated(dest, content, output, tests_output)
            except GenerationError as e:
                err_console.print(f"[red]{escape(str(e))}[/red]")
                issues.append(plain_issue(contract, "generation", str(e)))
                finish(ExitCode.INVALID)
        entries.append(file_entry(dest, layer, content, status))
        if not as_json:
            label = "dry-run" if dry_run else "written"
            console.print(f"{escape(str(dest))} {label}", highlight=False)
    if unmapped:
        typer.echo(
            f"modelgen: {len(unmapped)} quality rule(s) not mapped:"
            f" {', '.join(unmapped)}",
            err=True,
        )
        failed = strict
        issues.extend(
            unmapped_issues(
                contract, unmapped, "error" if failed else "warning"
            )
        )
        if failed:
            finish(ExitCode.LINT)
    finish(ExitCode.OK)


@app.command()
def schema(
    contract: Path = typer.Argument(
        ...,
        help="Path to the YAML data contract.",
        exists=True,
        readable=True,
    ),
    output: Path = typer.Option(
        Path("./models"),
        "--output",
        "-o",
        help="Directory to write the yml into. Default: ./models.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview without writing."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Log everything."
    ),
    table: list[str] | None = _TABLE_OPTION,
    server: str | None = _SERVER_OPTION,
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Exit 3 when a quality rule cannot be mapped to a dbt test.",
    ),
    tests_output: Path | None = typer.Option(
        None,
        "--tests-output",
        help=(
            "Write singular tests (tests/generated/*.sql) under this"
            " directory instead of <output>/tests/."
        ),
    ),
    output_format: OutputFormat = _FORMAT_OPTION,
):
    """
    Generate models yml for tables the project PRODUCES.

    One `_<contract-name>__models.yml` per contract: enforced column
    types, not_null constraints, key tests and tests mapped from ODCS
    quality rules (singular ones under tests/generated/). No sources block,
    no loaded-at column needed.

    \b
    Examples:
        modelgen schema contracts/bus-marts.odcs.yaml -o models/marts
        modelgen schema contract.yml --table eta_models --server prod
    """
    _configure_logging(verbose, err_console)
    _write_schema(
        "schema", contract, output, dry_run, table, server, strict,
        tests_output=tests_output,
        as_json=output_format is OutputFormat.JSON,
    )  # fmt: skip


@app.command()
def sources(
    contract: Path = typer.Argument(
        ...,
        help="Path to the YAML data contract.",
        exists=True,
        readable=True,
    ),
    output: Path = typer.Option(
        Path("./models"),
        "--output",
        "-o",
        help="Directory to write the yml into. Default: ./models.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview without writing."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Log everything."
    ),
    table: list[str] | None = _TABLE_OPTION,
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Exit 3 when a quality rule cannot be mapped to a dbt test.",
    ),
    tests_output: Path | None = typer.Option(
        None,
        "--tests-output",
        help=(
            "Write singular tests (tests/generated/*.sql) under this"
            " directory instead of <output>/tests/."
        ),
    ),
    output_format: OutputFormat = _FORMAT_OPTION,
):
    """
    Generate a sources yml for tables the project READS (extract contracts).

    One `_<contract-name>__sources.yml` per contract: external location,
    freshness, a column-set test and tests mapped from ODCS quality rules
    (singular ones under tests/generated/, selecting from `source()`).

    \b
    Examples:
        modelgen sources contracts/extract.odcs.yaml -o models/staging
    """
    _configure_logging(verbose, err_console)
    _write_schema(
        "sources", contract, output, dry_run, table, None, strict,
        sources=True, tests_output=tests_output,
        as_json=output_format is OutputFormat.JSON,
    )  # fmt: skip


@app.command()
def validate(
    contract: Path = typer.Argument(
        ...,
        help="Path to the YAML data contract.",
        exists=True,
        readable=True,
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Treat warnings as errors. For CI gates with standards.",
    ),
    server: str | None = _SERVER_OPTION,
    output_format: OutputFormat = _FORMAT_OPTION,
):
    """
    Validate a contract offline: structure first, then semantics.

    Catches parse errors, missing fields, bad values, invalid column
    types and strategies, duplicate or unsafe column declarations, and
    staging configs that reference columns the model never selects,
    all in one pass, before you spend a round-trip to the warehouse.
    Every schema object in the contract is validated.
    """
    as_json = output_format is OutputFormat.JSON
    out = err_console if as_json else console
    issues: list[dict] = []

    try:
        events = EventContract.from_file_all(contract, server=server)
    except ContractError as e:
        _contract_invalid("validate", contract, e, issues, [], as_json)

    _print_notices(events[0].notices)
    issues.extend(contract_issue(contract, i) for i in events[0].notices)
    positions = node_positions(read_contract(contract)) if as_json else {}
    failed = False
    for event in events:
        findings = lint_contract(event)
        _print_findings(findings)
        issues.extend(
            finding_issue(contract, f, event.name, positions) for f in findings
        )
        errs = lint_errors(findings)
        warns = lint_warnings(findings)
        # Neither count includes exemptions, so a clean run with standing
        # exemptions must not read as a clean bill of health.
        exempt = sum(1 for f in findings if f.exempted)
        exempt_note = f", {exempt} exempt" if exempt else ""

        if errs or (strict and warns):
            failed = True
            err_console.print(
                f"[red]FAIL[/red] [bold]{event.name}[/bold]:"
                f" {len(errs)} error(s), {len(warns)} warning(s)"
                f"{exempt_note}"
                + (" [dim](--strict)[/dim]" if strict and not errs else "")
            )
            continue

        out.print(
            f"[green]OK[/green] [bold]{event.name}[/bold]\n"
            f"  Source:   {event.source.fqn}\n"
            f"  Key:      {', '.join(event.unique_key)}\n"
            f"  Columns:  {len(event.staging.columns)} staging declared\n"
            f"  Warnings: {len(warns)}{exempt_note}"
        )
    _finish(
        "validate",
        ExitCode.LINT if failed else ExitCode.OK,
        contract,
        issues,
        [],
        as_json,
    )


@app.command()
def run(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help=(
            "Path to .modelgen.yml. Auto-discovered from cwd"
            " upward if not given."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would be generated without writing.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable debug logging."
    ),
    from_contract: bool = typer.Option(
        False,
        "--from-contract",
        help=(
            "The default: types and column list come from each contract's"
            " staging declarations. Kept so existing scripts keep working."
        ),
    ),
    introspect: bool = _INTROSPECT_OPTION,
    server: str | None = _SERVER_OPTION,
    output_format: OutputFormat = _FORMAT_OPTION,
):
    """
    Find all contracts and regenerate everything .modelgen.yml names.

    Reads .modelgen.yml (or opinionated defaults) to locate contracts.
    Event contracts yield the raw and staging layers. Produced and extract
    contracts (schema_contracts, sources_contracts) yield their yml and
    tests when the matching *_output key is set. BigQuery is queried only
    for BigQuery contracts. Reports each failure by contract name and
    layer before exiting non-zero.

    \b
    Default outputs:
      models/raw/     -- raw (Origin Data Product) layer
      models/staging/ -- staging layer (skipped if no columns declared)

    \b
    Default layout expected in the consuming repo:
      contracts/          <- YAML data contracts
      models/raw/         <- generated raw models (output)
      models/staging/     <- generated staging models (output)
    """
    _configure_logging(verbose, err_console)
    from_contract = from_contract or not introspect

    from modelgen.config import ConfigError, ModelgenConfig

    try:
        if config is not None:
            cfg = ModelgenConfig.from_file(config)
        else:
            cfg = ModelgenConfig.discover(Path.cwd())
    except ConfigError as e:
        err_console.print(f"[red]Config error:[/red] {e}")
        _stop("run", ExitCode.INVALID, output_format, None, str(e))

    contract_files = cfg.contract_files()
    _require_patterns(cfg, "run", output_format)
    if (
        not contract_files
        and not cfg.schema_contract_files()
        and not cfg.sources_contract_files()
    ):
        err_console.print(
            f"[yellow]No contracts found.[/yellow] "
            f"Pattern [dim]{cfg.contracts_glob!r}[/dim]"
            f" matched nothing in {cfg.root}"
        )
        _stop("run", ExitCode.OK, output_format, cfg)

    as_json = output_format is OutputFormat.JSON
    if not as_json:
        console.print(
            Panel(
                f"[bold]Contracts:[/bold]       {len(contract_files)}\n"
                f"[bold]Root:[/bold]            {cfg.root}\n"
                f"[bold]Raw output:[/bold]      {cfg.raw_output}\n"
                f"[bold]Staging output:[/bold]  {cfg.staging_output}",
                title="[bold green]👻 modelgen run[/bold green]",
                subtitle=(
                    "[dim]contract-only introspection[/dim]"
                    if from_contract
                    else None
                ),
                expand=False,
            )
        )

    result = run_project(
        cfg,
        server=server,
        from_contract=from_contract,
        dry_run=dry_run,
        notify=_notify,
    )

    _print_notices(n for _, n in result.notices)
    if as_json:
        _emit_project("run", result)

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Contract")
    table.add_column("Layer")
    table.add_column("File")
    table.add_column("Status")
    for f in result.files:
        status = (
            "[yellow]dry-run[/yellow]"
            if f.status is FileStatus.DRY_RUN
            else "[green]written[/green]"
        )
        table.add_row(f.contract.name, f.layer, f.path.name, status)

    console.print(table)
    if result.unmapped_rules:
        typer.echo(
            f"modelgen: {len(result.unmapped_rules)} quality rule(s) not"
            f" mapped: {', '.join(result.unmapped_rules)}",
            err=True,
        )

    for event_name, cols in result.new_source_columns:
        console.print(
            Panel(
                f"[bold]{event_name}[/bold]: {len(cols)} source column(s)"
                " not yet in staging:\n"
                f"  [cyan]{', '.join(cols)}[/cyan]\n\n"
                "[dim]Flowing through raw untouched."
                " Add to staging when consumers actually need it.[/dim]",
                title="[yellow]⚠ Schema evolution detected[/yellow]",
                border_style="yellow",
            )
        )

    _print_problems(result.problems)
    raise typer.Exit(result.exit_code)


@app.command()
def check(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help=(
            "Path to .modelgen.yml. Auto-discovered from cwd"
            " upward if not given."
        ),
    ),
    diff: bool = typer.Option(
        False,
        "--diff",
        help=(
            "Print a unified diff for every drifted file. Ignored with"
            " --format json."
        ),
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable debug logging."
    ),
    from_contract: bool = typer.Option(
        False,
        "--from-contract",
        help=(
            "The default: types and column list come from each contract's"
            " staging declarations. Kept so existing scripts keep working."
        ),
    ),
    introspect: bool = _INTROSPECT_OPTION,
    server: str | None = _SERVER_OPTION,
    output_format: OutputFormat = _FORMAT_OPTION,
):
    """
    Verify committed models match what the contracts would generate.

    Renders every contract in memory and compares the output against the
    files on disk. Nothing is written. Exit code 0 means everything is
    in sync; 4 means drift or missing files; contract errors exit with
    their own code (1 invalid, 3 lint, 5 introspection). Wire it
    straight into CI next to your linter.

    \b
    Typical CI step:
      modelgen check --diff
    """
    _configure_logging(verbose, err_console)
    from_contract = from_contract or not introspect

    from modelgen.config import ConfigError, ModelgenConfig

    try:
        if config is not None:
            cfg = ModelgenConfig.from_file(config)
        else:
            cfg = ModelgenConfig.discover(Path.cwd())
    except ConfigError as e:
        err_console.print(f"[red]Config error:[/red] {e}")
        _stop("check", ExitCode.INVALID, output_format, None, str(e))

    contract_files = cfg.contract_files()
    schema_files = cfg.schema_contract_files()
    sources_files = cfg.sources_contract_files()
    _require_patterns(cfg, "check", output_format)
    if not contract_files and not schema_files and not sources_files:
        err_console.print(
            f"[yellow]No contracts found.[/yellow] "
            f"Pattern [dim]{cfg.contracts_glob!r}[/dim]"
            f" matched nothing in {cfg.root}"
        )
        _stop("check", ExitCode.OK, output_format, cfg)

    result = check_project(
        cfg, server=server, from_contract=from_contract, notify=_notify
    )

    _print_notices(n for _, n in result.notices)
    if output_format is OutputFormat.JSON:
        _emit_project("check", result)

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Contract")
    table.add_column("Layer")
    table.add_column("File")
    table.add_column("Status")
    for f in result.files:
        table.add_row(
            f.contract.name, f.layer, f.path.name, _CHECK_STATUS[f.status]
        )
    console.print(table)

    drifted = [
        f
        for f in result.files
        if f.status in (FileStatus.DRIFT, FileStatus.MISSING)
    ]
    if diff and drifted:
        import difflib

        for f in drifted:
            udiff = difflib.unified_diff(
                (f.on_disk or "").splitlines(keepends=True),
                f.content.splitlines(keepends=True),
                fromfile=f"{f.path} (on disk)",
                tofile=f"{f.path} (expected)",
            )
            console.print(f"[bold]--- {f.path}[/bold]")
            console.print("".join(udiff), markup=False, highlight=False)

    _print_problems(result.problems)

    if drifted or result.problems:
        err_console.print(
            f"[red]Drift detected:[/red] {len(drifted)} file(s) out of"
            f" sync, {len(result.problems)} error(s)."
            " Run [bold]modelgen run[/bold] and commit the result."
        )
        raise typer.Exit(result.exit_code)

    in_sync = sum(1 for f in result.files if f.status is FileStatus.IN_SYNC)
    console.print(
        f"[green]Everything in sync.[/green] {in_sync} file(s) match"
        " their contracts."
    )


@app.command()
def new(
    table: str = typer.Argument(
        ...,
        help="Fully-qualified source table: project.dataset.table",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        "-n",
        help="Event name for the contract. Default: derived from table.",
    ),
    output: Path = typer.Option(
        Path("./contracts"),
        "--output",
        "-o",
        help="Directory to write the contract into. Default: ./contracts.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing contract file.",
    ),
):
    """
    Bootstrap a draft contract from a live BigQuery table.

    Introspects the table and writes a contract with every column
    declared in staging, plus guessed unique_key and loaded_at_field
    (marked TODO, so confirm them before the contract goes live).
    Requires GCP Application Default Credentials.
    """
    from modelgen.introspect import (
        BigQueryIntrospector,
        has_default_credentials,
    )
    from modelgen.scaffold import ScaffoldError, draft_contract

    parts = table.split(".")
    if len(parts) != 3 or not all(parts):
        err_console.print(
            f"[red]Expected project.dataset.table,"
            f" got {escape(repr(table))}.[/red]"
        )
        raise typer.Exit(ExitCode.USAGE)
    project, dataset, table_name = parts

    if not has_default_credentials():
        err_console.print(
            "[red]No GCP credentials found.[/red] modelgen new needs to"
            " introspect the source table. Run:\n"
            "  gcloud auth application-default login"
        )
        raise typer.Exit(ExitCode.ENVIRONMENT)

    try:
        columns = BigQueryIntrospector().get_columns(
            project, dataset, table_name
        )
        yaml_text = draft_contract(
            project, dataset, table_name, columns, name=name
        )
    except (IntrospectionError, ScaffoldError) as e:
        err_console.print(f"[red]Could not draft contract:[/red] {e}")
        raise typer.Exit(ExitCode.ENVIRONMENT)

    event = EventContract.from_text(yaml_text)
    dest = output / f"{event.name}.yml"
    if dest.exists() and not force:
        err_console.print(
            f"[red]{dest} already exists.[/red] Pass --force to overwrite."
        )
        raise typer.Exit(ExitCode.USAGE)
    output.mkdir(parents=True, exist_ok=True)
    dest.write_text(yaml_text)

    console.print(
        Panel(
            f"[bold]Contract:[/bold]  {dest}\n"
            f"[bold]Event:[/bold]     {event.name}\n"
            f"[bold]Columns:[/bold]   {len(columns)} declared in staging\n"
            f"[bold]Key:[/bold]       {', '.join(event.unique_key)}"
            "  [dim](guessed)[/dim]\n"
            f"[bold]Loaded at:[/bold] {event.loaded_at_field}"
            "  [dim](guessed)[/dim]",
            title="[bold green]👻 modelgen new[/bold green]",
            subtitle="[dim]review the TODOs before generating[/dim]",
            expand=False,
        )
    )
    console.print(
        "[dim]Next: confirm unique_key and loaded_at_field, add"
        " descriptions, then run [bold]modelgen run[/bold].[/dim]"
    )


@app.command()
def init(
    directory: Path = typer.Argument(
        Path("."),
        help="Project root to initialise. Default: current directory.",
    ),
):
    """
    Initialise a modelgen project layout.

    Creates .modelgen.yml with documented defaults plus the contracts/
    and models/ directories. Safe to run in an existing repo; refuses
    to touch a project that already has a .modelgen.yml.
    """
    from modelgen.scaffold import ScaffoldError, init_project

    try:
        created = init_project(directory)
    except ScaffoldError as e:
        err_console.print(f"[yellow]{e}[/yellow]")
        raise typer.Exit(ExitCode.USAGE)

    for path in created:
        console.print(f"[green]created[/green] {path}")
    console.print(
        "\n[dim]Next: [bold]modelgen new project.dataset.table[/bold]"
        " to draft your first contract.[/dim]"
    )


def main():
    app()


if __name__ == "__main__":
    main()
