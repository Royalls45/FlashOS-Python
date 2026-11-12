from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from flashos.ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("FlashOS")
    app.setOrganizationName("FlashOS")
    try:
        window = MainWindow()
    except Exception as exc:
        QMessageBox.critical(None, "Не удалось запустить FlashOS", str(exc))
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
