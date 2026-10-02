"""扫描循环：ID 游标降序拉取 → 写 CSV → 每页原子更新断点状态。

两种模式（由 ``floor_id`` 是否为 None 决定）：

``floor_id is None``（全量 / 续传）
    游标从 ``state.min_id``（空状态则从 API 顶部）开始，``page=b{cursor}``
    一路向 id 更小的方向推进，直到返回空页，置 ``state.complete = True``。

``floor_id`` 非 None（增量）
    游标从 API 当前最大 id + 1 开始向下推进，只写 ``id > floor_id`` 的行，
    一旦游标 ≤ floor_id 即追上存量数据，停止并置 ``complete = True``。

每页结束都会 flush CSV 并原子写状态文件，因此 Ctrl-C / 断电都能安全续传。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from hanatsumi.errors import ScanError
from hanatsumi.models import Tag, TagDecodeError
from hanatsumi.storage import CsvStore, State, utc_now

log = logging.getLogger(__name__)

ProgressFn = Callable[["ScanStats", int | None, int | None, float], None]

__all__ = ["PageFetcher", "ProgressFn", "ScanError", "ScanStats", "run_scan"]


class PageFetcher(Protocol):
    """扫描循环只需要这两个方法；测试里可以用假实现替代。"""

    def latest_id(self) -> int | None: ...

    def fetch_page(self, cursor: int | None, limit: int) -> list[dict]: ...


@dataclass
class ScanStats:
    pages: int = 0
    fetched: int = 0
    written: int = 0
    skipped: int = 0
    reason: str = ""
    started_at: str = field(default_factory=utc_now)

    def summary(self, elapsed: float) -> str:
        rate = self.written / elapsed if elapsed > 0 else 0.0
        return f"pages={self.pages} fetched={self.fetched:,} written={self.written:,} skipped={self.skipped} elapsed={elapsed:.1f}s rate={rate:.1f} rows/s reason={self.reason}"


def _decode(raw_rows: list[dict], stats: ScanStats) -> list[Tag]:
    tags: list[Tag] = []
    for raw in raw_rows:
        try:
            tags.append(Tag.from_api(raw))
        except (TagDecodeError, TypeError, ValueError) as exc:
            stats.skipped += 1
            log.warning("跳过无法解析的记录：%s", exc)
    return tags


def run_scan(
    client: PageFetcher,
    store: CsvStore,
    state: State,
    *,
    state_path: str | Path,
    floor_id: int | None = None,
    page_size: int = 1000,
    max_pages: int = 0,
    progress: ProgressFn | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ScanStats:
    """执行一次扫描，返回统计信息。调用方负责 ``store.close()``。

    ``floor_id is None`` 是全量/续传模式；否则是增量模式（只写更大的 id）。
    """
    mode = "update" if floor_id is not None else "fetch"
    stats = ScanStats()
    started = clock()

    if floor_id is None:
        cursor = state.min_id  # None => 从最新开始
        start_id = state.max_id
    else:
        latest = client.latest_id()
        if latest is None or latest <= floor_id:
            stats.reason = "no_new_tags"
            state.complete = True
            state.last_mode = mode
            state.save(state_path)
            return stats
        # page=b{id} 是严格小于，所以从 latest+1 起步才能包含最新那条
        cursor = latest + 1
        start_id = latest

    while True:
        if max_pages and stats.pages >= max_pages:
            stats.reason = "max_pages"
            break

        raw_rows = client.fetch_page(cursor, page_size)
        stats.pages += 1

        if not raw_rows:
            # 到达数据集底部
            stats.reason = "eof"
            if floor_id is None:
                state.complete = True
            break

        # 防御性校验：必须严格按 id 降序，且严格小于游标
        rows = sorted((r for r in raw_rows if cursor is None or int(r["id"]) < cursor), key=lambda r: int(r["id"]), reverse=True)
        if len(rows) != len(raw_rows):
            log.warning("第 %d 页有 %d 条 id 不小于游标，已丢弃", stats.pages, len(raw_rows) - len(rows))
        if not rows:
            raise ScanError(f"第 {stats.pages} 页全部记录 id ≥ 游标 {cursor}，无法安全续传")
        ids = [int(r["id"]) for r in rows]
        if ids != sorted(ids, reverse=True):
            raise ScanError(f"第 {stats.pages} 页未按 id 降序返回：{ids[:10]}")

        stats.fetched += len(rows)
        if start_id is None:  # 全新扫描：用首页最大 id 作为进度分母
            start_id = ids[0]
        tags = _decode(rows, stats)

        if floor_id is not None:
            tags = [t for t in tags if t.id > floor_id]

        lo = min((t.id for t in tags), default=None)
        hi = max((t.id for t in tags), default=None)
        written = store.write_tags(tags)
        stats.written += written
        state.note_rows(written, lo, hi, mode)

        store.flush()
        state.save(state_path)

        new_cursor = ids[-1]
        if floor_id is not None and new_cursor <= floor_id:
            state.complete = True
            stats.reason = "caught_up"
            break
        if cursor is not None and new_cursor >= cursor:
            raise ScanError(f"游标未前进（{cursor} → {new_cursor}），疑似 API 返回异常")
        cursor = new_cursor

        if progress is not None:
            progress(stats, cursor, start_id, clock() - started)

    state.complete = state.complete or (floor_id is None and stats.reason == "eof")
    state.last_mode = mode
    state.save(state_path)
    if progress is not None:
        progress(stats, cursor, start_id, clock() - started)
    return stats
