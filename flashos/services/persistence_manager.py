from __future__ import annotations

import json
import os
from pathlib import Path

from flashos.models import CompatibilityResult, DiscoveredFile, PersistenceReport

MAX_CONFIG_BYTES = 1024 * 1024
MAX_SCRIPT_BYTES = 256 * 1024
MAX_SCRIPT_PREVIEW_CHARS = 8_000
REPARSE_POINT = 0x400


class PersistenceManager:
    def analyze(self, root: Path, files: list[DiscoveredFile]) -> tuple[list[PersistenceReport], str]:
        reports: list[PersistenceReport] = []
        configs = [item for item in files if item.kind == "Конфигурация Ventoy"]
        iso_paths = {item.path.resolve() for item in files if item.kind == "ISO-образ"}
        if not configs:
            reports.append(PersistenceReport("ventoy.json", "Не найдено", "Конфигурация Ventoy не обнаружена."))
        for config in configs:
            reports.append(self._analyze_config(root, config.path, iso_paths))

        data_files = [item for item in files if item.kind.startswith("Файл .dat")]
        if not data_files:
            reports.append(PersistenceReport("Файлы .dat", "Не найдено", "Файлы с расширением .dat не обнаружены."))
        else:
            reports.append(PersistenceReport(
                "Файлы .dat", "Обнаружено",
                f"Найдено: {len(data_files)}. Формат каждого файла проверяется отдельно; совместимость не подтверждена.",
            ))
            for item in data_files:
                label = self._ext_filesystem_label(item.path)
                if label:
                    reports.append(PersistenceReport(str(item.path), "Формат распознан", f"ext filesystem, метка {label}; Persistence не проверен."))

        scripts = [item for item in files if item.kind.startswith("BAT-скрипт")]
        if not scripts:
            reports.append(PersistenceReport("BAT-скрипты", "Не найдено", "Скрипты не обнаружены."))
            preview = "BAT-скрипт не найден."
        else:
            reports.append(PersistenceReport("BAT-скрипты", "Только чтение", f"Найдено: {len(scripts)}. Скрипты не запускались."))
            preview = self._read_script(scripts[0].path)
            if len(scripts) > 1:
                preview = f"Показан первый из {len(scripts)} скриптов: {scripts[0].path}\n\n{preview}"
        return reports, preview

    def compatibility_for(self, root: Path, iso_path: Path, files: list[DiscoveredFile]) -> CompatibilityResult:
        """Find matching metadata but never promote it to tested support."""
        iso_full = iso_path.resolve()
        iso_candidates = {item.path.resolve() for item in files if item.kind == "ISO-образ"}
        if iso_full not in iso_candidates:
            return CompatibilityResult("ISO не проверен", "Выбранный ISO не входит в результаты последнего сканирования.")

        for item in files:
            if item.kind != "Конфигурация Ventoy":
                continue
            try:
                config = self._read_config(item.path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return CompatibilityResult("Ошибка конфигурации", str(exc))
            entries = config.get("persistence", []) if isinstance(config, dict) else []
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                image = entry.get("image")
                if not isinstance(image, str):
                    continue
                if self._resolve_usb_path(root, image) != iso_full:
                    continue
                for backend in self._backend_values(entry.get("backend")):
                    path = self._resolve_usb_path(root, backend)
                    if path is None or not path.is_file():
                        continue
                    label = self._ext_filesystem_label(path)
                    detail = f"ISO и backend указаны вместе; метка {label or 'не распознана'}. Сохранение после перезапуска ещё не проверено."
                    return CompatibilityResult("Кандидат найден, не проверено", detail, supported=False)
                return CompatibilityResult("Backend не найден", "Конфигурация ссылается на backend, которого нет на томе.")
        return CompatibilityResult("Не подтверждено", "Нет точной записи, связывающей выбранный ISO с backend.")

    def _analyze_config(self, root: Path, path: Path, iso_paths: set[Path]) -> PersistenceReport:
        try:
            config = self._read_config(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return PersistenceReport(str(path), "Ошибка JSON", f"Не удалось прочитать конфигурацию: {exc}")
        if not isinstance(config, dict) or not isinstance(config.get("persistence"), list):
            return PersistenceReport(str(path), "Формат не распознан", "Ожидается массив persistence в корне JSON.")
        entries = config["persistence"]
        if not entries:
            return PersistenceReport(str(path), "Настроек нет", "Массив persistence пуст.")

        details: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                details.append("Запись имеет неверный тип.")
                continue
            image = entry.get("image")
            backends = self._backend_values(entry.get("backend"))
            if not isinstance(image, str) or not backends:
                details.append("В записи нет корректных полей image/backend.")
                continue
            image_path = self._resolve_usb_path(root, image)
            if image_path is None:
                details.append(f"Недопустимый или небезопасный путь ISO: {image}.")
                continue
            if image_path in iso_paths:
                image_status = "ISO найден по точному пути"
            elif any(candidate.name.casefold() == image_path.name.casefold() for candidate in iso_paths):
                image_status = "точного пути ISO нет, найден кандидат с таким именем"
            else:
                image_status = "ISO не найден"
            backend_status: list[str] = []
            for backend in backends:
                backend_path = self._resolve_usb_path(root, backend)
                if backend_path is None:
                    backend_status.append(f"небезопасный путь backend {backend}")
                elif backend_path.is_file():
                    label = self._ext_filesystem_label(backend_path)
                    descriptor = f", ext label {label}" if label else ", формат не подтверждён"
                    backend_status.append(f"backend найден ({backend_path.stat().st_size} Б{descriptor})")
                else:
                    backend_status.append(f"backend не найден: {backend}")
            details.append(f"{image_status}: {image}; {'; '.join(backend_status)}.")
        details.append("Наличие конфигурации не доказывает совместимость прямого запуска через QEMU.")
        return PersistenceReport(str(path), "Ссылки проверены", " ".join(details))

    @staticmethod
    def _read_config(path: Path) -> object:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise ValueError("Файл конфигурации больше 1 МБ; чтение пропущено.")
        with path.open("r", encoding="utf-8-sig") as stream:
            return json.load(stream)

    @staticmethod
    def _backend_values(value: object) -> list[str]:
        if isinstance(value, str) and value.strip():
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str) and item.strip()]
        return []

    @staticmethod
    def _resolve_usb_path(root: Path, configured: str) -> Path | None:
        if not configured or "\x00" in configured or configured.startswith(("//", "\\\\")):
            return None
        normalized = configured.replace("\\", "/")
        parts = normalized.lstrip("/").split("/")
        if not parts or any(part in ("", ".", "..") or ":" in part for part in parts):
            return None
        try:
            root_full = root.resolve(strict=True)
            target = root_full.joinpath(*parts)
            resolved = target.resolve(strict=False)
            if os.path.commonpath((str(root_full), str(resolved))).casefold() != str(root_full).casefold():
                return None
            current = root_full
            for part in parts:
                current = current / part
                try:
                    metadata = current.lstat()
                except FileNotFoundError:
                    continue
                if current.is_symlink() or getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT:
                    return None
            return resolved
        except (OSError, ValueError):
            return None

    @staticmethod
    def _ext_filesystem_label(path: Path) -> str | None:
        try:
            with path.open("rb") as stream:
                stream.seek(1080)
                if stream.read(2) != b"\x53\xef":
                    return None
                stream.seek(1144)
                raw = stream.read(16)
            label = raw.split(b"\0", 1)[0].decode("ascii", errors="replace").strip()
            return label or "(без метки)"
        except OSError:
            return None

    @staticmethod
    def _read_script(path: Path) -> str:
        try:
            if path.stat().st_size > MAX_SCRIPT_BYTES:
                return f"Скрипт больше 256 КБ; предпросмотр не загружен.\n{path}"
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("cp1251", errors="replace")
            if len(text) > MAX_SCRIPT_PREVIEW_CHARS:
                text = text[:MAX_SCRIPT_PREVIEW_CHARS] + "\n… предпросмотр ограничен"
            return f"{path}\nТолько текст; выполнение отключено.\n\n{text}"
        except OSError as exc:
            return f"Не удалось прочитать скрипт как текст: {exc}"
