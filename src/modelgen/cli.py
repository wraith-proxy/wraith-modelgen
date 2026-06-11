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
from modelgen.generator import GenerationError, Layer, generate
from modelgen.introspect import IntrospectionError
from modelgen.lint import Finding, lint_contract

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
        colour = "red" if f.is_error else "yellow"
        label = "error  " if f.is_error else "warning"
        console.print(
            f"[{colour}]{label}[/{colour}] [dim]{f.code}[/dim] {f.message}"
        )


def _lint_errors(event: EventContract) -> list[Finding]:
    return [f for f in lint_contract(event) if f.is_error]


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
    """
    _configure_logging(verbose)

    if raw == staging:
        console.print("[red]Specify exactly one of --raw or --staging.[/red]")
        raise typer.Exit(2)

    layer = Layer.RAW if raw else Layer.STAGING

    try:
        event = EventContract.from_file(contract)
    except ContractError as e:
        console.print(f"[red]Invalid contract:[/red] {e}")
        raise typer.Exit(1)

    lint_errs = _lint_errors(event)
    if lint_errs:
        _print_findings(lint_errs)
        console.print(
            f"[red]Contract failed lint with {len(lint_errs)}"
            " error(s).[/red] Run [bold]modelgen validate[/bold]"
            " for the full report."
        )
        raise typer.Exit(1)

    from modelgen.introspect import (
        BigQueryIntrospector,
        ContractIntrospector,
        has_default_credentials,
    )

    if from_contract or not has_default_credentials():
        if not from_contract:
            console.print(
                "[yellow]No GCP credentials found."
                " Generating from contract only.[/yellow]"
            )
        introspector = ContractIntrospector(event)
        mode_subtitle = "contract-only mode"
    else:
        introspector = BigQueryIntrospector()
        mode_subtitle = "introspecting BigQuery…"

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
            contract_path=str(contract),
            dry_run=dry_run,
        )
    except (GenerationError, IntrospectionError) as e:
        console.print(f"[red]Generation failed:[/red] {e}")
        console.print(
            "[dim]Fix the contract or the source. Then try again.[/dim]"
        )
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Unexpected error:[/red] {type(e).__name__}: {e}")
        console.print(
            "[dim]This looks like a bug."
            " Run with --verbose for the full traceback.[/dim]"
        )
        if verbose:
            console.print_exception()
        raise typer.Exit(1)

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("File")
    table.add_column("Status")
    for path in result.written:
        status = (
            "[yellow]dry-run[/yellow]" if dry_run else "[green]written[/green]"
        )
        table.add_row(str(path), status)
    console.print(table)

    if result.new_source_columns:
        console.print(
            Panel(
                f"[bold]{len(result.new_source_columns)} new source column(s)"
                " not yet in staging:[/bold]\n"
                f"  [cyan]{', '.join(result.new_source_columns)}[/cyan]\n\n"
                "[dim]Flowing through raw automatically."
                " Add to the staging contract when consumers need them.[/dim]",
                title="[yellow]⚠ Schema evolution detected[/yellow]",
                border_style="yellow",
            )
        )


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
):
    """
    Validate a contract offline: structure first, then semantics.

    Catches parse errors, missing fields, bad values, invalid BigQuery
    types and strategies, duplicate or unsafe column declarations, and
    staging configs that reference columns the model never selects —
    all in one pass, before you spend a round-trip to BigQuery.
    """
    try:
        event = EventContract.from_file(contract)
    except ContractError as e:
        console.print(f"[red]Invalid contract:[/red] {e}")
        raise typer.Exit(1)

    findings = lint_contract(event)
    _print_findings(findings)
    errs = [f for f in findings if f.is_error]
    warns = [f for f in findings if not f.is_error]

    if errs or (strict and warns):
        console.print(
            f"[red]FAIL[/red] [bold]{event.name}[/bold]:"
            f" {len(errs)} error(s), {len(warns)} warning(s)"
            + (" [dim](--strict)[/dim]" if strict and not errs else "")
        )
        raise typer.Exit(1)

    console.print(
        f"[green]OK[/green] [bold]{event.name}[/bold]\n"
        f"  Source:   {event.source.fqn}\n"
        f"  Key:      {', '.join(event.unique_key)}\n"
        f"  Columns:  {len(event.staging.columns)} staging declared\n"
        f"  Warnings: {len(warns)}"
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
            "Skip BigQuery introspection for all contracts. Types and"
            " column list come from each contract's staging declarations."
        ),
    ),
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
            event = EventContract.from_file(contract_path)
        except ContractError as e:
            errors.append((contract_path.name, "parse", str(e)))
            continue

        lint_errs = _lint_errors(event)
        if lint_errs:
            errors.append(
                (
                    contract_path.name,
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

        try:
            rel = contract_path.relative_to(cfg.root)
        except ValueError:
            rel = contract_path

        # Raw layer
        try:
            raw_result = generate(
                contract=event,
                introspector=introspector,
                output_dir=cfg.raw_output,
                layer=Layer.RAW,
                contract_path=str(rel),
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
            errors.append((contract_path.name, "raw", str(e)))

        # Staging layer: skip if no columns declared
        if not event.staging.columns:
            continue

        try:
            stg_result = generate(
                contract=event,
                introspector=introspector,
                output_dir=cfg.staging_output,
                layer=Layer.STAGING,
                contract_path=str(rel),
                dry_run=dry_run,
            )
            status = (
                "[yellow]dry-run[/yellow]"
                if dry_run
                else "[green]written[/green]"
            )
            for p in stg_result.written:
                table.add_row(contract_path.name, "staging", p.name, status)
            if stg_result.new_source_columns:
                new_cols_notices.append(
                    (event.name, stg_result.new_source_columns)
                )
        except (GenerationError, IntrospectionError) as e:
            errors.append((contract_path.name, "staging", str(e)))

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
):
    """
    Verify committed models match what the contracts would generate.

    Renders every contract in memory and compares the output against the
    files on disk. Nothing is written. Exit code 0 means everything is
    in sync; 1 means drift, missing files, or contract errors — wire it
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

    for contract_path in contract_files:
        try:
            event = EventContract.from_file(contract_path)
        except ContractError as e:
            errors.append((contract_path.name, "parse", str(e)))
            continue

        lint_errs = _lint_errors(event)
        if lint_errs:
            errors.append(
                (
                    contract_path.name,
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

        try:
            rel = contract_path.relative_to(cfg.root)
        except ValueError:
            rel = contract_path

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
                    contract_path=str(rel),
                    dry_run=True,
                )
            except (GenerationError, IntrospectionError) as e:
                errors.append((contract_path.name, layer.value, str(e)))
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


def main():
    app()


if __name__ == "__main__":
    main()
