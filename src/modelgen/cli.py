from __future__ import annotations

import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from modelgen import __version__
from modelgen.contract import EventContract, ContractError
from modelgen.generator import Layer, generate, GenerationError
from modelgen.introspect import IntrospectionError

app = typer.Typer(
    name="modelgen",
    help="BigQuery dbt model scaffolder. Generates one layer at a time from a YAML data contract.",
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
        handlers=[RichHandler(console=console, rich_tracebacks=False, show_path=False)],
    )


@app.command()
def gen(
    contract: Path = typer.Argument(
        ...,
        help="Path to the YAML data contract file.",
        exists=True,
        readable=True,
    ),
    raw: bool = typer.Option(False, "--raw", help="Generate the raw (Origin Data Product) layer."),
    staging: bool = typer.Option(False, "--staging", help="Generate the staging (Consumption Data Product) layer."),
    output: Path = typer.Option(
        Path("./models"),
        "--output",
        "-o",
        help="Directory to write generated files into.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be generated without writing."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
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

    target_dataset = event.raw.dataset if layer == Layer.RAW else event.staging.dataset
    target_model = event.raw_model_name if layer == Layer.RAW else event.staging_model_name

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
        status = "[yellow]dry-run[/yellow]" if dry_run else "[green]written[/green]"
        table.add_row(str(path), status)
    console.print(table)

    if result.new_source_columns:
        console.print(
            Panel(
                f"Source has {len(result.new_source_columns)} column(s) not yet in the staging contract:\n"
                f"  {', '.join(result.new_source_columns)}\n\n"
                f"[dim]These flow through raw automatically. Add to the staging contract when "
                f"downstream consumers need them.[/dim]",
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


def main():
    app()


if __name__ == "__main__":
    main()
