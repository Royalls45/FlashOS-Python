from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

SECTOR_SIZE = 2048
MAX_BOOT_FILE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class IsoBootAssets:
    kernel: Path
    initrd: Path


class IsoBootAssetExtractor:
    """Extract the Linux Mint Casper kernel and initrd from an ISO9660 image."""

    def __init__(self, cache_root: Path | None = None) -> None:
        app_data = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        self.cache_root = cache_root or app_data / "FlashOS-Python" / "boot-cache"

    def extract(self, iso_path: Path) -> IsoBootAssets:
        iso_path = iso_path.resolve(strict=True)
        if not iso_path.is_file() or iso_path.suffix.casefold() != ".iso":
            raise ValueError("Выбранный ISO недоступен.")
        stat = iso_path.stat()
        key_data = f"{str(iso_path).casefold()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
        cache_dir = self.cache_root / hashlib.sha256(key_data).hexdigest()[:20]
        kernel_target = cache_dir / "vmlinuz"
        initrd_target = cache_dir / "initrd"
        if kernel_target.is_file() and initrd_target.is_file():
            return IsoBootAssets(kernel_target, initrd_target)

        with iso_path.open("rb") as image:
            root = self._find_root_directory(image)
            casper = next(
                (entry for entry in self._read_directory(image, root) if entry[0].casefold() == "casper" and entry[3]),
                None,
            )
            if casper is None:
                raise ValueError("В ISO не найден каталог casper; Persistence-запуск недоступен.")
            files = {name.casefold(): (extent, size) for name, extent, size, is_dir in self._read_directory(image, casper) if not is_dir}
            kernel = files.get("vmlinuz")
            initrd = next((files[name] for name in ("initrd.lz", "initrd", "initrd.gz") if name in files), None)
            if kernel is None or initrd is None:
                raise ValueError("В ISO не найдены casper/vmlinuz и/или initrd.lz.")
            for name, (extent, size) in (("vmlinuz", kernel), ("initrd", initrd)):
                if size <= 0 or size > MAX_BOOT_FILE_BYTES or extent * SECTOR_SIZE + size > stat.st_size:
                    raise ValueError(f"Недопустимый размер или смещение файла casper/{name} в ISO.")

            cache_dir.mkdir(parents=True, exist_ok=True)
            self._extract_file(image, kernel, kernel_target)
            self._extract_file(image, initrd, initrd_target)

        return IsoBootAssets(kernel_target, initrd_target)

    @staticmethod
    def _find_root_directory(image) -> tuple[int, int]:
        for sector in range(16, 64):
            image.seek(sector * SECTOR_SIZE)
            descriptor = image.read(SECTOR_SIZE)
            if len(descriptor) != SECTOR_SIZE or descriptor[1:6] != b"CD001":
                continue
            if descriptor[0] == 255:
                break
            if descriptor[0] == 1:
                record = descriptor[156:]
                extent = int.from_bytes(record[2:6], "little")
                size = int.from_bytes(record[10:14], "little")
                if extent and size:
                    return extent, size
        raise ValueError("ISO не содержит корректный каталог ISO9660.")

    @staticmethod
    def _read_directory(image, location: tuple[int, int]) -> list[tuple[str, int, int, bool]]:
        extent, size = location
        image.seek(extent * SECTOR_SIZE)
        data = image.read(size)
        if len(data) != size:
            raise ValueError("Не удалось полностью прочитать каталог ISO.")
        entries: list[tuple[str, int, int, bool]] = []
        offset = 0
        while offset < len(data):
            record_size = data[offset]
            if record_size == 0:
                offset = ((offset // SECTOR_SIZE) + 1) * SECTOR_SIZE
                continue
            record = data[offset:offset + record_size]
            if len(record) < 34 or record_size < 34:
                raise ValueError("Некорректная запись каталога ISO.")
            name_size = record[32]
            name_bytes = record[33:33 + name_size]
            if not name_bytes or name_bytes in (b"\x00", b"\x01"):
                offset += record_size
                continue
            name = name_bytes.decode("ascii", errors="replace")
            if ";" in name:
                name = name.split(";", 1)[0]
            if name.endswith("."):
                name = name[:-1]
            child_extent = int.from_bytes(record[2:6], "little")
            child_size = int.from_bytes(record[10:14], "little")
            is_dir = bool(record[25] & 0x02)
            entries.append((name, child_extent, child_size, is_dir))
            offset += record_size
        return entries

    @staticmethod
    def _extract_file(image, source: tuple[int, int], target: Path) -> None:
        extent, size = source
        partial = target.with_suffix(target.suffix + ".partial")
        image.seek(extent * SECTOR_SIZE)
        remaining = size
        try:
            with partial.open("wb") as output:
                while remaining:
                    chunk = image.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("ISO завершился во время извлечения файлов загрузки.")
                    output.write(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(partial, target)
        finally:
            if partial.exists():
                partial.unlink()
