"""Diagnostics shared by validation and relationship checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    file: str
    message: str
    field: str | None = None
    line: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def as_text(self) -> str:
        location = f":{self.line}" if self.line else ""
        field = f" [{self.field}]" if self.field else ""
        return f"{self.severity}: {self.file}{location}{field}: {self.message}"
