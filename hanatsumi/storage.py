"""CSV 落盘与断点状态（State）管理。

CSV 约定
--------
- 编码 UTF-8；**仅在新建文件**时写入 BOM（``\\ufeff``），保证 Excel 直接双击
  打开不乱码，同时避免追加时重复写 BOM 损坏文件；
- 表头为 ``config.FIELDS``，追加模式下不重复写表头；
- 若旧文件末尾缺少换行（异常中断导致），补一个换行再继续追加。

状态文件约定
------------
``*.state.json`` 记录扫描进度的关键不变量：

- ``min_id``：已写入 CSV 的最小 id —— 降序扫描的**续传游标**（下次从
  ``page=b{min_id}`` 继续，天然不会与已有数据重叠）；
- ``max_id``：已写入 CSV 的最大 id —— 增量更新的**下界 floor**（只写
  ``id > max_id`` 的新行）；
- ``rows``：已写入数据行数，用于与 CSV 实际行数对账；
- ``complete``：是否已完成一次全量扫描（到达数据集底部）。

状态文件采用 “临时文件 + ``os.replace``” 原子替换写入，避免中断留下半截 JSON。
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hanatsumi.config import FIELDS, TAGS_URL
from hanatsumi.errors import StateError
from hanatsumi.models import Tag

BOM = "\ufeff"

__all__ = ["BOM", "CsvStore", "State", "csv_stats", "iter_row_ids", "promote_staging", "staging_paths", "state_path_for", "utc_now"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
class CsvStore:
    """追加写 CSV，负责表头/BOM/刷新。"""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._fh: Any = None
        self._writer: Any = None
        self.rows_written = 0

    # -- 生命周期 -------------------------------------------------------- #
    def open(self) -> CsvStore:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists() or self.path.stat().st_size == 0
        self._fh = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        if fresh:
            # 新建文件才写 BOM + 表头
            self._fh.write(BOM)
            self._writer.writerow(FIELDS)
            self._fh.flush()
        else:
            self._repair_trailing_newline()
        return self

    def _repair_trailing_newline(self) -> None:
        with self.path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                return
            fh.seek(-1, os.SEEK_END)
            last = fh.read(1)
        if last not in (b"\n", b"\r"):
            self._fh.write("\n")
            self._fh.flush()

    def write_tags(self, tags: Sequence[Tag]) -> int:
        if self._writer is None:
            raise RuntimeError("CsvStore 尚未 open()")
        if not tags:
            return 0
        self._writer.writerows(tag.row() for tag in tags)
        self.rows_written += len(tags)
        return len(tags)

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
            finally:
                self._fh.close()
                self._fh = None
                self._writer = None

    def __enter__(self) -> CsvStore:  # noqa: PYI034 - 保持 Python 3.10 兼容，不用 3.11 的 typing.Self
        return self.open()

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def csv_stats(path: str | os.PathLike[str], *, check_dups: bool = False) -> dict:
    """快速统计 CSV：表头、行数、id 范围、坏行，可选重复 id 检查。"""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return {"exists": False, "rows": 0, "min_id": None, "max_id": None, "header_ok": False, "bad_rows": 0, "size_bytes": 0, "duplicate_ids": None, "header": None}

    stats: dict[str, Any] = {
        "exists": True,
        "size_bytes": p.stat().st_size,
        "rows": 0,
        "min_id": None,
        "max_id": None,
        "bad_rows": 0,
        "header": None,
        "header_ok": False,
        "duplicate_ids": None,
    }
    seen: set[int] | None = set() if check_dups else None
    duplicates = 0

    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        stats["header"] = header
        stats["header_ok"] = list(header or []) == list(FIELDS)
        for row in reader:
            if not row:
                continue
            if len(row) != len(FIELDS):
                stats["bad_rows"] += 1
                continue
            try:
                row_id = int(row[0])
            except ValueError:
                stats["bad_rows"] += 1
                continue
            stats["rows"] += 1
            lo, hi = stats["min_id"], stats["max_id"]
            stats["min_id"] = row_id if lo is None else min(lo, row_id)
            stats["max_id"] = row_id if hi is None else max(hi, row_id)
            if seen is not None:
                if row_id in seen:
                    duplicates += 1
                else:
                    seen.add(row_id)

    stats["duplicate_ids"] = duplicates if check_dups else None
    return stats


def iter_row_ids(path: str | os.PathLike[str]) -> Iterator[int]:
    """按行产出 id，供轻量校验使用。"""
    with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        for row in reader:
            if row:
                yield int(row[0])


# --------------------------------------------------------------------------- #
# 断点状态
# --------------------------------------------------------------------------- #
@dataclass
class State:
    version: int = 1
    source: str = TAGS_URL
    min_id: int | None = None  # 续传游标（已写入的最小 id）
    max_id: int | None = None  # 增量下界（已写入的最大 id）
    rows: int = 0
    complete: bool = False  # 全量扫描是否已到达底部
    last_mode: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    # -- 持久化 ---------------------------------------------------------- #
    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> State | None:
        p = Path(path)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"状态文件损坏：{p}（{exc}），可用 `hanatsumi verify --init-state` 重建") from exc
        if not isinstance(data, dict):
            raise StateError(f"状态文件格式异常：{p}，可用 `hanatsumi verify --init-state` 重建")
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: str | os.PathLike[str]) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        now = utc_now()
        self.created_at = self.created_at or now
        self.updated_at = now
        fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(asdict(self), fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, p)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    # -- 进度更新 -------------------------------------------------------- #
    def note_rows(self, written: int, lo: int | None, hi: int | None, mode: str) -> None:
        if written:
            self.rows += written
            if lo is not None:
                self.min_id = lo if self.min_id is None else min(self.min_id, lo)
            if hi is not None:
                self.max_id = hi if self.max_id is None else max(self.max_id, hi)
        self.last_mode = mode

    @property
    def scanned_range(self) -> str:
        if self.min_id is None or self.max_id is None:
            return "(空)"
        return f"[{self.min_id}, {self.max_id}]"


# --------------------------------------------------------------------------- #
# refresh 的暂存与原子替换
# --------------------------------------------------------------------------- #
def state_path_for(out_path: str | os.PathLike[str], explicit: str | os.PathLike[str] | None) -> Path:
    if explicit:
        return Path(explicit)
    p = Path(out_path)
    return p.with_suffix(".state.json")


def staging_paths(out_path: str | os.PathLike[str]) -> tuple[Path, Path]:
    """refresh 的暂存文件路径：tags.csv → (tags.new.csv, tags.new.state.json)。"""
    p = Path(out_path)
    tmp = p.with_name(p.stem + ".new" + p.suffix)
    return tmp, tmp.with_suffix(".state.json")


def promote_staging(
    tmp_out: str | os.PathLike[str],
    out: str | os.PathLike[str],
    tmp_state: str | os.PathLike[str],
    state_path: str | os.PathLike[str],
) -> None:
    """把暂存产物原子切换为正式产物。

    先替换 CSV 再替换状态文件：状态文件是“数据已一致”的标记，
    中途崩溃时最坏情况是「新 CSV + 旧状态」，用 ``verify --init-state`` 即可修复。
    """
    tmp_out, out, tmp_state, state_path = map(Path, (tmp_out, out, tmp_state, state_path))
    os.replace(tmp_out, out)
    if tmp_state.exists():
        os.replace(tmp_state, state_path)
