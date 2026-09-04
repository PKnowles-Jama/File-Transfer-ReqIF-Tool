from __future__ import annotations

import tkinter as tk

from .gui import Application
from .logging_setup import configure_logging


def main() -> None:
    logger, log_path = configure_logging()
    root = tk.Tk()
    Application(root, logger, log_path)
    root.mainloop()


if __name__ == "__main__":
    main()

