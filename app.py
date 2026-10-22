"""Application entry point for the PySide6 cloud-phone manager."""

from ui.main_window import MainWindow, create_application


def main() -> int:
    application = create_application()
    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
