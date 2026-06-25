from __future__ import annotations

import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from grab_app.config import app_icon_path
from grab_app.ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(app_icon_path())))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
