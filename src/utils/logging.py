from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from loguru import logger


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    task_id: Optional[str] = None,
) -> None:
    logger.remove()
    fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{extra[task_id]}</cyan> | "
        "<level>{message}</level>"
    )
    logger.configure(extra={"task_id": task_id or "system"})
    logger.add(sys.stderr, level=level, format=fmt, colorize=True)
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_file,
            level=level,
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {extra[task_id]} | {message}",
            rotation="20 MB",
            retention="7 days",
            enqueue=True,
        )


def get_logger(task_id: str = "system"):
    return logger.bind(task_id=task_id)
