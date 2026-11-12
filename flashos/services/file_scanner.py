from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event

from flashos.models import DiscoveredFile

IGNORED_DIRECTORIES = {"$RECYCLE.BIN", "SYSTEM VOLUME INFORMATION", "$EXTEND"}
MAX_SCAN_SECONDS = 60
MAX_DIRECTORIES = 50_000
MAX_DEPTH = 24
REPARSE_POINT = 0x400


class ScanCancelled(Exception):
    pass


class FileScanner:
    def scan(
        self,
        root: Path,
        cancel: Event,
        on_warning: Callable[[str], None] | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> list[DiscoveredFile]:
        started = time.monotonic()
        pending: list[tuple[Path, int]] = [(root, 0)]
        found: list[DiscoveredFile] = []
        visited = 0
        warn = on_warning or (lambda _message: None)

        while pending:
            if cancel.is_set():
                raise ScanCancelled("Сканирование отменено.")
            if time.monotonic() - started > MAX_SCAN_SECONDS:
                warn(f"Сканирование остановлено по ограничению времени ({MAX_SCAN_SECONDS} с).")
                break
            if visited >= MAX_DIRECTORIES:
                warn(f"Достигнут лимит каталогов ({MAX_DIRECTORIES}).")
                break

            directory, depth = pending.pop()
            visited += 1
            if on_progress and visited % 100 == 0:
                on_progress(visited)
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if cancel.is_set():
                            raise ScanCancelled("Сканирование отменено.")
                        try:
                            stat = entry.stat(follow_symlinks=False)
                            attributes = getattr(stat, "st_file_attributes", 0)
                            if entry.is_symlink() or attributes & REPARSE_POINT:
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if depth < MAX_DEPTH and entry.name.upper() not in IGNORED_DIRECTORIES:
                                    pending.append((Path(entry.path), depth + 1))
                                continue
                            kind = self._classify(entry.name)
                            if kind is None:
                                continue
                            path = Path(entry.path)
                            # Opening the file proves basic readability without loading large ISOs.
                            with open(path, "rb") as stream:
                                stream.read(1)
                            found.append(DiscoveredFile(path, kind, stat.st_size, stat.st_mtime_ns))
                        except (OSError, PermissionError) as exc:
                            warn(f"Не удалось проверить {entry.path}: {exc}")
            except (OSError, PermissionError) as exc:
                warn(f"Не удалось прочитать {directory}: {exc}")

        return sorted(found, key=lambda item: (item.kind != "ISO-образ", item.name.casefold(), str(item.path).casefold()))

    @staticmethod
    def _classify(name: str) -> str | None:
        lowered = name.casefold()
        if lowered.endswith(".iso"):
            return "ISO-образ"
        if lowered == "persistence.bat":
            return "BAT-скрипт (не запускался)"
        if lowered.endswith(".dat"):
            return "Файл .dat (формат не подтверждён)"
        if lowered == "ventoy.json":
            return "Конфигурация Ventoy"
        return None
