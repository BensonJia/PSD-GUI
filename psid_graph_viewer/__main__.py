from __future__ import annotations

import sys

from Qt import QtCore, QtGui, QtWidgets

from .window import (
    APPLICATION_DISPLAY_NAME,
    APPLICATION_ICON,
    APPLICATION_NAME,
    MainWindow,
)


def configure_application(app: QtWidgets.QApplication) -> None:
    app.setApplicationName(APPLICATION_DISPLAY_NAME)
    app.setApplicationDisplayName(APPLICATION_DISPLAY_NAME)
    app.setOrganizationName("PowerSimulationsDynamics")
    app.setWindowIcon(QtGui.QIcon(str(APPLICATION_ICON)))
    if sys.platform == "darwin":
        app.setStyle("macOS")


def configure_platform_identity() -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "PowerSimulationsDynamics.PSD-GUI"
        )


def main() -> int:
    # macOS creates the application menu while QApplication is constructed, so
    # the short display name must be available before that point.
    QtCore.QCoreApplication.setApplicationName(APPLICATION_DISPLAY_NAME)
    QtCore.QCoreApplication.setOrganizationName("PowerSimulationsDynamics")
    configure_platform_identity()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    configure_application(app)
    window = MainWindow()
    window.show()
    QtCore.QTimer.singleShot(0, window.julia.start_backend)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
