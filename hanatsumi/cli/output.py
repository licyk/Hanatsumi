"""命令输出：Rich 控制台、表格、扫描进度行、运行摘要。

分工（与 Hanaikada 一致）：

- **诊断信息** —— 摘要、告警、错误、重试，走 ``hanatsumi.logger``（RichHandler，
  写 stderr、带彩色等级）；
- **命令结果** —— ``status`` / ``verify`` 的表格写 stdout（``console``），可被管道消费；
- **扫描进度** —— 那条原地刷新的实时行写 stderr（``err_console``），命令结束后换行。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from rich.console import Console
from rich.table import Table

from hanatsumi.config import FIELDS
from hanatsumi.scan import Progress, ProgressFn
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


def _estimate_eta(p: Progress) -> str:
    """剩余时间估算，两种口径，优先行口径。

    两者都是同一个式子：``已用时 × (剩余量 / 已完成量)``。

    1. **行口径**（有 ``total_hint`` 时，即 ``refresh``）——用上一次完整快照的行数当
       总数，行数是实际工作量，估算接近精确；
    2. **id 口径**（兜底，全新 ``fetch`` 只有这个）——按**本次运行**已扫过的 id 区间
       线性外推。id 密度并不均匀（实测 4%~96%），所以这是估算值，且方向偏保守
       （顶部密、底部疏，通常会把剩余时间说长一些）。

    Danbooru 不提供总数（无 Count 头、无 counts 端点、偏移分页封顶 1000 页），
    所以全新抓取拿不到精确总数。
    """
    # 1) 行口径
    if p.total_hint and p.stats.written and p.total_hint > p.stats.written and p.elapsed > 0:
        return _hms(p.elapsed * (p.total_hint - p.stats.written) / p.stats.written)

    # 2) id 口径：速率分母必须是“本次运行”覆盖的区间（续传时 start_id 含上次跑的区间）
    floor = p.floor_id if p.floor_id is not None else 1
    if p.run_origin is None or p.cursor is None or p.elapsed <= 0:
        return "?"
    covered = p.run_origin - p.cursor
    remaining = p.cursor - floor
    if covered <= 0 or remaining <= 0:
        return "<1m"
    return _hms(p.elapsed * remaining / covered)


def _id_progress(p: Progress) -> float:
    """整个数据集扫过的 id 区间百分比（精确值，与行数密度无关）。"""
    floor = p.floor_id if p.floor_id is not None else 1
    if not p.start_id or p.cursor is None or p.start_id <= floor:
        return 0.0
    return min(100.0, max(0.0, (p.start_id - p.cursor) / (p.start_id - floor) * 100))


def _progress_line(p: Progress) -> str:
    rate = p.stats.written / p.elapsed if p.elapsed > 0 else 0.0
    cursor_text = f"{p.cursor:,}" if p.cursor else "top"
    return (
        f"[cyan][{_hms(p.elapsed)}][/cyan] id {_id_progress(p):>3.0f}% "
        f"pages={p.stats.pages} rows={p.stats.written:,} "
        f"[green]rate={rate:.0f}/s[/green] cursor=[dim]{cursor_text}[/dim] eta=[yellow]{_estimate_eta(p)}[/yellow]"
    )


def _render_progress(p: Progress) -> None:
    line = _progress_line(p)
    # 按“可见宽度”补空格，否则上一行残留的字符会留在原地（终端列宽不受 ANSI 影响）
    plain = re.sub(r"\[.*?\]", "", line)
    pad = max(0, _PROGRESS_WIDTH - len(plain))
    err_console.print(line + " " * pad, end="\r", soft_wrap=True)


@contextmanager
def scan_progress(enabled: bool = True) -> Iterator[ProgressFn]:
    """给出一个回调，把扫描进度原地刷新到 stderr；结束时换行。"""
    if not enabled:
        yield lambda _p: None
        return

    try:
        yield _render_progress
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
