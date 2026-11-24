"""Render a whole project from its .modelgen.yml: `run` and `check`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from modelgen.config import ModelgenConfig
from modelgen.contract import ContractError, EventContract, Issue
from modelgen.exit_codes import ExitCode, worst
from modelgen.generator import (
    GenerationError,
    Layer,
    generate,
    mask_version,
    write_generated,
)
from modelgen.introspect import IntrospectionError, IntrospectorChooser
from modelgen.lint import errors as lint_errors
from modelgen.lint import lint_contract


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
    event: str | None = None
    code: str = "contract"


@dataclass
class ProjectResult:
    contracts: list[Path]
    files: list[FileOutcome]
    problems: list[Problem]
    unmapped_rules: list[str]
    new_source_columns: list[tuple[str, list[str]]]
    notices: list[tuple[Path, Issue]] = field(default_factory=list)
    unmapped_by_contract: list[tuple[Path, str]] = field(default_factory=list)

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
    unmapped: list[tuple[Path, str]] = field(default_factory=list)
    notices: list[tuple[Path, Issue]] = field(default_factory=list)


def failure_class(e: GenerationError | IntrospectionError):
    """The exit code and issue code for a failed generation."""
    if isinstance(e, IntrospectionError):
        return ExitCode.ENVIRONMENT, "introspection"
    return ExitCode.INVALID, "generation"


def dest_path(output: Path, filename: str, tests_output: Path | None) -> Path:
    """Where a rendered file lands; `tests/...` moves under tests_output."""
    parts = Path(filename).parts
    if tests_output is not None and parts[0] == "tests":
        return tests_output.joinpath(*parts[1:])
    return output / filename


def _produced_files(
    cfg: ModelgenConfig,
    server: str | None,
    unmapped: list[tuple[Path, str]],
    notices: list[tuple[Path, Issue]],
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
            found: list[Issue] = []
            found_unmapped: list[str] = []
            try:
                if mode == "schema":
                    rendered = render_schema(
                        contract_path,
                        server=server,
                        unmapped=found_unmapped,
                        notices=found,
                    )
                else:
                    rendered = render_sources(
                        contract_path, unmapped=found_unmapped, notices=found
                    )
            except ContractError as e:
                yield contract_path, mode, e, ""
                continue
            notices.extend((contract_path, n) for n in found)
            unmapped.extend((contract_path, r) for r in found_unmapped)
            for filename, content in rendered.items():
                dest = dest_path(out_dir, filename, cfg.tests_output)
                yield contract_path, mode, dest, content


def _package_notices(
    cfg: ModelgenConfig, items: list[_Unit | Problem]
) -> list[tuple[Path, Issue]]:
    if not any(
        "dbt_expectations." in content
        for item in items
        if isinstance(item, _Unit)
        for _, content in item.files
    ):
        return []
    outputs = (
        cfg.raw_output,
        cfg.staging_output,
        cfg.schema_output,
        cfg.sources_output,
        cfg.tests_output,
    )
    root = cfg.root.resolve()
    for out in filter(None, outputs):
        out = out.resolve()
        project = next(
            (
                d
                for d in [out, *out.parents]
                if d.is_relative_to(root) and (d / "dbt_project.yml").is_file()
            ),
            None,
        )
        if project and not _installs_expectations(project):
            return [
                (
                    project / "dbt_project.yml",
                    Issue(
                        "dbt-package-missing",
                        f"dbt project {project} does not install"
                        " dbt_expectations, which the generated tests call."
                        " Add it to packages.yml",
                        severity="warning",
                    ),
                )
            ]
    return []


def _installs_expectations(directory: Path) -> bool:
    return (directory / "dbt_packages" / "dbt_expectations").is_dir() or any(
        "dbt_expectations" in (directory / name).read_text()
        for name in ("packages.yml", "dependencies.yml")
        if (directory / name).is_file()
    )


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

        if parsed:
            events.notices.extend(
                (contract_path, n) for n in parsed[0].notices
            )
        for event in parsed:
            label = (
                contract_path.name
                if len(parsed) == 1
                else f"{contract_path.name}:{event.name}"
            )

            lint_errs = lint_errors(lint_contract(event))
            if lint_errs:
                events.items.append(
                    Problem(
                        contract_path,
                        label,
                        "lint",
                        "; ".join(f.message for f in lint_errs),
                        ExitCode.LINT,
                        tuple(lint_errs),
                        event.name,
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
                    code, issue_code = failure_class(e)
                    events.items.append(
                        Problem(
                            contract_path,
                            label,
                            layer.value,
                            str(e),
                            code,
                            event=event.name,
                            code=issue_code,
                        )
                    )
                    continue
                if layer == Layer.STAGING:
                    events.unmapped.extend(
                        (contract_path, r) for r in event.unmapped_rules
                    )
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
        cfg, server, produced.unmapped, produced.notices
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
        first.unmapped + second.unmapped,
        cfg.notices
        + first.notices
        + second.notices
        + _package_notices(cfg, first.items + second.items),
    )


def _empty_result(rendered: _Rendered) -> ProjectResult:
    return ProjectResult(
        rendered.contracts,
        [],
        [],
        [rule for _, rule in rendered.unmapped],
        [],
        rendered.notices,
        rendered.unmapped,
    )


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
                        code="generation",
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
    ignore_version: bool = False,
) -> ProjectResult:
    """Compare every file .modelgen.yml names against what is on disk.

    With `ignore_version`, a file that differs only in the modelgen
    version of its header is in sync.
    """
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
            elif on_disk == content or (
                ignore_version
                and mask_version(on_disk) == mask_version(content)
            ):
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
