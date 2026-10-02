"""业务编排：六个动作 fetch / update / refresh / status / verify / reset。

CLI 只做参数解析与结果渲染，所有前置条件检查、文件落盘、暂存与原子替换都在这里，
因此既能被命令行调用，也能被测试或其它程序直接 ``import hanatsumi.service``。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from hanatsumi.client import DanbooruClient
from hanatsumi.config import (
    DEFAULT_DELAY,
    DEFAULT_MAX_RETRIES,
    DEFAULT_OUT,
    DEFAULT_PAGE_SIZE,
    DEFAULT_TIMEOUT,
    MIN_RECOMMENDED_DELAY,
)
from hanatsumi.errors import UsageError
from hanatsumi.scan import ProgressFn, ScanStats, run_scan
from hanatsumi.storage import (
    CsvStore,
    State,
    csv_stats,
    promote_staging,
    staging_paths,
    state_path_for,
)

log = logging.getLogger(__name__)

__all__ = [
    "Paths",
    "Report",
    "RunResult",
    "build_client",
    "fetch",
    "refresh",
    "reset",
    "reset_targets",
    "status",
    "update",
    "verify",
]


# --------------------------------------------------------------------------- #
# 路径与结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Paths:
    """一次运行涉及的文件：正式 CSV、正式状态文件，以及 refresh 的暂存文件。"""

    out: Path
    state: Path

    @classmethod
    def build(cls, out: str | Path = DEFAULT_OUT, state: str | Path | None = None) -> Paths:
        out_path = Path(out)
        return cls(out=out_path, state=state_path_for(out_path, state))

    @property
    def staging(self) -> tuple[Path, Path]:
        return staging_paths(self.out)


@dataclass
class RunResult:
    """一次抓取动作的结果，交给 CLI 渲染。"""

    paths: Paths
    state: State
    mode: str  # fetch / update / refresh
    stats: ScanStats | None = None  # None 表示没有真正开抓（例如已是全量完成态）
    promoted: bool = False  # refresh：已原子替换正式文件
    elapsed: float = 0.0  # 本次扫描耗时（秒）

    @property
    def ran(self) -> bool:
        return self.stats is not None

    @property
    def csv_path(self) -> Path:
        """本次结果实际写到的 CSV：refresh 未完成时还是暂存文件。"""
        if self.mode == "refresh" and not self.promoted:
            return self.paths.staging[0]
        return self.paths.out


@dataclass
class Report:
    """status / verify 的结构化结果。"""

    paths: Paths
    state: State | None
    csv: dict
    staging: Path
    staging_exists: bool
    mismatch: bool = False  # status：CSV 行数与状态不一致
    state_match: bool | None = None  # verify：对账结果，None = 没有状态文件
    rebuilt: bool = False  # verify --init-state 已重建状态
    duplicates: int | None = None

    @property
    def ok(self) -> bool:
        if not self.csv.get("header_ok"):
            return False
        if self.csv.get("bad_rows", 0):
            return False
        if self.duplicates:
            return False
        return self.state_match is not False


# --------------------------------------------------------------------------- #
# 客户端与扫描
# --------------------------------------------------------------------------- #
def build_client(*, delay: float = DEFAULT_DELAY, timeout: float = DEFAULT_TIMEOUT, max_retries: int = DEFAULT_MAX_RETRIES) -> DanbooruClient:
    """按命令行参数构造 HTTP 客户端（测试里可整体替换这一个符号）。"""
    if delay < MIN_RECOMMENDED_DELAY:
        log.warning("--delay %.2fs 低于 %.1fs，容易触发 Danbooru 匿名限速 429", delay, MIN_RECOMMENDED_DELAY)
    return DanbooruClient(delay=delay, timeout=timeout, max_retries=max_retries)


def _scan(
    *,
    client: DanbooruClient,
    out: Path,
    state: State,
    state_path: Path,
    floor_id: int | None,
    page_size: int,
    max_pages: int,
    progress: ProgressFn | None,
) -> tuple[ScanStats, float]:
    store = CsvStore(out).open()
    started = time.monotonic()
    try:
        stats = run_scan(client, store, state, state_path=state_path, floor_id=floor_id, page_size=page_size, max_pages=max_pages, progress=progress)
        return stats, time.monotonic() - started
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 动作
# --------------------------------------------------------------------------- #
def fetch(
    paths: Paths,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = 0,
    delay: float = DEFAULT_DELAY,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    progress: ProgressFn | None = None,
    client: DanbooruClient | None = None,
) -> RunResult:
    """全量抓取；已有断点则从 ``state.min_id`` 续传。"""
    staging_out, _ = paths.staging
    if staging_out.exists():
        log.warning("存在未完成的刷新暂存文件 %s（不影响 fetch，可用 `reset --yes` 清掉）", staging_out)

    state = State.load(paths.state)
    if state is None and paths.out.exists() and paths.out.stat().st_size > 0:
        raise UsageError(
            f"{paths.out} 已存在但缺少状态文件 {paths.state}。\n  继续这份数据：hanatsumi verify --init-state，再 hanatsumi fetch\n  重新抓取：删除该 CSV 后再运行 fetch"
        )
    if state is not None and state.complete:
        return RunResult(paths=paths, state=state, mode="fetch", stats=None)

    state = state or State()
    stats, elapsed = _scan(
        client=client or build_client(delay=delay, timeout=timeout, max_retries=max_retries),
        out=paths.out,
        state=state,
        state_path=paths.state,
        floor_id=None,
        page_size=page_size,
        max_pages=max_pages,
        progress=progress,
    )
    return RunResult(paths=paths, state=state, mode="fetch", stats=stats, elapsed=elapsed)


def update(
    paths: Paths,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = 0,
    delay: float = DEFAULT_DELAY,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    progress: ProgressFn | None = None,
    client: DanbooruClient | None = None,
) -> RunResult:
    """只抓 ``id > state.max_id`` 的新 tag；必须在全量抓取完成之后。"""
    state = State.load(paths.state)
    if state is None:
        raise UsageError("尚无状态文件，请先运行 `hanatsumi fetch` 完成一次全量抓取。")
    if not state.complete:
        raise UsageError("全量抓取尚未完成，update 只能在全量之后使用；请先运行 `hanatsumi fetch`。")

    floor = state.max_id
    mode = "update"
    if floor is None:  # 空状态：没有可比较的下界，退化成全量抓取
        mode = "fetch"
        log.info("状态为空，转为全量抓取。")

    stats, elapsed = _scan(
        client=client or build_client(delay=delay, timeout=timeout, max_retries=max_retries),
        out=paths.out,
        state=state,
        state_path=paths.state,
        floor_id=floor,
        page_size=page_size,
        max_pages=max_pages,
        progress=progress,
    )
    return RunResult(paths=paths, state=state, mode=mode, stats=stats, elapsed=elapsed)


def refresh(
    paths: Paths,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = 0,
    delay: float = DEFAULT_DELAY,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    progress: ProgressFn | None = None,
    client: DanbooruClient | None = None,
) -> RunResult:
    """全量重扫到暂存文件，真正到达底部才原子替换正式文件。"""
    if not paths.out.exists() or paths.out.stat().st_size == 0:
        raise UsageError("尚无 CSV，请先运行 `hanatsumi fetch` 完成一次全量抓取。")

    tmp_out, tmp_state = paths.staging
    if tmp_out.exists():
        state = State.load(tmp_state)
        if state is None:
            stats = csv_stats(tmp_out)
            if not stats.get("header_ok"):
                raise UsageError(f"残留暂存文件 {tmp_out} 无法解析，请删除后重试。")
            # 中途崩溃（CSV 有数据但状态丢失）：按 CSV 重建，complete=False 会自动补到底
            state = State(min_id=stats["min_id"], max_id=stats["max_id"], rows=stats["rows"], complete=False, last_mode="rebuild")
            state.save(tmp_state)
            log.warning("暂存状态文件丢失，已按 %s 重建并续跑", tmp_out)
        else:
            log.info("续跑刷新：已写入 %s 行，继续…", f"{state.rows:,}")
    else:
        state = State()
        log.info("开始刷新：全量重扫到暂存文件 %s，完成后原子替换 %s（期间旧文件可正常读取）", tmp_out, paths.out)

    stats, elapsed = _scan(
        client=client or build_client(delay=delay, timeout=timeout, max_retries=max_retries),
        out=tmp_out,
        state=state,
        state_path=tmp_state,
        floor_id=None,
        page_size=page_size,
        max_pages=max_pages,
        progress=progress,
    )
    if stats.reason != "eof" or not state.complete:
        log.warning("刷新未完成（%s），暂存于 %s，重跑 `hanatsumi refresh` 继续；正式文件 %s 未被改动", stats.reason, tmp_out, paths.out)
        return RunResult(paths=paths, state=state, mode="refresh", stats=stats, elapsed=elapsed, promoted=False)

    promote_staging(tmp_out, paths.out, tmp_state, paths.state)
    return RunResult(paths=paths, state=state, mode="refresh", stats=stats, elapsed=elapsed, promoted=True)


def status(paths: Paths) -> Report:
    """读取状态文件与 CSV 的概况，不做全表扫描以外的事。"""
    state = State.load(paths.state)
    staging_out, _ = paths.staging
    csv = csv_stats(paths.out)
    mismatch = bool(state and csv.get("exists") and csv["rows"] != state.rows)
    return Report(paths=paths, state=state, csv=csv, staging=staging_out, staging_exists=staging_out.exists(), mismatch=mismatch)


def verify(paths: Paths, *, dups: bool = False, init_state: bool = False) -> Report:
    """校验 CSV，必要时依据 CSV 重建状态文件。"""
    if not paths.out.exists():
        raise UsageError(f"{paths.out} 不存在")

    csv = csv_stats(paths.out, check_dups=dups)
    state = State.load(paths.state)
    staging_out, _ = paths.staging
    rebuilt = False
    state_match: bool | None = None

    if init_state:
        # 无条件依据 CSV 重建（覆盖已有状态）。complete 保守置 False：
        # 下一次 fetch 会从 CSV 最小 id 续扫到底并置位，避免误判“已全量”。
        state = State(min_id=csv["min_id"], max_id=csv["max_id"], rows=csv["rows"], complete=False, last_mode="rebuild")
        state.save(paths.state)
        rebuilt = True
        state_match = True
        log.warning("已重建状态文件 %s（complete=False，运行 `hanatsumi fetch` 会自动收尾）", paths.state)
    elif state is None:
        state_match = None
    else:
        state_match = state.rows == csv["rows"] and state.min_id == csv["min_id"] and state.max_id == csv["max_id"]

    return Report(
        paths=paths,
        state=state,
        csv=csv,
        staging=staging_out,
        staging_exists=staging_out.exists(),
        state_match=state_match,
        rebuilt=rebuilt,
        duplicates=csv.get("duplicate_ids"),
    )


def reset_targets(paths: Paths) -> list[Path]:
    """reset 会删除的全部文件（含 refresh 暂存）。"""
    tmp_out, tmp_state = paths.staging
    return [paths.out, paths.state, tmp_out, tmp_state]


def reset(paths: Paths) -> list[Path]:
    """删除数据文件，返回真正删掉的路径。"""
    deleted = []
    for target in reset_targets(paths):
        if target.exists():
            target.unlink()
            deleted.append(target)
            log.info("已删除 %s", target)
    return deleted
