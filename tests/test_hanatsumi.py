"""离线单元测试（不访问网络）。

运行：``python -m pytest -q`` 或 ``python3 -m unittest discover -s tests``。
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

import hanatsumi.service as service_module
from hanatsumi.cli.app import get_app
from hanatsumi.cli.output import _estimate_eta, _id_progress, _progress_line
from hanatsumi.client import DanbooruClient
from hanatsumi.config import FIELDS
from hanatsumi.errors import ApiError, ScanError, UsageError
from hanatsumi.logger import LOGGER_NAME
from hanatsumi.models import Tag, TagDecodeError
from hanatsumi.scan import Progress, ScanStats, run_scan
from hanatsumi.storage import BOM, CsvStore, State, csv_stats, promote_staging, staging_paths, state_path_for

runner = CliRunner()


def make_raw(tag_id: int, name: str | None = None, **over) -> dict:
    raw = {
        "id": tag_id,
        "name": name or f"tag_{tag_id}",
        "post_count": tag_id % 100,
        "category": 0,
        "created_at": "2026-10-02T00:00:00.000-04:00",
        "updated_at": "2026-10-02T00:00:00.000-04:00",
        "is_deprecated": False,
        "words": ["tag", f"{tag_id}"],
    }
    raw.update(over)
    return raw


def scripted_client(all_ids: list[int], page_size: int = 3) -> object:
    """模拟 API：数据集按 id 升序，返回 id < cursor 的至多 page_size 条降序记录。"""
    client = type("Scripted", (), {})()

    def fetch_page(cursor: int | None, limit: int) -> list[dict]:
        ids = sorted((i for i in all_ids if cursor is None or i < cursor), reverse=True)
        return [make_raw(i) for i in ids[: min(limit, page_size)]]

    client.fetch_page = fetch_page  # type: ignore[attr-defined]
    client.latest_id = lambda: max(all_ids) if all_ids else None  # type: ignore[attr-defined]
    return client


class FakeResponse:
    def __init__(self, status_code: int, payload: object = None, headers: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """按脚本顺序返回响应，记录请求参数。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def get(self, url: str, params: dict | None = None, timeout: float | None = None, headers: dict | None = None) -> FakeResponse:
        self.calls.append(dict(params or {}))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class PatchedClient:
    """把 service.build_client 换成给定的假客户端，退出时还原。"""

    def __init__(self, client: object) -> None:
        self.client = client
        self._original = None

    def __enter__(self) -> None:
        self._original = service_module.build_client
        service_module.build_client = lambda **_kwargs: self.client  # type: ignore[assignment]

    def __exit__(self, *exc_info: object) -> None:
        service_module.build_client = self._original  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
class ModelTest(unittest.TestCase):
    def test_from_api_and_row(self) -> None:
        tag = Tag.from_api(make_raw(7, words=["alpha", "beta"], is_deprecated=True, category=5))
        self.assertEqual(tag.category_name, "meta")
        self.assertEqual(tag.post_count, 7)
        self.assertEqual(tag.row(), (7, "tag_7", 7, 5, "true", tag.created_at, tag.updated_at, "alpha beta"))

    def test_missing_field_raises(self) -> None:
        raw = make_raw(1)
        del raw["post_count"]
        with self.assertRaises(TagDecodeError):
            Tag.from_api(raw)


