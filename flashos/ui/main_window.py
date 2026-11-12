from __future__ import annotations

from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from flashos.models import CompatibilityResult, DiskInfo, DiscoveredFile, PersistenceReport, format_bytes
from flashos.services.config_store import AppSettings, ConfigStore
from flashos.services.file_scanner import FileScanner, ScanCancelled
from flashos.services.logging_service import redact_sensitive_paths
from flashos.services.persistence_manager import PersistenceManager
from flashos.services.qemu_controller import QemuController
from flashos.services.usb_scanner import UsbScanner


class ScanWorker(QObject):
    finished = Signal(object, list, list, str)
    progress = Signal(int)

    def __init__(self, root: Path, cancel: Event) -> None:
        super().__init__()
        self.root = root
        self.cancel = cancel

    @Slot()
    def run(self) -> None:
        warnings: list[str] = []
        try:
            files = FileScanner().scan(self.root, self.cancel, warnings.append, self.progress.emit)
            self.finished.emit(files, warnings, [], "")
        except ScanCancelled as exc:
            self.finished.emit(None, warnings, [], str(exc))
        except Exception as exc:  # keep worker failures visible in the app
            self.finished.emit(None, warnings, [], str(exc))


class ProbeSignals(QObject):
    finished = Signal(str, object, str)


