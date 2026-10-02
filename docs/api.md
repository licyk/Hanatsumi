# Danbooru Tags API 调研

> 端点：`GET https://danbooru.donmai.us/tags.json`，匿名可访问（无需 API key）。
> 以下结论均为 **2026-10 实测**，每一条都直接决定了本项目的一个设计选择。

## 分页与字段约束

| 结论 | 依据 | 对设计的影响 |
|---|---|---|
| 单页 `limit` 最大 1000 条 | `limit=1000` 返回 1000 条 | 每页按 1000 条抓取（`--page-size` 上限 1000） |
| **偏移分页最多 1000 页** | `page=100000` → `PaginationError: You cannot go beyond page 1000.` | 偏移分页上限 = 100 万条，而 tag id 已到 **274 万**，**必须用 ID 游标分页** |
| `page=b{id}` 返回 **id 严格小于** cursor，按 id **降序** | `page=b2743963&limit=5` → `2743962, 2743961, …`；深游标 `page=b100000&limit=1000` 返回 1000 条且严格降序 | 全量扫描 = 从顶部开始 `page=b{cursor}` 不断下探，直到空页 |
| `page=a{id}` 返回 id **严格大于** cursor | `page=a2743960&limit=5` → `2743965 … 2743961` | 备用；本项目统一用 `b` 游标 + floor 过滤实现增量 |
| 默认排序即 id 降序 | 不带 `search[order]` 返回最新 id 在前 | 无需额外排序参数（非法 `order` 值会被静默忽略） |
| **`search[updated_after]` / `updated_before` 会被静默忽略** | 传入后返回结果与不传完全一致 | 无法按更新时间增量拉取 → `post_count` 变化、改名、废弃、删除都**只能靠全量 `refresh` 同步** |
| 匿名可访问，需遵守限速 | 无 key 返回 200 | 默认 **1 请求/秒** + 429/5xx 指数退避重试 |

> ⚠️ 如果按“偏移分页”写（`page=1,2,3…`），数据量一旦超过 100 万条就会直接报错，
> 而且中途无法定位续传点 —— 这是本项目采用 **ID 游标** 的根本原因。

## category 数值映射

按 `search[category]=N` 逐个实测：

| 值 | 含义 |
|---|---|
| `0` | general |
| `1` | artist |
| `2` | unused（查询为空） |
| `3` | copyright |
| `4` | character |
| `5` | meta |

CSV 里保存的是原始数值列 `category`，含义对照见上表（`hanatsumi.models.Tag.category_name` 也使用同一映射）。

## 返回字段

`tags.json` 每条记录中本项目采集的 8 个字段（见 `hanatsumi.config.FIELDS`）：

`id`, `name`, `post_count`, `category`, `is_deprecated`, `created_at`, `updated_at`, `words`

## 实测规模（2026-10-02 全量抓取）

| 指标 | 数值 |
|---|---|
| 数据行数 | **1,836,879** 行 |
| CSV 体积 | 192.8 MiB |
| id 区间 | `[2, 2743986]`（id 有空洞：被删除的 tag 不会再返回，因此行数 < max_id） |
| 页数 | ≈ 1837 页 |
| 耗时 | 1 req/s 限速下约 **31 分钟** |
| 抓取后新增 | 31 分钟后执行一次 `hanatsumi update`，新增 17 个 tag |

> `post_count` 是 counter cache，随时在变，且 `1girl.updated_at` 之类的字段会停在很早的时间
> （改计数不改 tag 行）。所以 CSV 是**某一时刻的快照**，属数据源特性而非工具缺陷。
