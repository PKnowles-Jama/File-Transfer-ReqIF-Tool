from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path


def configure_logging(directory: Path | None = None) -> tuple[logging.Logger, Path]:
    log_dir = directory or (Path.cwd() / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"ftr-tool-{datetime.now():%Y%m%d-%H%M%S}.log"
    logger = logging.getLogger("ftr_tool")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger, path

