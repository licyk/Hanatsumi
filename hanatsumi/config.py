"""全局配置与常量。

API 约束（2026-10 实测，详见 ``docs/api.md``）：

- ``tags.json`` 单页 ``limit`` 最大 1000 条；
- 偏移分页最多 1000 页（``page=100000`` 返回 PaginationError），全量抓取
  必须使用 ID 游标分页 ``page=b{id}``；
- 默认排序为 id 降序；``page=b{id}`` 返回 id 严格小于 cursor 的记录，
  ``page=a{id}`` 返回 id 严格大于 cursor 的记录；
- ``search[updated_after]`` / ``updated_before`` 会被静默忽略，无法按更新时间增量拉取；
- category 数值实测：0=general, 1=artist, 2=未使用, 3=copyright,
  4=character, 5=meta。
"""

from __future__ import annotations

API_BASE = "https://danbooru.donmai.us"
TAGS_URL = f"{API_BASE}/tags.json"
USER_AGENT = "hanatsumi/0.1 (local research script)"

# 单页条数：API 上限 1000
DEFAULT_PAGE_SIZE = 1000
MAX_PAGE_SIZE = 1000

# 匿名访问限速（默认 1 请求/秒），可调大但会更容易触发 429
DEFAULT_DELAY = 1.0
MIN_RECOMMENDED_DELAY = 0.5
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 8
BACKOFF_BASE = 1.0
BACKOFF_CAP = 60.0
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

DEFAULT_OUT = "data/tags.csv"

# CSV 列（顺序即写入顺序），对应 tags.json 的原始字段
FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "post_count",
    "category",
    "is_deprecated",
    "created_at",
    "updated_at",
    "words",
)

# 实测得到的 category 映射；2 与 6+ 目前查询为空
CATEGORY_NAMES: dict[int, str] = {
    0: "general",
    1: "artist",
    2: "unused",
    3: "copyright",
    4: "character",
    5: "meta",
}
