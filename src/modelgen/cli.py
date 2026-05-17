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
        "[bold cyan]👻[/bold cyan] Feed it a YAML data contract."
        " Get back sovereign dbt models that won't ghost you at 3am.\n\n"
        "One layer per run. No hand-holding. BigQuery introspected."
        " Drift-proof by design."
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
        help="Path to the YAML data contract. The contract is law.",
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
        " Source schema drama stays upstream.",
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
):
    """
    Generate a dbt model layer from a YAML data contract.

    Pick a lane: [bold]--raw[/bold] or [bold]--staging[/bold].
    One layer per run. That's the deal.

    \b
    Examples:
        modelgen gen contract.yml --raw -o ./models/raw
        modelgen gen contract.yml --staging -o ./models/staging
        modelgen gen contract.yml --raw --dry-run
    """
    _configure_logging(verbose)

    if raw == staging:
        console.print(
            "[red]Pick a lane.[/red]"
            " [dim]--raw or --staging. One. Never both, never neither.[/dim]"
        )
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
            f"[bold]Event:[/bold]      {event.name}\n"
            f"[bold]Layer:[/bold]      {layer.value}\n"
            f"[bold]Source:[/bold]     {event.source.fqn}\n"
            f"[bold]Target:[/bold]     {target_dataset}.{target_model}\n"
            f"[bold]Unique key:[/bold] {', '.join(event.unique_key)}"
            f"  [bold]Loaded at:[/bold] {event.loaded_at_field}",
            title="[bold green]👻 modelgen[/bold green]",
            subtitle="[dim]introspecting BigQuery…[/dim]",
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
            "[dim]That one's on us."
            " Run with --verbose and file a report.[/dim]"
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
                "[dim]Flowing through raw untouched. Source schema drama,"
                " not yours. Add to staging when consumers need it.[/dim]",
                title="[yellow]⚠ Schema evolution detected[/yellow]",
                border_style="yellow",
            )
        )


@app.command()
def validate(
    contract: Path = typer.Argument(
        ...,
        help="Path to the YAML data contract. No BigQuery. No mercy.",
        exists=True,
        readable=True,
    ),
):
    """
    Interrogate a contract's YAML structure.

    No BigQuery credentials required. No tolerance for malformed YAML either.
    Fast, offline, brutal — fix the contract before it fixes you.
    """
    try:
        event = EventContract.from_file(contract)
        console.print(
            f"[green]OK[/green] [bold]{event.name}[/bold] is not broken."
            f" Today.\n"
            f"  Source:   {event.source.fqn}\n"
            f"  Key:      {', '.join(event.unique_key)}\n"
            f"  Columns:  {len(event.staging.columns)} staging declared"
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
    Sweep the repo. Generate everything. Touch grass afterward.

    Finds every contract, introspects BigQuery for each, and writes both
    layers without asking you to hold its hand. If something's broken,
    it tells you — loudly, and by name.

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
            f"[bold]Contracts:[/bold]       {len(contract_files)}\n"
            f"[bold]Root:[/bold]            {cfg.root}\n"
            f"[bold]Raw output:[/bold]      {cfg.raw_output}\n"
            f"[bold]Staging output:[/bold]  {cfg.staging_output}",
            title="[bold green]👻 modelgen run[/bold green]",
            subtitle="[dim]buckle up[/dim]",
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
            console.print(f"[red]Error[/red] [{layer_name}] {name}: {msg}")
        raise typer.Exit(1)


def main():
    app()


if __name__ == "__main__":
    main()
