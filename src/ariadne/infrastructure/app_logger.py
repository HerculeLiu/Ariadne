from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_file_path: str, level: str = "INFO") -> None:
    logger = logging.getLogger("ariadne")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.handlers:
        return

    path = Path(log_file_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)



def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ariadne.{name}")
