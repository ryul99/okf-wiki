"""User-facing exception types and stable exit codes."""


class OkfError(Exception):
    """Base error carrying the process exit code."""

    exit_code = 3


class UsageError(OkfError):
    """Invalid command arguments or unsafe requested path."""

    exit_code = 2


class OperationalError(OkfError):
    """Filesystem or parsing failure."""

    exit_code = 3


class CheckError(OkfError):
    """A validation or requested check found errors."""

    exit_code = 1
