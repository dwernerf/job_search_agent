from __future__ import annotations

import logging
from pathlib import Path

from .config import JobAgentConfig


def setup_logging(config: JobAgentConfig, log_path: Path) -> logging.Logger:
    logger = logging.getLogger("jobagent")
    logger.handlers.clear()
    logger.setLevel(getattr(logging, config.logging.level.upper(), logging.INFO))
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    if config.logging.console:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)

    if config.logging.file:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger
