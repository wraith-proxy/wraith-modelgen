from collections.abc import Iterable
from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    INVALID = 1
    USAGE = 2
    LINT = 3
    DRIFT = 4
    ENVIRONMENT = 5
    INTERNAL = 6


_PRECEDENCE = (
    ExitCode.INTERNAL,
    ExitCode.ENVIRONMENT,
    ExitCode.INVALID,
    ExitCode.LINT,
    ExitCode.DRIFT,
)


def worst(codes: Iterable[ExitCode]) -> ExitCode:
    """The code a run exits with when it hit several failure classes."""
    present = set(codes)
    return next((c for c in _PRECEDENCE if c in present), ExitCode.OK)
