from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Signal


class QemuController(QObject):
    state_changed = Signal(str)
    output_received = Signal(str)
    finished = Signal(int, str)
    error_occurred = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._process: QProcess | None = None
        self._qmp_port: int | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.state() != QProcess.ProcessState.NotRunning

    @staticmethod
    def probe(executable: str) -> str:
        path = Path(executable)
        if not path.is_file() or path.name.casefold() != "qemu-system-x86_64.exe":
            raise ValueError("Выберите qemu-system-x86_64.exe.")
        result = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True,
            timeout=6, check=False, shell=False,
        )
        output = (result.stdout or result.stderr).strip()
        if result.returncode != 0 or not output.casefold().startswith("qemu emulator version"):
            raise RuntimeError(f"QEMU не прошёл проверку: {output or result.returncode}")
        return output.splitlines()[0]

    def start_iso(self, executable: str, iso_path: str, memory_mb: int, cpu_count: int) -> None:
        if self.running:
            raise RuntimeError("Виртуальная машина уже запущена.")
        qemu = Path(executable).resolve(strict=True)
        iso = Path(iso_path).resolve(strict=True)
        if qemu.name.casefold() != "qemu-system-x86_64.exe":
            raise ValueError("Выберите qemu-system-x86_64.exe.")
        if not iso.is_file() or iso.suffix.casefold() != ".iso":
            raise ValueError("Выберите доступный ISO-файл.")
        if not 512 <= memory_mb <= 131072:
            raise ValueError("Память должна быть в диапазоне 512–131072 МБ.")
        if not 1 <= cpu_count <= 64:
            raise ValueError("Число CPU должно быть в диапазоне 1–64.")

        self._qmp_port = self._find_free_port()
        arguments = [
            "-accel", "tcg", "-m", str(memory_mb), "-smp", str(cpu_count),
            "-cdrom", str(iso), "-boot", "order=d", "-display", "sdl",
            "-vga", "std", "-nic", "none", "-qmp",
            f"tcp:127.0.0.1:{self._qmp_port},server=on,wait=off",
        ]
        process = QProcess(self)
        process.setProgram(str(qemu))
        process.setArguments(arguments)
        process.setWorkingDirectory(str(qemu.parent))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self._read_output)
        process.started.connect(lambda: self.state_changed.emit("VM запущена"))
        process.errorOccurred.connect(self._on_error)
        process.finished.connect(self._on_finished)
        self._process = process
        self.state_changed.emit("Запуск QEMU…")
        process.start()

    def request_guest_shutdown(self) -> None:
        if not self.running or self._qmp_port is None:
            raise RuntimeError("Виртуальная машина не запущена.")
        with socket.create_connection(("127.0.0.1", self._qmp_port), timeout=4) as connection:
            connection.settimeout(4)
            reader = connection.makefile("rb")
            greeting = self._read_qmp_object(reader)
            if "QMP" not in greeting:
                raise RuntimeError("QEMU не прислал приветствие QMP.")
            self._qmp_command(connection, reader, {"execute": "qmp_capabilities"})
            response = self._qmp_command(connection, reader, {"execute": "system_powerdown"})
            if "error" in response:
                error = response["error"].get("desc", "неизвестная ошибка")
                raise RuntimeError(f"QEMU отклонил штатное выключение: {error}")
        self.state_changed.emit("Отправлен запрос штатного выключения гостевой ОС")

    def terminate_process(self) -> None:
        if self.running and self._process is not None:
            self.state_changed.emit("Остановка процесса QEMU; возможно повреждение данных гостя")
            self._process.terminate()

    def _read_output(self) -> None:
        if self._process is not None:
            data = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
            if data:
                self.output_received.emit(data[-16_000:])

    def _on_error(self, error: QProcess.ProcessError) -> None:
        if error != QProcess.ProcessError.Crashed:
            self.error_occurred.emit(f"Ошибка процесса QEMU: {error.name}")

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        status = "нормально" if exit_status == QProcess.ExitStatus.NormalExit else "аварийно"
        self.state_changed.emit(f"QEMU завершён ({status}, код {exit_code})")
        self.finished.emit(exit_code, status)
        self._process = None
        self._qmp_port = None

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    @staticmethod
    def _read_qmp_object(reader) -> dict:
        while True:
            line = reader.readline()
            if not line:
                raise ConnectionError("Соединение QMP закрыто до ответа.")
            try:
                parsed = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                return parsed

    @classmethod
    def _qmp_command(cls, connection: socket.socket, reader, command: dict) -> dict:
        payload = json.dumps(command, separators=(",", ":")).encode("utf-8") + b"\r\n"
        connection.sendall(payload)
        while True:
            result = cls._read_qmp_object(reader)
            if "return" in result or "error" in result:
                return result
