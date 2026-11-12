from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass(slots=True)
class AppSettings:
    qemu_path: str = ""
    memory_mb: int = 4096
    cpu_count: int = 2
    last_iso: str = ""
    last_drive: str = ""


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        self.path = path or base / "FlashOS-Python" / "settings.json"

    def load(self) -> tuple[AppSettings, str | None]:
        if not self.path.exists():
            return AppSettings(), None
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
            version = payload.get("schema_version") if isinstance(payload, dict) else None
            if version != SCHEMA_VERSION:
                return AppSettings(), f"Версия настроек {version!r} не поддерживается; используются значения по умолчанию."
            values = payload.get("settings", {})
            return AppSettings(
                qemu_path=str(values.get("qemu_path", "")),
                memory_mb=self._bounded_int(values.get("memory_mb"), 4096, 512, 131072),
                cpu_count=self._bounded_int(values.get("cpu_count"), 2, 1, 64),
                last_iso=str(values.get("last_iso", "")),
                last_drive=str(values.get("last_drive", "")),
            ), None
        except (OSError, ValueError, TypeError) as exc:
            return AppSettings(), f"Не удалось прочитать settings.json: {exc}"

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": SCHEMA_VERSION, "settings": asdict(settings)}
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="\n", dir=self.path.parent,
                prefix="settings.", suffix=".tmp", delete=False,
            ) as stream:
                temporary = stream.name
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if minimum <= parsed <= maximum else default