class ProbeTask(QRunnable):
    def __init__(self, executable: str) -> None:
        super().__init__()
        self.executable = executable
        self.signals = ProbeSignals()

    @Slot()
    def run(self) -> None:
        try:
            version = QemuController.probe(self.executable)
            self.signals.finished.emit(self.executable, version, "")
        except Exception as exc:
            self.signals.finished.emit(self.executable, None, str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("FlashOS")
        self.resize(1320, 880)
        self.setMinimumSize(1000, 680)
        self.usb_scanner = UsbScanner()
        self.persistence_manager = PersistenceManager()
        self.config_store = ConfigStore()
        self.settings, warning = self.config_store.load()
        self.devices: list[DiskInfo] = []
        self.files: list[DiscoveredFile] = []
        self.selected_device: DiskInfo | None = None
        self.selected_iso: Path | None = None
        self._scan_thread: QThread | None = None
        self._scan_worker: ScanWorker | None = None
        self._scan_cancel: Event | None = None
        self._probe_task: ProbeTask | None = None
        self._qemu_version = ""
        self.vm = QemuController(self)
        self.vm.state_changed.connect(self._on_vm_state)
        self.vm.output_received.connect(self._on_vm_output)
        self.vm.finished.connect(self._on_vm_finished)
        self.vm.error_occurred.connect(lambda msg: self._log("Ошибка QEMU", msg))
        self._build_ui()
        self._apply_settings()
        if warning:
            self._log("Настройки", warning)
        self._refresh_devices()

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(14)

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title = QLabel("FlashOS")
        title.setObjectName("title")
        subtitle = QLabel("USB и библиотека Linux-образов")
        subtitle.setObjectName("muted")
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        header.addLayout(title_col)
        header.addStretch(1)
        self.refresh_button = QPushButton("⟳  Обновить накопители")
        self.refresh_button.clicked.connect(self._refresh_devices)
        header.addWidget(self.refresh_button)
        root.addLayout(header)

        banner = QLabel(
            "Обычный запуск ISO не обещает сохранение данных. Совместимость Persistence показывается отдельно и требует повторного запуска для подтверждения."
        )
        banner.setObjectName("banner")
        banner.setWordWrap(True)
        root.addWidget(banner)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_overview(), "Обзор")
        self.tabs.addTab(self._build_qemu(), "Виртуальная машина")
        root.addWidget(self.tabs, 1)

        footer = QHBoxLayout()
        self.status_label = QLabel("Подключите USB-накопитель и нажмите «Обновить накопители».")
        self.status_label.setObjectName("muted")
        footer.addWidget(self.status_label, 1)
        self.scan_button = QPushButton("Сканировать выбранный том")
        self.scan_button.clicked.connect(self._scan_selected)
        footer.addWidget(self.scan_button)
        self.cancel_scan_button = QPushButton("Отменить")
        self.cancel_scan_button.setEnabled(False)
        self.cancel_scan_button.clicked.connect(self._cancel_scan)
        footer.addWidget(self.cancel_scan_button)
        root.addLayout(footer)
        self.setCentralWidget(central)
        self.setStyleSheet(DARK_STYLE)

    def _build_overview(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 12, 10, 10)
        top = QSplitter(Qt.Orientation.Horizontal)

        device_panel = self._panel("Накопители")
        device_layout = self._panel_layout(device_panel)
        self.device_list = QListWidget()
        self.device_list.currentItemChanged.connect(self._on_device_selected)
        device_layout.addWidget(self.device_list)
        self.device_summary = QLabel("Накопитель не выбран")
        self.device_summary.setWordWrap(True)
        self.device_summary.setObjectName("muted")
        device_layout.addWidget(self.device_summary)
        top.addWidget(device_panel)

        files_panel = self._panel("Найденные файлы")
        files_layout = self._panel_layout(files_panel)
        self.file_table = QTableWidget(0, 4)
        self.file_table.setHorizontalHeaderLabels(["Тип", "Файл", "Путь", "Размер"])
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.file_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.file_table.horizontalHeader().setStretchLastSection(False)
        self.file_table.horizontalHeader().setSectionResizeMode(0, self.file_table.horizontalHeader().ResizeMode.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(1, self.file_table.horizontalHeader().ResizeMode.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(2, self.file_table.horizontalHeader().ResizeMode.Stretch)
        self.file_table.horizontalHeader().setSectionResizeMode(3, self.file_table.horizontalHeader().ResizeMode.ResizeToContents)
        self.file_table.itemSelectionChanged.connect(self._on_file_selected)
        files_layout.addWidget(self.file_table)
        self.selected_iso_label = QLabel("Сначала выберите ISO в списке.")
        self.selected_iso_label.setObjectName("muted")
        self.selected_iso_label.setWordWrap(True)
        files_layout.addWidget(self.selected_iso_label)
        top.addWidget(files_panel)
        top.setStretchFactor(0, 1)
        top.setStretchFactor(1, 3)
        layout.addWidget(top, 3)

        bottom = QSplitter(Qt.Orientation.Horizontal)
        persistence_panel = self._panel("Диагностика Persistence")
        persistence_layout = self._panel_layout(persistence_panel)
        self.persistence_label = QLabel("Выберите том для анализа.")
        self.persistence_label.setWordWrap(True)
        self.persistence_label.setObjectName("banner")
        persistence_layout.addWidget(self.persistence_label)
        self.persistence_table = QTableWidget(0, 3)
        self.persistence_table.setHorizontalHeaderLabels(["Источник", "Статус", "Подробности"])
        self.persistence_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.persistence_table.horizontalHeader().setSectionResizeMode(0, self.persistence_table.horizontalHeader().ResizeMode.ResizeToContents)
        self.persistence_table.horizontalHeader().setSectionResizeMode(1, self.persistence_table.horizontalHeader().ResizeMode.ResizeToContents)
        self.persistence_table.horizontalHeader().setSectionResizeMode(2, self.persistence_table.horizontalHeader().ResizeMode.Stretch)
        persistence_layout.addWidget(self.persistence_table, 1)
        bottom.addWidget(persistence_panel)

        script_panel = self._panel("BAT предпросмотр")
        script_layout = self._panel_layout(script_panel)
        self.script_preview = QPlainTextEdit()
        self.script_preview.setReadOnly(True)
        self.script_preview.setPlaceholderText("Найденный persistence.bat будет показан только как текст.")
        self.script_preview.setFont(QFont("Consolas", 10))
        script_layout.addWidget(self.script_preview)
        bottom.addWidget(script_panel)
        bottom.setStretchFactor(0, 3)
        bottom.setStretchFactor(1, 2)
        layout.addWidget(bottom, 2)
        return page

    def _build_qemu(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 12, 10, 10)
        card = self._panel("Запуск Linux через QEMU")
        card_layout = self._panel_layout(card)
        form = QFormLayout()
        card_layout.addLayout(form)
        qemu_row = QHBoxLayout()
        self.qemu_path = QLineEdit()
        self.qemu_path.setPlaceholderText("Путь к qemu-system-x86_64.exe")
        self.qemu_path.editingFinished.connect(self._save_settings)
        qemu_row.addWidget(self.qemu_path, 1)
        browse = QPushButton("Выбрать…")
        browse.clicked.connect(self._browse_qemu)
        qemu_row.addWidget(browse)
        self.check_qemu_button = QPushButton("Проверить")
        self.check_qemu_button.clicked.connect(self._probe_qemu)
        qemu_row.addWidget(self.check_qemu_button)
        form.addRow("QEMU", qemu_row)

        self.qemu_version_label = QLabel("QEMU не проверен")
        self.qemu_version_label.setObjectName("muted")
        form.addRow("Версия", self.qemu_version_label)

        settings_row = QHBoxLayout()
        self.memory_spin = QSpinBox()
        self.memory_spin.setRange(512, 131072)
        self.memory_spin.setSingleStep(512)
        self.memory_spin.setSuffix(" МБ")
        self.memory_spin.valueChanged.connect(self._save_settings)
        settings_row.addWidget(QLabel("Память"))
        settings_row.addWidget(self.memory_spin)
        self.cpu_spin = QSpinBox()
        self.cpu_spin.setRange(1, 64)
        self.cpu_spin.valueChanged.connect(self._save_settings)
        settings_row.addWidget(QLabel("CPU"))
        settings_row.addWidget(self.cpu_spin)
        settings_row.addStretch(1)
        form.addRow("Ресурсы", settings_row)

        mode = QComboBox()
        mode.addItem("Live ISO — Persistence не включено", userData="live")
        mode.addItem("Live Persistence — не подтверждено для запуска QEMU", userData="unverified")
        mode.setItemData(1, 0, Qt.ItemDataRole.UserRole - 1)
        mode.setToolTip("Обычный запуск доступен. Persistence не включается, пока комбинация не пройдёт контрольный тест.")
        form.addRow("Режим", mode)

        actions = QHBoxLayout()
        self.start_vm_button = QPushButton("Запустить выбранный ISO")
        self.start_vm_button.clicked.connect(self._start_vm)
        actions.addWidget(self.start_vm_button)
        self.shutdown_vm_button = QPushButton("Запросить штатное выключение")
        self.shutdown_vm_button.clicked.connect(self._request_shutdown)
        self.shutdown_vm_button.setEnabled(False)
        actions.addWidget(self.shutdown_vm_button)
        self.stop_vm_button = QPushButton("Остановить принудительно…")
        self.stop_vm_button.clicked.connect(self._force_stop)
        self.stop_vm_button.setEnabled(False)
        actions.addWidget(self.stop_vm_button)
        form.addRow("Управление", actions)

        self.vm_status = QLabel("Виртуальная машина не запущена.")
        self.vm_status.setWordWrap(True)
        self.vm_status.setObjectName("banner")
        layout.addWidget(card)
        layout.addWidget(self.vm_status)
        log_panel = self._panel("Журнал")
        log_layout = self._panel_layout(log_panel)
        log_actions = QHBoxLayout()
        clear_log = QPushButton("Очистить")
        clear_log.clicked.connect(lambda: self.log_view.clear())
        export_log = QPushButton("Экспортировать…")
        export_log.clicked.connect(self._export_log)
        log_actions.addStretch(1)
        log_actions.addWidget(export_log)
        log_actions.addWidget(clear_log)
        log_layout.addLayout(log_actions)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(500)
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_panel, 1)
        return page

    @staticmethod
    def _panel(title: str) -> QFrame:
        panel = QFrame()
        panel.setObjectName("panel")
        panel.setProperty("panelTitleText", title)
        return panel

    @staticmethod
    def _panel_layout(panel: QFrame) -> QVBoxLayout:
        layout = QVBoxLayout(panel)
        label = QLabel(str(panel.property("panelTitleText") or ""))
        label.setObjectName("panelTitle")
        layout.addWidget(label)
        return layout

    def _apply_settings(self) -> None:
        self.qemu_path.setText(self.settings.qemu_path)
        self.memory_spin.setValue(self.settings.memory_mb)
        self.cpu_spin.setValue(self.settings.cpu_count)

    def _refresh_devices(self) -> None:
        previous = str(self.selected_device.root) if self.selected_device else self.settings.last_drive
        self.device_list.clear()
        self.devices = self.usb_scanner.scan()
        for disk in self.devices:
            item = QListWidgetItem(disk.display_name)
            item.setData(Qt.ItemDataRole.UserRole, disk)
            item.setToolTip(
                f"{disk.filesystem or 'Файловая система неизвестна'} · "
                f"{format_bytes(disk.free_bytes)} свободно из {format_bytes(disk.total_bytes)}"
            )
            self.device_list.addItem(item)
        if not self.devices:
            self.selected_device = None
            self._clear_scan_results()
            self.status_label.setText("Доступные USB-тома не найдены.")
            self._log("Информация", "Съёмные и USB-фиксированные тома не обнаружены.")
            return
        selected = next((i for i, disk in enumerate(self.devices) if str(disk.root).casefold() == previous.casefold()), 0)
        self.device_list.setCurrentRow(selected)
        self.status_label.setText(f"Найдено томов: {len(self.devices)}.")

    def _on_device_selected(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            self.selected_device = None
            self.scan_button.setEnabled(False)
            return
        disk = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(disk, DiskInfo):
            return
        changed = self.selected_device is None or self.selected_device.root != disk.root
        self.selected_device = disk
        self.device_summary.setText(
            f"{disk.filesystem or 'Файловая система неизвестна'} · {format_bytes(disk.free_bytes)} свободно из {format_bytes(disk.total_bytes)}"
        )
        self.scan_button.setEnabled(True)
        self._save_settings()
        if changed:
            self._scan_selected()

    def _scan_selected(self) -> None:
        if self.selected_device is None or self._scan_thread is not None:
            return
        root = self.selected_device.root
        self._scan_cancel = Event()
        self._scan_thread = QThread(self)
        self._scan_worker = ScanWorker(root, self._scan_cancel)
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.finished.connect(self._scan_thread.quit)
        self._scan_worker.finished.connect(self._scan_worker.deleteLater)
        self._scan_thread.finished.connect(self._on_scan_thread_finished)
        self._scan_thread.finished.connect(self._scan_thread.deleteLater)
        self.scan_button.setEnabled(False)
        self.cancel_scan_button.setEnabled(True)
        self.status_label.setText(f"Сканирование {root}…")
        self._log("Сканирование", f"Начато чтение выбранного тома {root}.")
        self._scan_thread.start()

    def _cancel_scan(self) -> None:
        if self._scan_cancel is not None:
            self._scan_cancel.set()
            self.status_label.setText("Отмена сканирования…")

    def _on_scan_progress(self, directories: int) -> None:
        self.status_label.setText(f"Сканирование… каталогов просмотрено: {directories}")

    def _on_scan_finished(self, files: object, warnings: list, _unused: list, error: str) -> None:
        if error:
            self.status_label.setText(error)
            self._log("Сканирование", error)
        elif not self.selected_device or not self.selected_device.root.exists():
            self._clear_scan_results()
            self.status_label.setText("USB-том отключён во время сканирования.")
            self._log("Предупреждение", "Том отключён; результаты отброшены.")
        else:
            self.files = list(files or [])
            self._populate_file_table()
            reports, preview = self.persistence_manager.analyze(self.selected_device.root, self.files)
            self._populate_persistence(reports)
            self.script_preview.setPlainText(preview)
            self.status_label.setText(f"Найдено файлов: {len(self.files)}. BAT не запускались.")
            self._log("Сканирование", f"Завершено; файлов найдено: {len(self.files)}.")
        for warning in warnings[:20]:
            self._log("Предупреждение", warning)
        if len(warnings) > 20:
            self._log("Предупреждение", f"Дополнительных предупреждений: {len(warnings) - 20}.")
        self.scan_button.setEnabled(self.selected_device is not None)
        self.cancel_scan_button.setEnabled(False)

    def _on_scan_thread_finished(self) -> None:
        self._scan_thread = None
        self._scan_worker = None
        self._scan_cancel = None

    def _populate_file_table(self) -> None:
        self.file_table.setRowCount(0)
        for found in self.files:
            row = self.file_table.rowCount()
            self.file_table.insertRow(row)
            values = [found.kind, found.name, str(found.path), found.size_text]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 2:
                    item.setData(Qt.ItemDataRole.UserRole, found.path)
                self.file_table.setItem(row, column, item)
        self.selected_iso = None
        self.selected_iso_label.setText("Сначала выберите ISO в списке.")
        self.start_vm_button.setEnabled(bool(self._qemu_version))

    def _populate_persistence(self, reports: list[PersistenceReport]) -> None:
        self.persistence_table.setRowCount(0)
        for report in reports:
            row = self.persistence_table.rowCount()
            self.persistence_table.insertRow(row)
            for column, value in enumerate((report.source, report.status, report.details)):
                self.persistence_table.setItem(row, column, QTableWidgetItem(value))
        self.persistence_label.setText(
            "Найденные конфигурации и метки — только диагностические признаки. Live Persistence для QEMU не подтверждён."
        )

    def _on_file_selected(self) -> None:
        row = self.file_table.currentRow()
        if row < 0 or row >= len(self.files):
            return
        item = self.files[row]
        if item.kind != "ISO-образ":
            self.selected_iso = None
            self.selected_iso_label.setText("Выбранный файл не является ISO-образом.")
            self.start_vm_button.setEnabled(False)
            return
        self.selected_iso = item.path
        self.selected_iso_label.setText(f"Выбран ISO: {item.path} · {item.size_text}")
        result = self.persistence_manager.compatibility_for(self.selected_device.root, item.path, self.files) if self.selected_device else CompatibilityResult("Не проверено", "Выберите USB-том.")
        self.persistence_label.setText(f"{result.status}. {result.details}")
        self.start_vm_button.setEnabled(bool(self._qemu_version))
        self._save_settings()

    def _browse_qemu(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Выберите QEMU", "", "QEMU (qemu-system-x86_64.exe)")
        if path:
            self.qemu_path.setText(path)
            self._save_settings()

    def _probe_qemu(self) -> None:
        executable = self.qemu_path.text().strip()
        if not executable:
            QMessageBox.information(self, "QEMU", "Укажите путь к qemu-system-x86_64.exe.")
            return
        self.check_qemu_button.setEnabled(False)
        self.qemu_version_label.setText("Проверка QEMU…")
        task = ProbeTask(executable)
        self._probe_task = task
        task.signals.finished.connect(self._on_qemu_probe_finished)
        QThreadPool.globalInstance().start(task)

    def _on_qemu_probe_finished(self, executable: str, version: object, error: str) -> None:
        self.check_qemu_button.setEnabled(True)
        self._probe_task = None
        if error:
            self._qemu_version = ""
            self.qemu_version_label.setText(f"Проверка не пройдена: {error}")
            self._log("Ошибка QEMU", error)
        else:
            self._qemu_version = str(version)
            self.qemu_path.setText(executable)
            self.qemu_version_label.setText(self._qemu_version)
            self._log("QEMU", f"Найдена версия {self._qemu_version}.")
            self._save_settings()
        self.start_vm_button.setEnabled(bool(self._qemu_version and self.selected_iso))

    def _start_vm(self) -> None:
        if self.selected_iso is None:
            QMessageBox.information(self, "ISO не выбран", "Выберите ISO-образ на вкладке «Обзор».")
            return
        try:
            self.vm.start_iso(
                self.qemu_path.text().strip(), str(self.selected_iso),
                self.memory_spin.value(), self.cpu_spin.value(),
            )
            self._set_vm_controls(True)
            self.tabs.setCurrentIndex(1)
            self._log("QEMU", f"Запуск обычного Live ISO без Persistence: {self.selected_iso.name}.")
        except Exception as exc:
            QMessageBox.critical(self, "Не удалось запустить QEMU", str(exc))
            self._log("Ошибка QEMU", str(exc))

    def _request_shutdown(self) -> None:
        try:
            self.vm.request_guest_shutdown()
            self._log("QEMU", "Отправлен запрос штатного выключения гостевой ОС.")
        except Exception as exc:
            QMessageBox.warning(self, "Завершение QEMU", str(exc))
            self._log("Предупреждение QEMU", str(exc))

    def _force_stop(self) -> None:
        answer = QMessageBox.warning(
            self, "Принудительная остановка",
            "Принудительная остановка может повредить данные гостевой системы. Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.vm.terminate_process()

    def _on_vm_state(self, state: str) -> None:
        self.vm_status.setText(state)
        running = self.vm.running
        self._set_vm_controls(running)
        self._log("QEMU", state)

    def _on_vm_output(self, text: str) -> None:
        if text.strip():
            self._log("QEMU output", text.strip())

    def _on_vm_finished(self, exit_code: int, status: str) -> None:
        self._set_vm_controls(False)
        self.vm_status.setText(f"QEMU завершён: {status}, код {exit_code}.")

    def _set_vm_controls(self, running: bool) -> None:
        self.start_vm_button.setEnabled(not running and bool(self._qemu_version and self.selected_iso))
        self.shutdown_vm_button.setEnabled(running)
        self.stop_vm_button.setEnabled(running)

    def _save_settings(self, *_args) -> None:
        settings = AppSettings(
            qemu_path=self.qemu_path.text().strip(),
            memory_mb=self.memory_spin.value(),
            cpu_count=self.cpu_spin.value(),
            last_iso=str(self.selected_iso or ""),
            last_drive=str(self.selected_device.root) if self.selected_device else "",
        )
        try:
            self.config_store.save(settings)
            self.settings = settings
        except OSError as exc:
            self._log("Настройки", f"Не удалось сохранить параметры: {exc}")

    def _export_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт журнала", "flashos-log.txt", "Текстовые файлы (*.txt)")
        if path:
            try:
                Path(path).write_text(self.log_view.toPlainText(), encoding="utf-8")
                self._log("Журнал", "Диагностический журнал экспортирован.")
            except OSError as exc:
                QMessageBox.warning(self, "Экспорт журнала", str(exc))

    def _log(self, category: str, message: str) -> None:
        from datetime import datetime

        safe = redact_sensitive_paths(message)
        for line in safe.splitlines() or [""]:
            self.log_view.appendPlainText(f"{datetime.now():%H:%M:%S}  {category}  {line}")

    def _clear_scan_results(self) -> None:
        self.files = []
        self.selected_iso = None
        self.file_table.setRowCount(0)
        self.persistence_table.setRowCount(0)
        self.script_preview.clear()
        self.selected_iso_label.setText("Сначала выберите ISO в списке.")
        self.persistence_label.setText("Выберите том для анализа.")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.vm.running:
            QMessageBox.warning(
                self, "QEMU запущен",
                "Сначала завершите гостевую систему штатно или используйте отдельную команду остановки.",
            )
            event.ignore()
            return
        if self._scan_cancel is not None:
            self._scan_cancel.set()
        if self._scan_thread is not None:
            self._scan_thread.quit()
            self._scan_thread.wait(3000)
        self._save_settings()
        event.accept()


DARK_STYLE = """
QWidget { background: #101319; color: #edf1f7; font-family: 'Segoe UI'; font-size: 10pt; }
QLabel#title { font-size: 25pt; font-weight: 650; }
QLabel#muted { color: #9aa6b7; }
QLabel#panelTitle { font-size: 13pt; font-weight: 600; padding-bottom: 5px; }
QLabel#banner { background: #17202b; color: #c8d7f5; border: 1px solid #33445b; border-radius: 7px; padding: 11px; }
QFrame#panel { background: #191e27; border: 1px solid #2b3340; border-radius: 8px; padding: 8px; }
QPushButton { background: #34466f; color: white; border: 1px solid #526ba5; border-radius: 5px; padding: 8px 13px; }
QPushButton:hover { background: #405989; }
QPushButton:disabled { background: #222832; color: #728096; border-color: #303947; }
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QListWidget, QTableWidget { background: #151a22; border: 1px solid #303947; border-radius: 4px; padding: 5px; selection-background-color: #34466f; }
QHeaderView::section { background: #222a36; color: #d7dfeb; border: none; padding: 7px; font-weight: 600; }
QTableWidget { gridline-color: #2b3340; alternate-background-color: #1d232d; }
QTabWidget::pane { border: 1px solid #2b3340; border-radius: 6px; background: #191e27; }
QTabBar::tab { background: #151a22; color: #9aa6b7; padding: 10px 18px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: white; background: #202b3b; border-bottom-color: #7c9cff; }
QSplitter::handle { background: #2b3340; }
"""
