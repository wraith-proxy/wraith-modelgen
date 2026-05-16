from __future__ import annotations

import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from modelgen import __version__
from modelgen.contract import ContractError, EventContract
from modelgen.generator import GenerationError, Layer, generate
from modelgen.introspect import IntrospectionError

app = typer.Typer(
    name="modelgen",
    help=(
        "BigQuery dbt model scaffolder. Generates one layer"
        " at a time from a YAML data contract."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"modelgen {__version__}")
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
        help="Path to the YAML data contract file.",
        exists=True,
        readable=True,
    ),
    raw: bool = typer.Option(
        False, "--raw", help="Generate the raw (Origin Data Product) layer."
    ),
    staging: bool = typer.Option(
        False,
        "--staging",
        help="Generate the staging (Consumption Data Product) layer.",
    ),
    output: Path = typer.Option(
        Path("./models"),
        "--output",
        "-o",
        help="Directory to write generated files into.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would be generated without writing.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable debug logging."
    ),
):
    """
    Generate a dbt model layer from a YAML data contract.

    \b
    Examples:
        modelgen gen contract.yml --raw -o ./models/raw
        modelgen gen contract.yml --staging -o ./models/staging
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

    from modelgen.introspect import BigQueryIntrospector

    introspector = BigQueryIntrospector()

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
            f"[bold]Event:[/bold] {event.name}\n"
            f"[bold]Layer:[/bold] {layer.value}\n"
            f"[bold]Source:[/bold] {event.source.fqn}\n"
            f"[bold]Target:[/bold] {target_dataset}.{target_model}\n"
            f"[bold]Unique key:[/bold] {', '.join(event.unique_key)} | "
            f"[bold]Loaded at:[/bold] {event.loaded_at_field}",
            title="[bold green]modelgen[/bold green]",
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
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Unexpected error:[/red] {type(e).__name__}: {e}")
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
                f"Source has {len(result.new_source_columns)} column(s)"
                " not yet in the staging contract:\n"
                f"  {', '.join(result.new_source_columns)}\n\n"
                "[dim]These flow through raw automatically. "
                "Add to the staging contract when "
                "downstream consumers need them.[/dim]",
                title="[yellow]Schema evolution detected[/yellow]",
                border_style="yellow",
            )
        )


@app.command()
def validate(
    contract: Path = typer.Argument(
        ...,
        help="Path to the YAML data contract file.",
        exists=True,
        readable=True,
    ),
):
    """Validate a contract's YAML structure (does not introspect BigQuery)."""
    try:
        event = EventContract.from_file(contract)
        console.print(
            f"[green]OK[/green] Contract valid: [bold]{event.name}[/bold]\n"
            f"  Source: {event.source.fqn}\n"
            f"  Unique key: {', '.join(event.unique_key)}\n"
            f"  Staging columns declared: {len(event.staging.columns)}"
        )
    except ContractError as e:
        console.print(f"[red]Invalid contract:[/red] {e}")
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
):
    """
    Discover all contracts in the repo and generate dbt models for every layer.

    \b
    Reads .modelgen.yml (or uses opinionated defaults) to find contracts
    and write to:
      models/raw/     — raw (Origin Data Product) layer
      models/staging/ — staging layer (skipped if no columns declared)

    \b
    Default layout expected in the consuming repo:
      contracts/          <- YAML data contracts
      models/raw/         <- generated raw models (output)
      models/staging/     <- generated staging models (output)
    """
    _configure_logging(verbose)

    from modelgen.config import ConfigError, ModelgenConfig
    from modelgen.introspect import BigQueryIntrospector

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

    console.print(
        Panel(
            f"[bold]Contracts:[/bold] {len(contract_files)}\n"
            f"[bold]Root:[/bold] {cfg.root}\n"
            f"[bold]Raw output:[/bold] {cfg.raw_output}\n"
            f"[bold]Staging output:[/bold] {cfg.staging_output}",
            title="[bold green]modelgen run[/bold green]",
            expand=False,
        )
    )

    introspector = BigQueryIntrospector()

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

        try:
            rel = contract_path.relative_to(cfg.root)
        except ValueError:
            rel = contract_path

        # Raw — always
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

        # Staging — skip if no columns declared in the contract
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
                f"[bold]{event_name}[/bold]: {len(cols)} source"
                " column(s) not yet in staging contract:\n"
                f"  {', '.join(cols)}\n\n"
                "[dim]These flow through raw automatically. "
                "Add to staging when consumers need them.[/dim]",
                title="[yellow]Schema evolution detected[/yellow]",
                border_style="yellow",
            )
        )

    if errors:
        for name, layer_name, msg in errors:
            console.print(f"[red]Error[/red] [{layer_name}] {name}: {msg}")
        raise typer.Exit(1)


def main():
    app()


if __name__ == "__main__":
    main()
