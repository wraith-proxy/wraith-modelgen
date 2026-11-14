from __future__ import annotations

import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from modelgen import __version__
from modelgen.contract import ContractError, EventContract
from modelgen.generator import (
    GenerationError,
    GenerationResult,
    Layer,
    generate,
)
from modelgen.introspect import IntrospectionError
from modelgen.lint import Finding, lint_contract
from modelgen.lint import errors as lint_errors
from modelgen.lint import warnings as lint_warnings

app = typer.Typer(
    name="modelgen",
    help=(
        "[bold cyan]👻[/bold cyan] BigQuery dbt model scaffolder."
        " Feed it a YAML data contract, get back generated raw and staging"
        " models. Introspects BigQuery at generation time."
        " One layer per run."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)
console = Console()


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
        console.print(
            f"[{colour}]{label}[/{colour}] [dim]{f.code}[/dim]"
            f" {f.message}{why}"
        )


def _lint_errors(event: EventContract) -> list[Finding]:
    return lint_errors(lint_contract(event))


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
        " first server whose type modelgen supports."
    ),
)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                console=console, rich_tracebacks=False, show_path=False
            )
        ],
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
            "Skip BigQuery introspection. Types and column list come"
            " from the contract's staging declarations. Used when GCP"
            " credentials are unavailable."
        ),
    ),
    table: list[str] | None = _TABLE_OPTION,
    server: str | None = _SERVER_OPTION,
    layer_opt: str | None = typer.Option(
        None,
        "--layer",
        help="'schema' (models yml for produced tables, same as"
        " [bold]modelgen schema[/bold]) or 'sources' (sources yml for"
        " extract contracts, same as [bold]modelgen sources[/bold]).",
    ),
):
    """
    Generate a dbt model layer from a YAML data contract.

    Specify [bold]--raw[/bold] or [bold]--staging[/bold]. One layer per run.

    Pass [bold]--from-contract[/bold] to skip BigQuery and generate from the
    contract alone. This also happens automatically when no GCP Application
    Default Credentials are found.

    \b
    Examples:
        modelgen gen contract.yml --raw -o ./models/raw
        modelgen gen contract.yml --staging -o ./models/staging
        modelgen gen contract.yml --raw --dry-run
        modelgen gen contract.yml --raw --from-contract
        modelgen gen contract.yml --raw --table eta_models --server prod

    A contract with several schema objects generates one model per object,
    in contract order.
    """
    _configure_logging(verbose)

    if layer_opt is not None:
        if layer_opt not in ("schema", "sources") or raw or staging:
            console.print(
                "[red]--layer only accepts 'schema' or 'sources', and not"
                " together with --raw or --staging.[/red]"
            )
            raise typer.Exit(2)
        _write_schema(
            contract, output, dry_run, table, server,
            sources=layer_opt == "sources",
        )  # fmt: skip
        return

    if raw == staging:
        console.print("[red]Specify exactly one of --raw or --staging.[/red]")
        raise typer.Exit(2)

    layer = Layer.RAW if raw else Layer.STAGING

    try:
        events = EventContract.from_file_all(
            contract, server=server, tables=table
        )
    except ContractError as e:
        console.print(f"[red]Invalid contract:[/red] {e}")
        raise typer.Exit(1)

    failed = 0
    for event in events:
        lint_errs = _lint_errors(event)
        if lint_errs:
            console.print(f"[bold]{event.name}[/bold]")
            _print_findings(lint_errs)
            failed += len(lint_errs)
    if failed:
        console.print(
            f"[red]Contract failed lint with {failed}"
            " error(s).[/red] Run [bold]modelgen validate[/bold]"
            " for the full report."
        )
        raise typer.Exit(1)

    from modelgen.introspect import (
        BigQueryIntrospector,
        ContractIntrospector,
        has_default_credentials,
    )

    use_contract = from_contract or not has_default_credentials()
    if use_contract and not from_contract:
        console.print(
            "[yellow]No GCP credentials found."
            " Generating from contract only.[/yellow]"
        )
    mode_subtitle = (
        "contract-only mode" if use_contract else "introspecting BigQuery…"
    )
    shared_introspector = None if use_contract else BigQueryIntrospector()

    results: list[tuple[EventContract, GenerationResult]] = []
    for event in events:
        introspector = (
            ContractIntrospector(event)
            if use_contract
            else shared_introspector
        )
        assert introspector is not None

        target_dataset = (
            event.raw.dataset if layer == Layer.RAW else event.staging.dataset
        )
        target_model = (
            event.raw_model_name
            if layer == Layer.RAW
            else event.staging_model_name
        )

        console.print(
            Panel(
                f"[bold]Event:[/bold]      {event.name}\n"
                f"[bold]Layer:[/bold]      {layer.value}\n"
                f"[bold]Source:[/bold]     {event.source.fqn}\n"
                f"[bold]Target:[/bold]     {target_dataset}.{target_model}\n"
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
            console.print(f"[red]Generation failed ({event.name}):[/red] {e}")
            console.print(
                "[dim]Fix the contract or the source. Then try again.[/dim]"
            )
            raise typer.Exit(1)
        except Exception as e:
            console.print(
                f"[red]Unexpected error ({event.name}):[/red]"
                f" {type(e).__name__}: {e}"
            )
            console.print(
                "[dim]This looks like a bug."
                " Run with --verbose for the full traceback.[/dim]"
            )
            if verbose:
                console.print_exception()
            raise typer.Exit(1)
        results.append((event, result))

    table_out = Table(show_header=True, header_style="bold cyan")
    table_out.add_column("File")
    table_out.add_column("Status")
    for _, result in results:
        for path in result.written:
            status = (
                "[yellow]dry-run[/yellow]"
                if dry_run
                else "[green]written[/green]"
            )
            table_out.add_row(str(path), status)
    console.print(table_out)

    for event, result in results:
        if result.new_source_columns:
            console.print(
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


def _write_schema(
    contract: Path,
    output: Path,
    dry_run: bool,
    table: list[str] | None,
    server: str | None,
    strict: bool = False,
    sources: bool = False,
) -> None:
    from modelgen.schema_mode import render_schema
    from modelgen.sources_mode import render_sources

    unmapped: list[str] = []
    try:
        if sources:
            files = render_sources(contract, tables=table, unmapped=unmapped)
        else:
            files = render_schema(
                contract, server=server, tables=table, unmapped=unmapped
            )
    except ContractError as e:
        console.print(f"[red]Invalid contract:[/red] {e}")
        raise typer.Exit(1)
    for filename, content in files.items():
        dest = output / filename
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        status = "dry-run" if dry_run else "written"
        console.print(f"{escape(str(dest))} {status}", highlight=False)
    if unmapped:
        typer.echo(
            f"modelgen: {len(unmapped)} quality rule(s) not mapped:"
            f" {', '.join(unmapped)}",
            err=True,
        )
        if strict:
            raise typer.Exit(1)


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
        help="Exit 1 when a quality rule cannot be mapped to a dbt test.",
    ),
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
    _configure_logging(verbose)
    _write_schema(contract, output, dry_run, table, server, strict)


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
        help="Exit 1 when a quality rule cannot be mapped to a dbt test.",
    ),
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
    _configure_logging(verbose)
    _write_schema(contract, output, dry_run, table, None, strict, sources=True)


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
):
    """
    Validate a contract offline: structure first, then semantics.

    Catches parse errors, missing fields, bad values, invalid BigQuery
    types and strategies, duplicate or unsafe column declarations, and
    staging configs that reference columns the model never selects,
    all in one pass, before you spend a round-trip to BigQuery.
    Every schema object in the contract is validated.
    """
    try:
        events = EventContract.from_file_all(contract, server=server)
    except ContractError as e:
        console.print(f"[red]Invalid contract:[/red] {e}")
        raise typer.Exit(1)

    failed = False
    for event in events:
        findings = lint_contract(event)
        _print_findings(findings)
        errs = lint_errors(findings)
        warns = lint_warnings(findings)
        # Neither count includes exemptions, so a clean run with standing
        # exemptions must not read as a clean bill of health.
        exempt = sum(1 for f in findings if f.exempted)
        exempt_note = f", {exempt} exempt" if exempt else ""

        if errs or (strict and warns):
            failed = True
            console.print(
                f"[red]FAIL[/red] [bold]{event.name}[/bold]:"
                f" {len(errs)} error(s), {len(warns)} warning(s)"
                f"{exempt_note}"
                + (" [dim](--strict)[/dim]" if strict and not errs else "")
            )
            continue

        console.print(
            f"[green]OK[/green] [bold]{event.name}[/bold]\n"
            f"  Source:   {event.source.fqn}\n"
            f"  Key:      {', '.join(event.unique_key)}\n"
            f"  Columns:  {len(event.staging.columns)} staging declared\n"
            f"  Warnings: {len(warns)}{exempt_note}"
        )
    if failed:
        raise typer.Exit(1)


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
            "Skip BigQuery introspection for all contracts. Types and"
            " column list come from each contract's staging declarations."
        ),
    ),
    server: str | None = _SERVER_OPTION,
):
    """
    Find all contracts and regenerate every layer.

    Reads .modelgen.yml (or opinionated defaults) to locate contracts,
    introspects BigQuery for each, and writes both layers. Reports each
    failure by contract name and layer before exiting non-zero.

    \b
    Reads .modelgen.yml (or uses opinionated defaults) to find contracts
    and write to:
      models/raw/     -- raw (Origin Data Product) layer
      models/staging/ -- staging layer (skipped if no columns declared)

    \b
    Default layout expected in the consuming repo:
      contracts/          <- YAML data contracts
      models/raw/         <- generated raw models (output)
      models/staging/     <- generated staging models (output)
    """
    _configure_logging(verbose)

    from modelgen.config import ConfigError, ModelgenConfig
    from modelgen.introspect import (
        BigQueryIntrospector,
        ContractIntrospector,
        has_default_credentials,
    )

    try:
        if config is not None:
            cfg = ModelgenConfig.from_file(config)
        else:
            cfg = ModelgenConfig.discover(Path.cwd())
    except ConfigError as e:
        console.print(f"[red]Config error:[/red] {e}")
        raise typer.Exit(1)

    contract_files = cfg.contract_files()
    if not contract_files:
        console.print(
            f"[yellow]No contracts found.[/yellow] "
            f"Pattern [dim]{cfg.contracts_glob!r}[/dim]"
            f" matched nothing in {cfg.root}"
        )
        raise typer.Exit(0)

    use_contract_introspection = from_contract or not has_default_credentials()
    if use_contract_introspection and not from_contract:
        console.print(
            "[yellow]No GCP credentials found."
            " Generating from contracts only.[/yellow]"
        )

    bq_subtitle = (
        "contract-only mode" if use_contract_introspection else "buckle up"
    )
    console.print(
        Panel(
            f"[bold]Contracts:[/bold]       {len(contract_files)}\n"
            f"[bold]Root:[/bold]            {cfg.root}\n"
            f"[bold]Raw output:[/bold]      {cfg.raw_output}\n"
            f"[bold]Staging output:[/bold]  {cfg.staging_output}",
            title="[bold green]👻 modelgen run[/bold green]",
            subtitle=f"[dim]{bq_subtitle}[/dim]",
            expand=False,
        )
    )

    if not use_contract_introspection:
        shared_introspector = BigQueryIntrospector()
    else:
        shared_introspector = None

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Contract")
    table.add_column("Layer")
    table.add_column("File")
    table.add_column("Status")

    errors: list[tuple[str, str, str]] = []
    new_cols_notices: list[tuple[str, list[str]]] = []

    for contract_path in contract_files:
        try:
            events = EventContract.from_file_all(contract_path, server=server)
        except ContractError as e:
            errors.append((contract_path.name, "parse", str(e)))
            continue

        for event in events:
            label = (
                contract_path.name
                if len(events) == 1
                else f"{contract_path.name}:{event.name}"
            )

            lint_errs = _lint_errors(event)
            if lint_errs:
                errors.append(
                    (
                        label,
                        "lint",
                        "; ".join(f.message for f in lint_errs),
                    )
                )
                continue

            introspector = (
                ContractIntrospector(event)
                if use_contract_introspection
                else shared_introspector
            )
            assert introspector is not None

            # Raw layer
            try:
                raw_result = generate(
                    contract=event,
                    introspector=introspector,
                    output_dir=cfg.raw_output,
                    layer=Layer.RAW,
                    dry_run=dry_run,
                )
                status = (
                    "[yellow]dry-run[/yellow]"
                    if dry_run
                    else "[green]written[/green]"
                )
                for p in raw_result.written:
                    table.add_row(contract_path.name, "raw", p.name, status)
            except (GenerationError, IntrospectionError) as e:
                errors.append((label, "raw", str(e)))

            # Staging layer: skip if no columns declared
            if not event.staging.columns:
                continue

            try:
                stg_result = generate(
                    contract=event,
                    introspector=introspector,
                    output_dir=cfg.staging_output,
                    layer=Layer.STAGING,
                    dry_run=dry_run,
                )
                status = (
                    "[yellow]dry-run[/yellow]"
                    if dry_run
                    else "[green]written[/green]"
                )
                for p in stg_result.written:
                    table.add_row(
                        contract_path.name, "staging", p.name, status
                    )
                if stg_result.new_source_columns:
                    new_cols_notices.append(
                        (event.name, stg_result.new_source_columns)
                    )
            except (GenerationError, IntrospectionError) as e:
                errors.append((label, "staging", str(e)))

    console.print(table)

    for event_name, cols in new_cols_notices:
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

    if errors:
        for name, layer_name, msg in errors:
            console.print(
                f"[red]Error[/red] {escape(f'[{layer_name}] {name}: {msg}')}"
            )
        raise typer.Exit(1)


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
        help="Print a unified diff for every drifted file.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable debug logging."
    ),
    from_contract: bool = typer.Option(
        False,
        "--from-contract",
        help=(
            "Skip BigQuery introspection for all contracts. Types and"
            " column list come from each contract's staging declarations."
        ),
    ),
    server: str | None = _SERVER_OPTION,
):
    """
    Verify committed models match what the contracts would generate.

    Renders every contract in memory and compares the output against the
    files on disk. Nothing is written. Exit code 0 means everything is
    in sync; 1 means drift, missing files, or contract errors. Wire it
    straight into CI next to your linter.

    \b
    Typical CI step:
      modelgen check --diff
    """
    _configure_logging(verbose)

    from modelgen.config import ConfigError, ModelgenConfig
    from modelgen.introspect import (
        BigQueryIntrospector,
        ContractIntrospector,
        has_default_credentials,
    )

    try:
        if config is not None:
            cfg = ModelgenConfig.from_file(config)
        else:
            cfg = ModelgenConfig.discover(Path.cwd())
    except ConfigError as e:
        console.print(f"[red]Config error:[/red] {e}")
        raise typer.Exit(1)

    contract_files = cfg.contract_files()
    schema_files = cfg.schema_contract_files()
    sources_files = cfg.sources_contract_files()
    for key, pattern, files in (
        ("schema_contracts", cfg.schema_contracts, schema_files),
        ("sources_contracts", cfg.sources_contracts, sources_files),
    ):
        if pattern and not files:
            console.print(
                f"[red]Error:[/red] {key} pattern"
                f" [dim]{pattern!r}[/dim] matched nothing in {cfg.root}"
            )
            raise typer.Exit(1)
    if not contract_files and not schema_files and not sources_files:
        console.print(
            f"[yellow]No contracts found.[/yellow] "
            f"Pattern [dim]{cfg.contracts_glob!r}[/dim]"
            f" matched nothing in {cfg.root}"
        )
        raise typer.Exit(0)

    use_contract_introspection = from_contract or not has_default_credentials()
    if use_contract_introspection and not from_contract:
        console.print(
            "[yellow]No GCP credentials found."
            " Checking against contracts only.[/yellow]"
        )

    shared_introspector = (
        None if use_contract_introspection else BigQueryIntrospector()
    )

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Contract")
    table.add_column("Layer")
    table.add_column("File")
    table.add_column("Status")

    errors: list[tuple[str, str, str]] = []
    drifted: list[tuple[Path, str, str]] = []  # path, on_disk, expected
    in_sync = 0

    from modelgen.schema_mode import render_schema
    from modelgen.sources_mode import render_sources

    produced = (
        (schema_files, cfg.schema_output, "schema", None),
        (
            cfg.sources_contract_files(),
            cfg.sources_output,
            "sources",
            render_sources,
        ),
    )
    for files_, out_dir, layer_name, renderer in produced:
        if not files_ or out_dir is None:
            continue
        for contract_path in files_:
            try:
                if renderer is None:
                    files = render_schema(contract_path, server=server)
                else:
                    files = renderer(contract_path)
            except ContractError as e:
                errors.append((contract_path.name, "parse", str(e)))
                continue
            for filename, expected in files.items():
                dest = out_dir / filename
                if not dest.exists():
                    drifted.append((dest, "", expected))
                    status = "[red]missing[/red]"
                elif dest.read_text() != expected:
                    drifted.append((dest, dest.read_text(), expected))
                    status = "[red]drift[/red]"
                else:
                    in_sync += 1
                    status = "[green]in sync[/green]"
                table.add_row(contract_path.name, layer_name, filename, status)

    for contract_path in contract_files:
        try:
            events = EventContract.from_file_all(contract_path, server=server)
        except ContractError as e:
            errors.append((contract_path.name, "parse", str(e)))
            continue

        for event in events:
            label = (
                contract_path.name
                if len(events) == 1
                else f"{contract_path.name}:{event.name}"
            )

            lint_errs = _lint_errors(event)
            if lint_errs:
                errors.append(
                    (
                        label,
                        "lint",
                        "; ".join(f.message for f in lint_errs),
                    )
                )
                continue

            introspector = (
                ContractIntrospector(event)
                if use_contract_introspection
                else shared_introspector
            )
            assert introspector is not None

            layers: list[tuple[Layer, Path]] = [(Layer.RAW, cfg.raw_output)]
            if event.staging.columns:
                layers.append((Layer.STAGING, cfg.staging_output))

            for layer, output_dir in layers:
                try:
                    result = generate(
                        contract=event,
                        introspector=introspector,
                        output_dir=output_dir,
                        layer=layer,
                        dry_run=True,
                    )
                except (GenerationError, IntrospectionError) as e:
                    errors.append((label, layer.value, str(e)))
                    continue

                for filename, expected in result.artefacts.items():
                    dest = output_dir / filename
                    if not dest.exists():
                        drifted.append((dest, "", expected))
                        status = "[red]missing[/red]"
                    elif dest.read_text() != expected:
                        drifted.append((dest, dest.read_text(), expected))
                        status = "[red]drift[/red]"
                    else:
                        in_sync += 1
                        status = "[green]in sync[/green]"
                    table.add_row(
                        contract_path.name, layer.value, filename, status
                    )

    console.print(table)

    if diff and drifted:
        import difflib

        for dest, on_disk, expected in drifted:
            udiff = difflib.unified_diff(
                on_disk.splitlines(keepends=True),
                expected.splitlines(keepends=True),
                fromfile=f"{dest} (on disk)",
                tofile=f"{dest} (expected)",
            )
            console.print(f"[bold]--- {dest}[/bold]")
            console.print("".join(udiff), markup=False, highlight=False)

    if errors:
        for name, layer_name, msg in errors:
            console.print(
                f"[red]Error[/red] {escape(f'[{layer_name}] {name}: {msg}')}"
            )

    if drifted or errors:
        console.print(
            f"[red]Drift detected:[/red] {len(drifted)} file(s) out of"
            f" sync, {len(errors)} error(s)."
            " Run [bold]modelgen run[/bold] and commit the result."
        )
        raise typer.Exit(1)

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
        console.print(
            f"[red]Expected project.dataset.table,"
            f" got {escape(repr(table))}.[/red]"
        )
        raise typer.Exit(2)
    project, dataset, table_name = parts

    if not has_default_credentials():
        console.print(
            "[red]No GCP credentials found.[/red] modelgen new needs to"
            " introspect the source table. Run:\n"
            "  gcloud auth application-default login"
        )
        raise typer.Exit(1)

    try:
        columns = BigQueryIntrospector().get_columns(
            project, dataset, table_name
        )
        yaml_text = draft_contract(
            project, dataset, table_name, columns, name=name
        )
    except (IntrospectionError, ScaffoldError) as e:
        console.print(f"[red]Could not draft contract:[/red] {e}")
        raise typer.Exit(1)

    event = EventContract.from_text(yaml_text)
    dest = output / f"{event.name}.yml"
    if dest.exists() and not force:
        console.print(
            f"[red]{dest} already exists.[/red] Pass --force to overwrite."
        )
        raise typer.Exit(1)
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
        console.print(f"[yellow]{e}[/yellow]")
        raise typer.Exit(1)

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
