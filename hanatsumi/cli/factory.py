"""Typer factory 与共享的 ``--debug`` 选项，参照 Hanaikada 的 ``cli/factory.py``。

命令注册只发生在 ``cli/app.py``；这里提供统一的 app/group 构造方式，
以及“每个层级都能用”的 ``--debug``。

私有的 ``typer._click`` 导入只出现在这个模块：typer 没有被固定版本，
新版（0.27 起）已不再依赖独立的 click 包，出问题会先在这里暴露，
由测试（``--help`` 快照、``--debug``）兜底。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import typer
from typer import _click
from typer.core import TyperCommand, TyperGroup, TyperOption

from hanatsumi.logger import LOGGER_NAME

DEBUG_OPTION_HELP = "打印调试日志"

# 给 app.py 复用，避免它自己 import 私有模块。
ClickException = _click.ClickException

__all__ = ["ClickException", "DebugOptionTyper", "typer_factory"]


def _debug_option_callback(ctx: _click.Context, _param: _click.Parameter, value: bool) -> None:
    """打开调试日志。"""
    if value and not ctx.resilient_parsing:
        app_logger = logging.getLogger(LOGGER_NAME)
        app_logger.setLevel(logging.DEBUG)
        app_logger.debug("调试日志已开启")


def _make_debug_option() -> TyperOption:
    """一个可以挂在任何层级的 ``--debug``。"""
    return TyperOption(
        param_decls=["--debug"],
        is_flag=True,
        is_eager=True,
        expose_value=False,
        help=DEBUG_OPTION_HELP,
        callback=_debug_option_callback,
    )


def _has_debug_option(params: list[_click.Parameter] | None) -> bool:
    return any(isinstance(param, TyperOption) and "--debug" in (param.opts or []) for param in params or [])


def _with_debug_option(params: list[_click.Parameter] | None) -> list[_click.Parameter]:
    normalized = list(params or [])
    if _has_debug_option(normalized):
        return normalized
    return [_make_debug_option(), *normalized]


class DebugTyperCommand(TyperCommand):
    """总是带 ``--debug`` 的 Typer 命令。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["params"] = _with_debug_option(kwargs.get("params"))
        super().__init__(*args, **kwargs)


class AlphabeticalMixedGroup(TyperGroup):
    """带 ``--debug``，且按字母序列出子命令与子分组的 group。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["params"] = _with_debug_option(kwargs.get("params"))
        super().__init__(*args, **kwargs)

    def list_commands(self, ctx: _click.Context) -> list[str]:  # type: ignore[override]
        return sorted(self.commands.keys())


class DebugOptionTyper(typer.Typer):
    """默认让每个命令都使用 ``DebugTyperCommand`` 的 app。"""

    def command(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        kwargs.setdefault("cls", DebugTyperCommand)
        return super().command(*args, **kwargs)


def typer_factory(help: str) -> typer.Typer:
    """构造带共享设置的 app 或子分组。"""
    return DebugOptionTyper(
        help=help,
        add_completion=True,
        no_args_is_help=True,
        cls=AlphabeticalMixedGroup,
        rich_markup_mode=None,
        rich_help_panel=None,
        pretty_exceptions_enable=False,
    )
