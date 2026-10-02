"""status / verify / reset：查看、校验与清理本地数据。"""

from typing import Annotated

import typer

from hanatsumi import service
from hanatsumi.cli.options import DEFAULT_OUT, OutOption, StateOption
from hanatsumi.cli.output import err_console, report_status, report_verify

__all__ = ["reset", "status", "verify"]


def status(
    out: OutOption = DEFAULT_OUT,
    state: StateOption = None,
) -> None:
    """查看断点状态与 CSV 概况。"""
    report_status(service.status(service.Paths.build(out, state)))


def verify(
    out: OutOption = DEFAULT_OUT,
    state: StateOption = None,
    dups: Annotated[bool, typer.Option(help="检查重复 id（全量数据下较耗内存）")] = False,
    init_state: Annotated[bool, typer.Option(help="依据 CSV 重建状态文件（状态丢失时的恢复手段）")] = False,
) -> None:
    """校验 CSV 与状态文件是否一致；不一致时退出码为 1。"""
    report = service.verify(service.Paths.build(out, state), dups=dups, init_state=init_state)
    report_verify(report)
    if not report.ok:
        raise typer.Exit(1)


def reset(
    out: OutOption = DEFAULT_OUT,
    state: StateOption = None,
    yes: Annotated[bool, typer.Option(help="确认删除")] = False,
) -> None:
    """删除 CSV、状态文件与 refresh 暂存文件。"""
    paths = service.Paths.build(out, state)
    if not yes:
        err_console.print("将删除以下文件（加 --yes 确认执行）：")
        for target in service.reset_targets(paths):
            err_console.print(f"  - {target}{'（存在）' if target.exists() else ''}")
        raise typer.Exit(1)

    deleted = service.reset(paths)
    if not deleted:
        err_console.print("没有可删除的文件")
    err_console.print("已重置，可重新运行 `hanatsumi fetch`")
