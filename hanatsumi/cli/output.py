"""命令输出：Rich 控制台、表格、扫描进度行、运行摘要。

分工（与 Hanaikada 一致）：

- **诊断信息** —— 摘要、告警、错误、重试，走 ``hanatsumi.logger``（RichHandler，
  写 stderr、带彩色等级）；
- **命令结果** —— ``status`` / ``verify`` 的表格写 stdout（``console``），可被管道消费；
- **扫描进度** —— 那条原地刷新的实时行写 stderr（``err_console``），命令结束后换行。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from rich.console import Console
from rich.table import Table

from hanatsumi.config import FIELDS
from hanatsumi.scan import ProgressFn, ScanStats
from hanatsumi.service import Report, RunResult

log = logging.getLogger(__name__)

console = Console()
err_console = Console(stderr=True)

# 进度行用软换行，避免窄终端把 \r 那一行折断
_PROGRESS_WIDTH = 96

__all__ = ["console", "err_console", "print_table", "report_scan", "report_status", "report_verify", "scan_progress"]


def print_table(title: str | None, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    table = Table(title=title, show_lines=False, header_style="bold")
    for column in columns:
        table.add_column(column, overflow="fold")
    for row in rows:
        table.add_row(*["" if v is None else str(v) for v in row])
    console.print(table)


# --------------------------------------------------------------------------- #
# 扫描进度
# --------------------------------------------------------------------------- #
def _hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def _estimate_eta(stats: ScanStats, cursor: int | None, start_id: int | None, elapsed: float) -> str:
    """按已覆盖的 id 区间线性外推剩余页数（估算值）。"""
    if not start_id or not cursor or cursor < 1 or stats.pages < 2:
        return "?"
    covered = start_id - cursor
    if covered <= 0:
        return "?"
    remaining_pages = stats.pages * (cursor - 1) / covered - stats.pages
    if remaining_pages <= 0:
        return "<1m"
    return _hms(elapsed * remaining_pages / stats.pages)


def _progress_line(stats: ScanStats, cursor: int | None, start_id: int | None, elapsed: float) -> str:
    rate = stats.written / elapsed if elapsed > 0 else 0.0
    cursor_text = f"{cursor:,}" if cursor else "top"
    return (
        f"[cyan][{_hms(elapsed)}][/cyan] pages={stats.pages} rows={stats.written:,} "
        f"[green]rate={rate:.0f}/s[/green] cursor=[dim]{cursor_text}[/dim] eta=[yellow]{_estimate_eta(stats, cursor, start_id, elapsed)}[/yellow]"
    )


@contextmanager
def scan_progress(enabled: bool = True) -> Iterator[ProgressFn]:
    """给出一个回调，把扫描进度原地刷新到 stderr；结束时换行。"""
    if not enabled:
        yield lambda _stats, _cursor, _start_id, _elapsed: None
        return

    def render(stats: ScanStats, cursor: int | None, start_id: int | None, elapsed: float) -> None:
        err_console.print(f"{_progress_line(stats, cursor, start_id, elapsed):<{_PROGRESS_WIDTH}}", end="\r", soft_wrap=True)

    try:
        yield render
    finally:
        err_console.print()


# --------------------------------------------------------------------------- #
# 结果渲染
# --------------------------------------------------------------------------- #
def report_scan(result: RunResult) -> None:
    """把 fetch / update / refresh 的结果打成日志摘要。"""
    state = result.state
    if not result.ran:
        log.info("全量抓取已完成：rows=%s range=%s", f"{state.rows:,}", state.scanned_range)
        log.info("同步新增的 tag 请运行：hanatsumi update；刷新已有字段请运行：hanatsumi refresh")
        return

    stats = result.stats
    assert stats is not None
    log.info("完成（%s）：%s", stats.reason, stats.summary(result.elapsed))
    log.info("状态：rows=%s range=%s complete=%s", f"{state.rows:,}", state.scanned_range, state.complete)
    log.info("CSV：%s", result.csv_path)

    if result.mode == "refresh" and result.promoted:
        log.info("刷新完成并已原子替换：%s（源里已删除的 tag 不再保留）", result.paths.out)
        log.info("提示：刷新期间新建的 tag 不在本轮快照里，可再执行 `hanatsumi update` 补齐")


def report_status(report: Report) -> None:
    rows: list[tuple[str, str]] = [("CSV", str(report.paths.out)), ("State", str(report.paths.state))]
    rows.append(("暂存", f"{report.staging}（存在，重跑 refresh 续跑）" if report.staging_exists else "—"))

    if report.state is None:
        rows.append(("状态", "<不存在>（尚未抓取，或状态文件丢失）"))
    else:
        state = report.state
        rows.append(("状态", f"rows={state.rows:,} range={state.scanned_range} complete={state.complete} last_mode={state.last_mode}"))
        rows.append(("最近更新", state.updated_at or "—"))

    if report.csv.get("exists"):
        csv = report.csv
        size = csv["size_bytes"] / 1024 / 1024
        rows.append(("CSV 实况", f"rows={csv['rows']:,} range=[{csv['min_id']}, {csv['max_id']}] header_ok={csv['header_ok']} bad_rows={csv['bad_rows']} size={size:.1f}MiB"))
    else:
        rows.append(("CSV 实况", "<空/不存在>"))

    print_table(None, ["项目", "值"], rows)
    if report.mismatch and report.state is not None:
        log.warning("CSV 行数与状态不一致（%s vs %s），建议运行 `hanatsumi verify`", f"{report.csv['rows']:,}", f"{report.state.rows:,}")


def report_verify(report: Report) -> None:
    csv = report.csv
    header = csv.get("header") or FIELDS
    rows: list[tuple[str, str]] = [
        ("表头", "OK" if csv["header_ok"] else f"不符（期望 {','.join(header)}）"),
        ("数据行", f"{csv['rows']:,}"),
        ("id 范围", f"[{csv['min_id']}, {csv['max_id']}]"),
        ("坏行", str(csv["bad_rows"])),
    ]
    if report.duplicates is not None:
        rows.append(("重复 id", f"{report.duplicates:,}"))
    if report.rebuilt:
        rows.append(("状态文件", f"已重建 {report.paths.state}（complete=False）"))
    elif report.state_match is None:
        rows.append(("状态文件", "不存在"))
    elif report.state_match:
        rows.append(("状态对账", "一致"))
    else:
        state = report.state
        rows.append(("状态对账", f"不一致 state={state.rows:,}行 range={state.scanned_range}" if state else "不一致"))

    print_table(f"校验 {report.paths.out}", ["检查项", "结果"], rows)
    if report.ok:
        log.info("结果：PASS")
    else:
        log.error("结果：FAIL")