# --------------------------------------------------------------------------- #
class StorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_new_file_writes_bom_and_header_once(self) -> None:
        path = self.dir / "tags.csv"
        with CsvStore(path) as store:
            store.write_tags([Tag.from_api(make_raw(3)), Tag.from_api(make_raw(2))])
        with CsvStore(path) as store:  # 第二次打开：追加
            store.write_tags([Tag.from_api(make_raw(1))])

        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(BOM))
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], list(FIELDS))
        self.assertEqual(sum(1 for row in rows if row == list(FIELDS)), 1)  # 表头只出现一次
        self.assertEqual(len(rows), 4)  # 表头 + 3 行数据
        self.assertEqual(rows[1][0], "3")

    def test_repair_missing_trailing_newline(self) -> None:
        path = self.dir / "tags.csv"
        path.write_text("id,name\n1,foo", encoding="utf-8")  # 无换行结尾
        with CsvStore(path) as store:
            store.write_tags([Tag.from_api(make_raw(2))])
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[-1].split(",")[0], "2")

    def test_csv_stats(self) -> None:
        path = self.dir / "tags.csv"
        with CsvStore(path) as store:
            store.write_tags([Tag.from_api(make_raw(i)) for i in (10, 8, 12)])
        stats = csv_stats(path, check_dups=True)
        self.assertTrue(stats["header_ok"])
        self.assertEqual(stats["rows"], 3)
        self.assertEqual((stats["min_id"], stats["max_id"]), (8, 12))
        self.assertEqual(stats["duplicate_ids"], 0)
        self.assertEqual(stats["bad_rows"], 0)

    def test_state_roundtrip_and_atomic_save(self) -> None:
        path = self.dir / "tags.state.json"
        state = State(min_id=5, max_id=99, rows=10)
        state.save(path)
        loaded = State.load(path)
        assert loaded is not None
        self.assertEqual((loaded.min_id, loaded.max_id, loaded.rows), (5, 99, 10))
        self.assertFalse(loaded.complete)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["source"], state.source)
        # 原子写不残留临时文件
        self.assertEqual([p.name for p in self.dir.iterdir()], ["tags.state.json"])

    def test_state_path_derivation(self) -> None:
        self.assertEqual(state_path_for("data/tags.csv", None), Path("data/tags.state.json"))
        self.assertEqual(state_path_for("data/tags.csv", "x.json"), Path("x.json"))

    def test_staging_paths_and_promote(self) -> None:
        tmp, tmp_state = staging_paths("data/tags.csv")
        self.assertEqual(tmp, Path("data/tags.new.csv"))
        self.assertEqual(tmp_state, Path("data/tags.new.state.json"))
        # 暂存状态路径与 state_path_for 推导一致，刷新续跑时能被正常加载
        self.assertEqual(state_path_for(tmp, None), tmp_state)

        out, state_path = Path(self.dir / "tags.csv"), Path(self.dir / "tags.state.json")
        staging_csv, staging_state = staging_paths(out)
        staging_csv.write_text("new data", encoding="utf-8")
        staging_state.write_text("{}", encoding="utf-8")
        out.write_text("old data", encoding="utf-8")

        promote_staging(staging_csv, out, staging_state, state_path)
        self.assertEqual(out.read_text(encoding="utf-8"), "new data")
        self.assertEqual(state_path.read_text(encoding="utf-8"), "{}")
        self.assertFalse(staging_csv.exists())
        self.assertFalse(staging_state.exists())

    def test_promote_without_state_file(self) -> None:
        out = Path(self.dir / "tags.csv")
        staging_csv, staging_state = staging_paths(out)
        staging_csv.write_text("new", encoding="utf-8")
        promote_staging(staging_csv, out, staging_state, Path(self.dir / "tags.state.json"))
        self.assertEqual(out.read_text(encoding="utf-8"), "new")

    def test_load_missing_returns_none(self) -> None:
        self.assertIsNone(State.load(self.dir / "none.json"))


# --------------------------------------------------------------------------- #
class ClientTest(unittest.TestCase):
    def _client(self, session: FakeSession) -> DanbooruClient:
        return DanbooruClient(session=session, delay=0, max_retries=3, sleep=lambda _s: None)

    def test_cursor_param_and_success(self) -> None:
        session = FakeSession([FakeResponse(200, [make_raw(9), make_raw(8)])])
        rows = self._client(session).fetch_page(10, 1000)
        self.assertEqual(len(rows), 2)
        self.assertEqual(session.calls[0], {"limit": 1000, "page": "b10"})

    def test_top_page_has_no_page_param(self) -> None:
        session = FakeSession([FakeResponse(200, [make_raw(9)])])
        self._client(session).fetch_page(None, 5)
        self.assertEqual(session.calls[0], {"limit": 5})

    def test_latest_id(self) -> None:
        session = FakeSession([FakeResponse(200, [make_raw(42)])])
        self.assertEqual(self._client(session).latest_id(), 42)

    def test_retry_on_429_then_success(self) -> None:
        session = FakeSession([FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, [make_raw(1)])])
        client = self._client(session)
        self.assertEqual(client.fetch_page(None, 1), [make_raw(1)])
        self.assertEqual(client.retries, 1)
        self.assertEqual(len(session.calls), 2)

    def test_retry_on_network_error(self) -> None:
        session = FakeSession([ConnectionError("boom"), FakeResponse(200, [make_raw(2)])])
        self.assertEqual(self._client(session).latest_id(), 2)

    def test_fatal_4xx_no_retry(self) -> None:
        session = FakeSession([FakeResponse(403, text="forbidden")])
        with self.assertRaises(ApiError) as ctx:
            self._client(session).fetch_page(None, 1)
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(len(session.calls), 1)

    def test_gives_up_after_max_retries(self) -> None:
        session = FakeSession([FakeResponse(503)] * 4)
        with self.assertRaises(ApiError):
            self._client(session).fetch_page(None, 1)
        self.assertEqual(len(session.calls), 4)  # 1 次原始 + 3 次重试


