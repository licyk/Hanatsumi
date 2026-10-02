"""fetch / update / refresh 共用的命令行选项别名。"""

from __future__ import annotations

from typing import Annotated

import typer

from hanatsumi.config import (
    DEFAULT_DELAY,
    DEFAULT_MAX_RETRIES,
    DEFAULT_OUT,
    DEFAULT_PAGE_SIZE,
    DEFAULT_TIMEOUT,
    MAX_PAGE_SIZE,
)

OutOption = Annotated[str, typer.Option(help=f"CSV 输出路径（默认 {DEFAULT_OUT}）")]
StateOption = Annotated[str | None, typer.Option(help="状态文件路径（默认由 --out 推导为 tags.state.json）")]
DelayOption = Annotated[float, typer.Option(min=0.0, help="请求最小间隔秒数，匿名建议 >= 1.0")]
PageOption = Annotated[int, typer.Option(min=1, max=MAX_PAGE_SIZE, help=f"单页条数（API 上限 {DEFAULT_PAGE_SIZE}）")]
TimeoutOption = Annotated[float, typer.Option(min=1.0, help="单次请求超时秒数")]
RetriesOption = Annotated[int, typer.Option(min=0, help="失败重试次数")]
MaxPagesOption = Annotated[int, typer.Option(min=0, help="本次最多抓取页数，0=不限（调试用）")]
QuietFlag = Annotated[bool, typer.Option("-q", "--quiet", help="不打印逐页进度")]

__all__ = [
    "DEFAULT_DELAY",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_OUT",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_TIMEOUT",
    "DelayOption",
    "MaxPagesOption",
    "OutOption",
    "PageOption",
    "QuietFlag",
    "RetriesOption",
    "StateOption",
    "TimeoutOption",
]
