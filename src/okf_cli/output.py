"""Human-readable CLI output helpers."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text


@dataclass(frozen=True)
class TableColumn:
    """Display settings for one human-readable table column."""

    header: str
    style: str | None = None
    no_wrap: bool = False
    ratio: int | None = None


def _cell(value: object) -> Text:
    if value is None or value == "":
        return Text("-", style="dim")
    normalized = " ".join(str(value).splitlines()).replace("\t", " ")
    return Text(normalized)


def print_table(columns: Sequence[TableColumn], rows: Iterable[Sequence[object]]) -> None:
    """Print rows as a terminal-width-aware table."""

    table = Table(
        box=box.ROUNDED,
        header_style="bold",
        safe_box=True,
    )
    for column in columns:
        table.add_column(
            column.header,
            style=column.style,
            no_wrap=column.no_wrap,
            overflow="ellipsis" if column.no_wrap else "fold",
            ratio=column.ratio,
        )
    for row in rows:
        table.add_row(*(_cell(value) for value in row))
    Console(highlight=False).print(table)