# --------------------------------------------------------------------------- #
class ScanTest(unittest.TestCase):
    """用脚本化 client 验证扫描 / 续传 / 增量逻辑。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.csv_path = self.dir / "tags.csv"
        self.state_path = self.dir / "tags.state.json"

    def test_full_scan_writes_everything_and_completes(self) -> None:
        state = State()
        with CsvStore(self.csv_path) as store:
            stats = run_scan(scripted_client([5, 4, 3, 2, 1]), store, state, state_path=self.state_path, page_size=3)
        self.assertEqual(stats.reason, "eof")
        self.assertTrue(state.complete)
        self.assertEqual(state.rows, 5)
        self.assertEqual((state.min_id, state.max_id), (1, 5))
        got = csv_stats(self.csv_path)
        self.assertEqual(got["rows"], 5)
        self.assertEqual((got["min_id"], got["max_id"]), (1, 5))
        self.assertEqual(State.load(self.state_path).rows, 5)  # 状态已落盘

    def test_resume_after_partial_run(self) -> None:
        ids = list(range(1, 11))
        state = State()
        with CsvStore(self.csv_path) as store:
            run_scan(scripted_client(ids), store, state, state_path=self.state_path, page_size=3, max_pages=2)
        self.assertFalse(state.complete)
        self.assertEqual(state.rows, 6)

        resumed = State.load(self.state_path)
        assert resumed is not None
        with CsvStore(self.csv_path) as store:
            stats = run_scan(scripted_client(ids), store, resumed, state_path=self.state_path, page_size=3)
        self.assertEqual(stats.reason, "eof")
        self.assertTrue(resumed.complete)
        got = csv_stats(self.csv_path, check_dups=True)
        self.assertEqual(got["rows"], 10)
        self.assertEqual(got["duplicate_ids"], 0)
        self.assertEqual((got["min_id"], got["max_id"]), (1, 10))

    def test_update_only_appends_newer_tags(self) -> None:
        old_ids = list(range(1, 6))
        state = State(min_id=1, max_id=5, rows=5, complete=True)
        state.save(self.state_path)

        new_ids = old_ids + [6, 7, 8]  # 更新期间又新增了 3 个
        with CsvStore(self.csv_path) as store:
            store.write_tags([Tag.from_api(make_raw(i)) for i in old_ids])  # 已存在的数据
            stats = run_scan(scripted_client(new_ids), store, state, state_path=self.state_path, floor_id=5, page_size=2)

        self.assertEqual(stats.reason, "caught_up")
        self.assertEqual(stats.written, 3)
        got = csv_stats(self.csv_path, check_dups=True)
        self.assertEqual(got["rows"], 8)
        self.assertEqual(got["duplicate_ids"], 0)
        self.assertEqual(state.max_id, 8)
        self.assertEqual(state.min_id, 1)

    def test_update_with_no_new_tags(self) -> None:
        state = State(min_id=1, max_id=5, rows=5, complete=True)
        state.save(self.state_path)
        with CsvStore(self.csv_path) as store:
            stats = run_scan(scripted_client([1, 2, 3, 4, 5]), store, state, state_path=self.state_path, floor_id=5)
        self.assertEqual(stats.reason, "no_new_tags")
        self.assertEqual(stats.written, 0)

    def test_corrupt_page_raises_scan_error(self) -> None:
        client = type("Bad", (), {})()
        client.latest_id = lambda: 10  # type: ignore[attr-defined]
        # 每页都返回同一批 id（游标不前进）→ 必须报错而非死循环
        client.fetch_page = lambda cursor, limit: [make_raw(10), make_raw(9)]  # type: ignore[attr-defined]
        state = State()
        with CsvStore(self.csv_path) as store, self.assertRaises(ScanError):
            run_scan(client, store, state, state_path=self.state_path, floor_id=5)

    def test_progress_carries_floor_and_run_origin(self) -> None:
        seen: list[Progress] = []
        state = State(min_id=1, max_id=5, rows=5, complete=True)
        state.save(self.state_path)
        with CsvStore(self.csv_path) as store:
            store.write_tags([Tag.from_api(make_raw(i)) for i in range(1, 6)])
            run_scan(scripted_client(list(range(1, 9))), store, state, state_path=self.state_path, floor_id=5, progress=seen.append)
        self.assertTrue(seen)
        self.assertEqual(seen[-1].floor_id, 5)  # 增量模式的停止线
        self.assertIsNotNone(seen[-1].run_origin)
        self.assertIsNone(seen[-1].total_hint)


# --------------------------------------------------------------------------- #
class ProgressLineTest(unittest.TestCase):
    """进度行与 ETA：数字取自 2026-10-02 那次真实全量抓取的现场观察。"""

    @staticmethod
    def _progress(**over) -> Progress:
        # 现场：[34:50] pages=1278 rows=1,285,000 cursor=1,293,425
        base: dict[str, Any] = {
            "stats": ScanStats(pages=1278, written=1_285_000),
            "cursor": 1_293_425,
            "start_id": 2_744_001,
            "run_origin": 2_744_001,
            "floor_id": None,
            "elapsed": 34 * 60 + 50,
        }
        base.update(over)
        return Progress(**base)  # type: ignore[arg-type]

    def test_eta_not_underflow_on_fresh_fetch(self) -> None:
        """根因回归：旧公式把 remaining 算成负数 → '<1m'，实际还剩约 30 分钟。"""
        eta = _estimate_eta(self._progress())
        self.assertTrue(eta.startswith("31:"), eta)

    def test_eta_uses_row_hint_when_known(self) -> None:
        """refresh 用旧快照行数（1,836,879）当总数 → 行口径，接近真相（约 15 分钟）。"""
        eta = _estimate_eta(self._progress(total_hint=1_836_879))
        self.assertTrue(eta.startswith("14:"), eta)

    def test_eta_uses_run_origin_not_cumulative_start(self) -> None:
        """续传时速率分母必须是本次运行覆盖的区间，否则 ETA 会小得离谱。"""
        resumed = self._progress(cursor=1_280_000, run_origin=1_293_425, elapsed=16.0, stats=ScanStats(pages=10, written=10_000))
        eta = _estimate_eta(resumed)
        self.assertTrue(eta.startswith("25:"), eta)  # 若误用 start_id 会算成 0:14

    def test_eta_at_or_past_floor_is_under_a_minute(self) -> None:
        self.assertEqual(_estimate_eta(self._progress(cursor=1, floor_id=None)), "<1m")
        self.assertEqual(_estimate_eta(self._progress(cursor=2_743_986, floor_id=2_743_986, run_origin=2_744_004)), "<1m")

    def test_eta_unknown_before_first_cursor(self) -> None:
        self.assertEqual(_estimate_eta(self._progress(cursor=None, run_origin=None)), "?")

    def test_progress_line_shows_id_percentage(self) -> None:
        line = _progress_line(self._progress())
        self.assertRegex(line, r"id\s+53%")  # 1,450,576 / 2,744,000 = 52.9%（右对齐补零到 3 位）
        self.assertIn("cursor=", line)
        self.assertIn("1,293,425", line)  # 带 [dim] 包裹，拆开断言
        self.assertIn("pages=1278", line)
        self.assertIn("eta=31:03", line.replace("[yellow]", "").replace("[/yellow]", ""))

    def test_id_progress_reaches_100_at_completion(self) -> None:
        done = self._progress(cursor=1)
        self.assertAlmostEqual(_id_progress(done), 100.0, places=5)
        self.assertEqual(_id_progress(self._progress(cursor=None)), 0.0)

    def test_summary_reports_retries(self) -> None:
        self.assertIn("retries=3", ScanStats(retries=3).summary(10.0))
        self.assertNotIn("retries=", ScanStats().summary(10.0))


# --------------------------------------------------------------------------- #
class CliTest(unittest.TestCase):
    """命令行外壳：注册、退出码、错误信息。"""

    def test_help_lists_every_command(self) -> None:
        result = runner.invoke(get_app(), ["--help"])
        self.assertEqual(result.exit_code, 0)
        for name in ("fetch", "update", "refresh", "status", "verify", "reset", "version"):
            self.assertIn(name, result.output)
        self.assertIn("--debug", result.output)

    def test_version(self) -> None:
        from hanatsumi.version import VERSION

        result = runner.invoke(get_app(), ["version"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn(VERSION, result.output)

    def test_update_without_state_is_a_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = runner.invoke(get_app(), ["update", "--out", str(Path(tmp) / "tags.csv")])
        self.assertIsInstance(result.exception, UsageError)
        self.assertIn("fetch", str(result.exception))

    def test_fetch_without_csv_is_a_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = runner.invoke(get_app(), ["refresh", "--out", str(Path(tmp) / "tags.csv")])
        self.assertIsInstance(result.exception, UsageError)

    def test_reset_needs_confirmation_then_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "tags.csv"
            paths = service_module.Paths.build(out)
            out.write_text("x", encoding="utf-8")
            paths.state.write_text("{}", encoding="utf-8")

            refused = runner.invoke(get_app(), ["reset", "--out", str(out)])
            self.assertNotEqual(refused.exit_code, 0)
            self.assertTrue(out.exists())  # 没有 --yes 不能删

            done = runner.invoke(get_app(), ["reset", "--out", str(out), "--yes"])
            self.assertEqual(done.exit_code, 0)
            self.assertFalse(out.exists())
            self.assertFalse(paths.state.exists())

    def test_debug_option_enables_debug_logging(self) -> None:
        app_logger = logging.getLogger(LOGGER_NAME)
        original = app_logger.level
        try:
            result = runner.invoke(get_app(), ["--debug", "version"])
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(app_logger.level, logging.DEBUG)
        finally:
            app_logger.setLevel(original)


# --------------------------------------------------------------------------- #
class RefreshCliTest(unittest.TestCase):
    """refresh 集成测试：脚本化 client + 真实 CSV / 状态文件。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.out = self.dir / "tags.csv"
        self.state_path = self.dir / "custom.state.json"

    def _invoke(self, *args: str):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return runner.invoke(get_app(), list(args))

    def test_refresh_replaces_file_atomically(self) -> None:
        # 旧数据：3 行，其中 8 号 tag 在源里已被删除
        with CsvStore(self.out) as store:
            store.write_tags([Tag.from_api(make_raw(i)) for i in (10, 9, 8)])
        State(min_id=8, max_id=10, rows=3, complete=True).save(self.state_path)

        # 源的最新全集：1..10（新增 7 号）
        with PatchedClient(scripted_client(list(range(1, 11)))):
            result = self._invoke("refresh", "--out", str(self.out), "--state", str(self.state_path), "-q")

        self.assertEqual(result.exit_code, 0, result.output)
        stats = csv_stats(self.out, check_dups=True)
        self.assertEqual(stats["rows"], 10)
        self.assertEqual(stats["duplicate_ids"], 0)
        state = State.load(self.state_path)
        assert state is not None
        self.assertTrue(state.complete)
        self.assertEqual(state.rows, 10)
        self.assertEqual((state.min_id, state.max_id), (1, 10))
        tmp_csv, tmp_state = staging_paths(self.out)
        self.assertFalse(tmp_csv.exists())
        self.assertFalse(tmp_state.exists())

    def test_refresh_incomplete_keeps_old_file_then_resumes(self) -> None:
        with CsvStore(self.out) as store:
            store.write_tags([Tag.from_api(make_raw(i)) for i in (3, 2, 1)])
        State(min_id=1, max_id=3, rows=3, complete=True).save(self.state_path)
        before = self.out.read_text(encoding="utf-8")

        client = scripted_client(list(range(1, 11)))
        with PatchedClient(client):
            result = self._invoke("refresh", "--out", str(self.out), "--state", str(self.state_path), "--max-pages", "1", "-q")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.out.read_text(encoding="utf-8"), before)  # 旧文件未被改动
        tmp_csv, tmp_state = staging_paths(self.out)
        self.assertTrue(tmp_csv.exists())  # 暂存保留，可续跑
        self.assertIsNotNone(State.load(tmp_state))

        with PatchedClient(client):  # 续跑到完成 → 替换
            result = self._invoke("refresh", "--out", str(self.out), "--state", str(self.state_path), "-q")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(csv_stats(self.out)["rows"], 10)
        self.assertFalse(tmp_csv.exists())

    def test_refresh_injects_previous_row_count_as_total_hint(self) -> None:
        """旧快照行数 → total_hint → 行口径 ETA。"""
        with CsvStore(self.out) as store:
            store.write_tags([Tag.from_api(make_raw(i)) for i in (10, 9, 8)])
        State(min_id=8, max_id=10, rows=3, complete=True).save(self.state_path)

        seen: list[Progress] = []
        with PatchedClient(scripted_client(list(range(1, 11)))):
            service_module.refresh(service_module.Paths.build(self.out, self.state_path), progress=seen.append)

        self.assertTrue(seen)
        self.assertEqual(seen[-1].total_hint, 3)
        # 行口径确实被采用：3 行为总数、进度写满后不再外推
        self.assertEqual(_estimate_eta(seen[-1]), "<1m")


if __name__ == "__main__":
    unittest.main()
