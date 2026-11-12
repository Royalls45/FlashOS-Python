from __future__ import annotations

import ctypes
import os
from pathlib import Path

from flashos.models import DiskInfo

DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
BUS_TYPE_USB = 7
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class UsbScanner:
    """Enumerate ready drive-letter volumes and identify USB-backed fixed disks."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("FlashOS поддерживает обнаружение дисков только в Windows.")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_api()

    def scan(self) -> list[DiskInfo]:
        mask = self._kernel32.GetLogicalDrives()
        devices: list[DiskInfo] = []
        for index in range(26):
            if not mask & (1 << index):
                continue
            root = f"{chr(ord('A') + index)}:\\"
            drive_type = self._kernel32.GetDriveTypeW(root)
            if drive_type not in (DRIVE_REMOVABLE, DRIVE_FIXED):
                continue
            removable = drive_type == DRIVE_REMOVABLE
            if not removable and not self._is_usb_volume(root):
                continue
            details = self._volume_details(root)
            if details is not None:
                devices.append(DiskInfo(Path(root), *details, removable=removable))
        return sorted(devices, key=lambda item: str(item.root).casefold())

    def _configure_api(self) -> None:
        kernel = self._kernel32
        kernel.GetLogicalDrives.restype = ctypes.c_uint32
        kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        kernel.GetDriveTypeW.restype = ctypes.c_uint32
        kernel.GetVolumeInformationW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_wchar_p, ctypes.c_uint32,
        ]
        kernel.GetVolumeInformationW.restype = ctypes.c_int
        kernel.GetDiskFreeSpaceExW.argtypes = [
            ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64),
        ]
        kernel.GetDiskFreeSpaceExW.restype = ctypes.c_int
        kernel.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.DeviceIoControl.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
        ]
        kernel.DeviceIoControl.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int

    def _volume_details(self, root: str) -> tuple[str, str, int, int] | None:
        label = ctypes.create_unicode_buffer(261)
        filesystem = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_uint32()
        max_component = ctypes.c_uint32()
        flags = ctypes.c_uint32()
        if not self._kernel32.GetVolumeInformationW(
            root, label, len(label), ctypes.byref(serial), ctypes.byref(max_component),
            ctypes.byref(flags), filesystem, len(filesystem),
        ):
            return None
        free_to_caller = ctypes.c_uint64()
        total = ctypes.c_uint64()
        free_total = ctypes.c_uint64()
        if not self._kernel32.GetDiskFreeSpaceExW(
            root, ctypes.byref(free_to_caller), ctypes.byref(total), ctypes.byref(free_total)
        ):
            return None
        return label.value, filesystem.value, total.value, free_to_caller.value

    def _is_usb_volume(self, root: str) -> bool:
        volume_device = f"\\\\.\\{root.rstrip('\\')}"
        handle = self._kernel32.CreateFileW(
            volume_device, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
            OPEN_EXISTING, 0, None,
        )
        if handle == INVALID_HANDLE_VALUE:
            return False
        try:
            query = ctypes.create_string_buffer(12)
            descriptor = ctypes.create_string_buffer(1024)
            returned = ctypes.c_uint32()
            success = self._kernel32.DeviceIoControl(
                handle, IOCTL_STORAGE_QUERY_PROPERTY, query, len(query), descriptor,
                len(descriptor), ctypes.byref(returned), None,
            )
            if not success or returned.value < 32:
                return False
            bus_type = ctypes.c_uint32.from_buffer(descriptor, 28).value
            return bus_type == BUS_TYPE_USB
        finally:
            self._kernel32.CloseHandle(handle)
