"""HTTP 客户端：限速、指数退避重试、ID 游标分页。

设计要点：

- 通过 ``delay`` 控制最小请求间隔，满足 Danbooru 匿名限速；
- 429/5xx 与网络异常按指数退避重试（尊重 ``Retry-After``）；
- 其余 4xx 视为致命错误直接抛出，避免无意义重试；
- ``session`` 与 ``sleep`` 可注入，便于单元测试。
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping
from typing import Any

from hanatsumi.config import (
    BACKOFF_BASE,
    BACKOFF_CAP,
    DEFAULT_DELAY,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    RETRYABLE_STATUS,
    TAGS_URL,
    USER_AGENT,
)
from hanatsumi.errors import ApiError

log = logging.getLogger(__name__)

__all__ = ["ApiError", "DanbooruClient"]


def _parse_retry_after(headers: Mapping[str, str] | None) -> float:
    raw = headers.get("Retry-After") if headers else None
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


class DanbooruClient:
    def __init__(
        self,
        *,
        session: Any | None = None,
        delay: float = DEFAULT_DELAY,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        user_agent: str = USER_AGENT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if session is None:
            import requests

            session = requests.Session()
        self._session = session
        self._delay = max(0.0, float(delay))
        self._timeout = timeout
        self._max_retries = max(0, int(max_retries))
        self._user_agent = user_agent
        self._sleep = sleep
        self._next_allowed = 0.0

        self.requests_made = 0
        self.retries = 0

    # ------------------------------------------------------------------ #
    def _throttle(self) -> None:
        now = time.monotonic()
        wait = self._next_allowed - now
        if wait > 0:
            self._sleep(wait)
        self._next_allowed = time.monotonic() + self._delay

    def _backoff(self, attempt: int, retry_after: float) -> float:
        base = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** (attempt - 1)))
        return max(base, retry_after) + random.uniform(0.0, 0.5)

    def get_json(self, params: Mapping[str, Any]) -> Any:
        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        last_error = "未知错误"
        retry_after = 0.0

        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                self.retries += 1
                delay = self._backoff(attempt, retry_after)
                log.warning("第 %d/%d 次重试，%.1fs 后重试（上次错误：%s）", attempt, self._max_retries, delay, last_error)
                self._sleep(delay)
                retry_after = 0.0

            self._throttle()
            try:
                resp = self._session.get(TAGS_URL, params=dict(params), timeout=self._timeout, headers=headers)
            except Exception as exc:  # noqa: BLE001 - 网络层异常（requests.RequestException 等）
                last_error = f"网络异常 {exc!r}"
                continue

            self.requests_made += 1
            status = int(getattr(resp, "status_code", 0))
            if status == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    last_error = f"JSON 解析失败 {exc!r}"
                    continue
            if status in RETRYABLE_STATUS:
                last_error = f"HTTP {status}"
                retry_after = _parse_retry_after(getattr(resp, "headers", None))
                continue

            body = (getattr(resp, "text", "") or "").strip()[:300]
            raise ApiError(f"请求失败 HTTP {status}: {body}", status=status)

        raise ApiError(f"重试 {self._max_retries} 次后仍失败，最后错误：{last_error}")

    # ------------------------------------------------------------------ #
    def fetch_page(self, cursor: int | None, limit: int) -> list[dict]:
        """按 id 降序取一页：``cursor=None`` 取最新，否则取 id < cursor。"""
        params: dict[str, Any] = {"limit": int(limit)}
        if cursor is not None:
            params["page"] = f"b{int(cursor)}"
        data = self.get_json(params)
        if not isinstance(data, list):
            raise ApiError(f"响应不是 JSON 数组：{type(data).__name__}")
        return data

    def latest_id(self) -> int | None:
        """当前最大的 tag id（数据集为空时返回 None）。"""
        rows = self.fetch_page(None, 1)
        if not rows:
            return None
        return int(rows[0]["id"])
