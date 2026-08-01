try:
    import pyi_splash
except ImportError:
    pyi_splash = None

from teslacraft_parser.gui import run_gui  # noqa: E402

if __name__ == "__main__":
    run_gui()
