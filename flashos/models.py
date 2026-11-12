from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DiskInfo:
    root: Path
    label: str
    filesystem: str
    total_bytes: int
    free_bytes: int
    removable: bool

    @property
    def display_name(self) -> str:
        label = self.label or "Без метки"
        return f"{self.root}  {label}"


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    path: Path
    kind: str
    size_bytes: int
    modified_ns: int

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def size_text(self) -> str:
        return format_bytes(self.size_bytes)


@dataclass(frozen=True, slots=True)
class PersistenceReport:
    source: str
    status: str
    details: str


@dataclass(frozen=True, slots=True)
class CompatibilityResult:
    status: str
    details: str
    supported: bool = False


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024 or unit == "ТБ":
            return f"{size:.2f}".rstrip("0").rstrip(".") + f" {unit}"
        size /= 1024
    return f"{value} Б"
