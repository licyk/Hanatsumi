"""应用日志：单一 ``hanatsumi`` logger，经 Rich 写到 stderr，带彩色等级。"""

from __future__ import annotations

import logging
import os

from rich.console import Console
from rich.logging import RichHandler

LOGGER_NAME = "hanatsumi"

# --debug 之前就已存在的 handler 不重复添加，setup_logging 可以被反复调用。
_configured = False


def setup_logging(level: int | str | None = None) -> logging.Logger:
    """配置 ``hanatsumi`` logger 并返回它。默认级别读 ``HANATSUMI_LOG_LEVEL``。"""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if level is None:
        level = os.environ.get("HANATSUMI_LOG_LEVEL", "INFO").upper()
    logger.setLevel(level)
    if not _configured and not any(getattr(h, "name", "") == LOGGER_NAME for h in logger.handlers):
        handler = RichHandler(
            console=Console(stderr=True),
            show_time=False,
            show_path=False,
            markup=False,
            rich_tracebacks=True,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.name = LOGGER_NAME
        logger.addHandler(handler)
        logger.propagate = False
        _configured = True
    return logger
