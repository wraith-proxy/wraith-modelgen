"""Render a whole project from its .modelgen.yml: `run` and `check`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from modelgen.config import ModelgenConfig
from modelgen.contract import ContractError, EventContract
from modelgen.exit_codes import ExitCode, worst
from modelgen.generator import (
    GenerationError,
    Layer,
    generate,
    write_generated,
)
from modelgen.introspect import IntrospectionError, IntrospectorChooser
from modelgen.lint import Finding, lint_contract
from modelgen.lint import errors as lint_errors


class FileStatus(str, Enum):
    WRITTEN = "written"
    DRY_RUN = "dry_run"
    IN_SYNC = "in_sync"
    DRIFT = "drift"
    MISSING = "missing"


@dataclass(frozen=True)
class FileOutcome:
    contract: Path
    layer: str
    path: Path
    content: str
    status: FileStatus
    on_disk: str | None = None


@dataclass(frozen=True)
class Problem:
    contract: Path
    label: str
    layer: str
    message: str
    exit_code: ExitCode
    issues: tuple = ()


@dataclass
class ProjectResult:
    contracts: list[Path]
    files: list[FileOutcome]
    problems: list[Problem]
    unmapped_rules: list[str]
    new_source_columns: list[tuple[str, list[str]]]

    @property
    def exit_code(self) -> ExitCode:
        codes = [p.exit_code for p in self.problems]
        if any(
            f.status in (FileStatus.DRIFT, FileStatus.MISSING)
            for f in self.files
        ):
            codes.append(ExitCode.DRIFT)
        return worst(codes)


@dataclass(frozen=True)
class _Unit:
    """Files rendered for one contract layer, written or compared as one."""

    contract: Path
    label: str
    layer: str
    files: list[tuple[Path, str]]
    roots: tuple[Path | None, ...]
    new_source_columns: tuple[str, list[str]] | None = None


@dataclass
class _Rendered:
    contracts: list[Path] = field(default_factory=list)
    items: list[_Unit | Problem] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)


def _lint_errors(event: EventContract) -> list[Finding]:
    return lint_errors(lint_contract(event))


def _dest(output: Path, filename: str, tests_output: Path | None) -> Path:
    """Where a rendered file lands; `tests/...` moves under tests_output."""
    parts = Path(filename).parts
    if tests_output is not None and parts[0] == "tests":
        return tests_output.joinpath(*parts[1:])
    return output / filename


def _produced_files(
    cfg: ModelgenConfig, server: str | None, unmapped: list[str]
):
    """Yield (contract, mode, destination, content).

    On a contract error the destination is the error and content is "".

    Covers the produced (`schema`) and extract (`sources`) modes, for the
    contracts whose `*_output` key is set.
    """
    from modelgen.schema_mode import render_schema
    from modelgen.sources_mode import render_sources

    for files, out_dir, mode in (
        (cfg.schema_contract_files(), cfg.schema_output, "schema"),
        (cfg.sources_contract_files(), cfg.sources_output, "sources"),
    ):
        if not files or out_dir is None:
            continue
        for contract_path in files:
            try:
                if mode == "schema":
                    rendered = render_schema(
                        contract_path, server=server, unmapped=unmapped
                    )
                else:
                    rendered = render_sources(contract_path, unmapped=unmapped)
            except ContractError as e:
                yield contract_path, mode, e, ""
                continue
            for filename, content in rendered.items():
                dest = _dest(out_dir, filename, cfg.tests_output)
                yield contract_path, mode, dest, content


def _render(
    cfg: ModelgenConfig,
    *,
    server: str | None,
    from_contract: bool,
    notice: str,
    notify: Callable[[str], None],
    produced_first: bool,
) -> _Rendered:
    chooser = IntrospectorChooser(from_contract, notice, notify)
    events = _Rendered()
    for contract_path in cfg.contract_files():
        events.contracts.append(contract_path)
        try:
            parsed = EventContract.from_file_all(contract_path, server=server)
        except ContractError as e:
            events.items.append(
                Problem(
                    contract_path,
                    contract_path.name,
                    "parse",
                    str(e),
                    ExitCode.INVALID,
                    tuple(e.issues),
                )
            )
            continue

        for event in parsed:
            label = (
                contract_path.name
                if len(parsed) == 1
                else f"{contract_path.name}:{event.name}"
            )

            lint_errs = _lint_errors(event)
            if lint_errs:
                events.items.append(
                    Problem(
                        contract_path,
                        label,
                        "lint",
                        "; ".join(f.message for f in lint_errs),
                        ExitCode.LINT,
                        tuple(lint_errs),
                    )
                )
                continue

            introspector = chooser.for_event(event)

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
                    events.items.append(
                        Problem(
                            contract_path,
                            label,
                            layer.value,
                            str(e),
                            ExitCode.ENVIRONMENT
                            if isinstance(e, IntrospectionError)
                            else ExitCode.INVALID,
                        )
                    )
                    continue
                events.items.append(
                    _Unit(
                        contract_path,
                        label,
                        layer.value,
                        [
                            (output_dir / name, content)
                            for name, content in result.artefacts.items()
                        ],
                        (output_dir,),
                        (event.name, result.new_source_columns)
                        if result.new_source_columns
                        else None,
                    )
                )

    produced = _Rendered()
    for contract_path, mode, dest, content in _produced_files(
        cfg, server, produced.unmapped
    ):
        if contract_path not in produced.contracts:
            produced.contracts.append(contract_path)
        if isinstance(dest, ContractError):
            produced.items.append(
                Problem(
                    contract_path,
                    contract_path.name,
                    "parse",
                    str(dest),
                    ExitCode.INVALID,
                    tuple(dest.issues),
                )
            )
            continue
        produced.items.append(
            _Unit(
                contract_path,
                contract_path.name,
                mode,
                [(dest, content)],
                (cfg.schema_output, cfg.sources_output, cfg.tests_output),
            )
        )

    first, second = (
        (produced, events) if produced_first else (events, produced)
    )
    return _Rendered(
        first.contracts + second.contracts,
        first.items + second.items,
        produced.unmapped,
    )


def _empty_result(rendered: _Rendered) -> ProjectResult:
    return ProjectResult(rendered.contracts, [], [], rendered.unmapped, [])


def run_project(
    cfg: ModelgenConfig,
    *,
    server: str | None,
    from_contract: bool,
    dry_run: bool,
    notify: Callable[[str], None],
) -> ProjectResult:
    """Generate every file .modelgen.yml names; write unless dry_run."""
    rendered = _render(
        cfg,
        server=server,
        from_contract=from_contract,
        notice="No GCP credentials found. Generating from contracts only.",
        notify=notify,
        produced_first=False,
    )
    result = _empty_result(rendered)
    status = FileStatus.DRY_RUN if dry_run else FileStatus.WRITTEN
    for item in rendered.items:
        if isinstance(item, Problem):
            result.problems.append(item)
            continue
        if not dry_run:
            try:
                for dest, content in item.files:
                    write_generated(dest, content, *item.roots)
            except GenerationError as e:
                result.problems.append(
                    Problem(
                        item.contract,
                        item.label,
                        item.layer,
                        str(e),
                        ExitCode.INVALID,
                    )
                )
                continue
        result.files.extend(
            FileOutcome(item.contract, item.layer, dest, content, status)
            for dest, content in item.files
        )
        if item.new_source_columns:
            result.new_source_columns.append(item.new_source_columns)
    return result


def check_project(
    cfg: ModelgenConfig,
    *,
    server: str | None,
    from_contract: bool,
    notify: Callable[[str], None],
) -> ProjectResult:
    """Compare every file .modelgen.yml names against what is on disk."""
    rendered = _render(
        cfg,
        server=server,
        from_contract=from_contract,
        notice="No GCP credentials found. Checking against contracts only.",
        notify=notify,
        produced_first=True,
    )
    result = _empty_result(rendered)
    for item in rendered.items:
        if isinstance(item, Problem):
            result.problems.append(item)
            continue
        for dest, content in item.files:
            on_disk = dest.read_text() if dest.exists() else None
            if on_disk is None:
                status = FileStatus.MISSING
            elif on_disk == content:
                status, on_disk = FileStatus.IN_SYNC, None
            else:
                status = FileStatus.DRIFT
            result.files.append(
                FileOutcome(
                    item.contract, item.layer, dest, content, status, on_disk
                )
            )
        if item.new_source_columns:
            result.new_source_columns.append(item.new_source_columns)
    return result
