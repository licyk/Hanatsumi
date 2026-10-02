# 设计文档

API 层面的硬约束见 [api.md](api.md)；本篇是这些约束下推导出的实现设计。

## 1. 分层架构

```text
hanatsumi/
├── version.py           # VERSION，pyproject 动态读取
├── errors.py            # HanatsumiError / UsageError / StateError / ScanError / ApiError
├── logger.py            # setup_logging()：RichHandler 彩色日志（stderr）
├── config.py            # API 地址、字段清单、category 映射、默认参数
├── models.py            # Tag dataclass：from_api() 解析 / row() 编码 CSV 行
├── client.py            # HTTP 客户端：限速、指数退避重试、游标分页
├── storage.py           # CsvStore（表头/BOM/追加）、State（原子写）、暂存与原子替换
├── scan.py              # 扫描循环：分页 → 写 CSV → 每页落状态
├── service.py           # 业务编排：fetch/update/refresh/status/verify/reset
└── cli/
    ├── factory.py       # typer app/group 构造 + 全层级 --debug
    ├── output.py        # Rich 表格、逐页进度行、运行摘要
    ├── options.py       # 三个抓取命令共用的选项别名
    └── commands/        # 命令实现（薄封装）
```

**分层职责**：`client` 只管“拿到一页”，`scan` 只管“怎么推进游标与状态”，
`storage` 只管“怎么安全落盘”，`service` 只管“动作的前置条件与取舍”，
`cli` 只管“参数解析与结果渲染”。每一层都可通过注入 fake 实现离线测试
（`session` / `sleep` / `client` / `store`）。

## 2. CLI：薄封装

命令注册只发生在 `cli/app.py`：

```python
app = typer_factory("…")
app.command(help="全量抓取 tag（可中断，重跑同一条命令续传）", name="fetch")(fetch)
```

`cli/commands/*.py` 里的每个函数只做三件事：把 typer 参数转发给 `service`、
渲染结果、把失败翻译成退出码。所有**业务判断**（状态文件是否允许 update、
CSV 是否可被 fetch 续传、refresh 何时替换正式文件）都在 `service.py`，
因此同一套逻辑既能被命令行调用，也能被测试或其它程序 `import hanatsumi.service` 使用。

### 输出与日志的分工

| 通道 | 内容 | 实现 |
|---|---|---|
| **日志**（stderr，彩色等级） | 摘要、告警、错误、重试、刷新进度提示 | `hanatsumi.logger.setup_logging()` → `RichHandler` |
| **命令结果**（stdout） | `status` / `verify` / `version` 的表格 | `cli/output.py` 的 `console` + `print_table` |
| **逐页进度**（stderr，原地刷新） | 扫描时那条 `pages/rows/rate/eta` 单行 | `cli/output.py` 的 `scan_progress()` → `err_console`（`\r`），结束时换行 |

即：**没有任何裸 `print()`**；结构化结果走 Rich 控制台，其余全部走日志。
日志级别默认 `INFO`，可用环境变量 `HANATSUMI_LOG_LEVEL` 或全局 `--debug` 调整。

### 退出码

| 退出码 | 含义 | 来源 |
|---|---|---|
| `0` | 成功 | — |
| `1` | 运行时错误 / 校验 FAIL | `HanatsumiError`、`verify` 不通过 |
| `2` | 用法或前置条件不满足 | `UsageError`（如全量没做完就想 `update`） |
| `130` | 用户 Ctrl-C | `KeyboardInterrupt`，已保存断点 |
| 其它 | 未预期异常 | `logger.exception` 打栈后退出 |

## 3. 分页策略：ID 游标降序扫描

```text
cursor = None（取最新）
loop:
    page = GET tags.json?limit=1000&page=b{cursor}     # 取 id < cursor 的 1000 条
    page 为空 → 扫描到底，state.complete = True，结束
    写入 CSV；state.min_id = min(page ids)
    cursor = min(page ids)                              # 游标严格单调下降
```

游标单调性由两层保证：

1. **服务端语义**：`b{cursor}` 天然只返回 `id < cursor`；
2. **客户端防御**：丢弃 `id ≥ cursor` 的记录、校验降序、断言 `new_cursor < cursor`，
   否则抛 `ScanError`（宁可报错也不死循环/写脏数据）。

## 4. 断点续传与增量更新（状态文件不变量）

`tags.state.json` 只维护 4 个关键字段：

| 字段 | 含义 | 用途 |
|---|---|---|
| `min_id` | 已写入 CSV 的**最小** id | **续传游标**：下次 `fetch` 从 `page=b{min_id}` 继续，天然不与已有数据重叠 |
| `max_id` | 已写入 CSV 的**最大** id | **增量下界 (floor)**：`update` 只写 `id > max_id` 的行 |
| `rows` | 已写入行数 | 与 CSV 实际行数对账 |
| `complete` | 是否到达数据集底部 | `update` 只允许在全量完成后执行 |

写入节奏：**每页结束 → flush CSV → 原子写状态**（临时文件 + `os.replace`）。
因此 Ctrl-C / 断电 / OOM 最多丢“正在写的那一页”，重跑同命令即可继续。

```text
fetch   : 游标 = state.min_id（或顶部）  → 向下扫 → 空页   → complete=True
update  : 游标 = 当前 API 最大 id + 1     → 向下扫 → 游标 ≤ state.max_id → 停止
```

> 增量起点用 `latest_id + 1` 而不是 `latest_id`：因为 `page=b{id}` 是**严格小于**，
> 若从 `latest_id` 起步会漏掉最新那一条（单测 `test_update_only_appends_newer_tags` 专门覆盖此坑）。

**为什么 `update` 之前必须 `complete=True`**：全量扫描期间在顶部新建的 tag
（id 大于扫描起点）当时不会被抓到，只有“全量已完成”的状态才能保证
`update` 的 floor 是可靠的全量上界，从而把这部分补齐。

## 5. 限速与重试

- 最小请求间隔 `--delay`（默认 **1.0s**，匿名建议 ≥ 1.0s，低于 0.5s 会告警）；
- `429 / 500 / 502 / 503 / 504` 与网络异常 → 指数退避 `1,2,4,8…`（封顶 60s，加 0~0.5s 抖动），
  并读取 `Retry-After` 作为下限；
- 其余 4xx（如 403/404）**直接失败不重试**，避免把参数错误重试 8 次；
- 每次重试打 WARNING 日志，便于观察是否被限速。

## 6. CSV 规范

- 编码 UTF-8；**仅新建文件时写 BOM**（Excel 双击不乱码），追加时不重复写 BOM；
- 表头 = `config.FIELDS`，追加时不重复写；
- 自动修补异常中断导致的“末尾缺换行”；
- 列与类型：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | int | tag id，游标分页依据 |
| `name` | str | tag 名 |
| `post_count` | int | 使用该 tag 的帖子数 |
| `category` | int | `0=general 1=artist 3=copyright 4=character 5=meta` |
| `is_deprecated` | `true`/`false` | 是否废弃 |
| `created_at` / `updated_at` | ISO8601 字符串 | 原样保留 API 时间戳 |
| `words` | str | tag 分词，空格拼接（tag 名本身不含空格） |

## 7. 数据源更新了怎么办：`update` 与 `refresh` 的分工

| 源数据的变化 | `update` | `refresh` | 原因 |
|---|---|---|---|
| **新增 tag** | ✅ 只抓几秒 | ✅ | `update` 从 `max_id` 扫到 API 最大 id 即停 |
| 全量扫描**期间**新建的 tag | ✅ | ✅ | 两者的起点都是“当前 API 最大 id” |
| `post_count` 变化 | ❌ | ✅ | Danbooru 是 counter cache，改计数**不改 tag 行**；且 API 不支持按更新时间过滤（见 [api.md](api.md)），无法增量取 |
| 改名 / 废弃 / 改分类 | ❌ | ✅ | 会改 tag 行，但同样无法按更新时间增量查询 |
| tag 被删除 | ❌ | ✅（直接丢弃） | `update` 只追加，不删除 |

`refresh` 的实现要点：

1. 全量重扫到**暂存文件** `tags.new.csv`（+ `tags.new.state.json`），
   **正式文件 `tags.csv` 全程不动、可正常读取**；
2. 只有扫描真正到达底部（`reason=eof`）才 `os.replace` 原子替换：
   先换 CSV、后换状态文件（状态文件是“数据已一致”的标记）；
3. 中断 / 失败 → 保留暂存，重跑 `refresh` 自动续传；暂存状态文件若丢失，
   会按暂存 CSV 自动重建（`complete=False`，扫描会补到底部）；
4. 未完成（如 `--max-pages` 提前停止）时**不会**替换正式文件；
5. `reset --yes` 会连暂存文件一起清理。

> `refresh` 完成后建议再跑一次 `update`：刷新期间新建的 tag 不在本轮快照里。

**建议节奏**：每天 `update`（秒级），需要准确 `post_count` 时每周 `refresh`（约 1 小时）。

## 8. 测试

```bash
python -m pytest -q          # 或 python3 -m unittest discover -s tests
```

30 个用例全部离线运行，覆盖：

- `Tag` 解析与 CSV 行编码、缺字段报错；
- CSV 新建写 BOM + 表头只出现一次、追加不重复表头、末尾缺换行自动修补；
- 状态文件原子写（无临时文件残留）、往返序列化；暂存路径推导与原子替换；
- 客户端：游标参数、`latest_id`、429/网络异常退避重试、4xx 不重试、重试耗尽；
- 扫描：全量到底、**分段中断后续传不重复**、**增量只追加新行且 floor 停止**、
  无新 tag、服务端返回异常数据时报 `ScanError` 而非死循环；
- CLI（typer `CliRunner`）：`--help` 覆盖全部命令与 `--debug`、`update` 前置条件报
  `UsageError`、`reset` 需 `--yes` 才删、`--debug` 生效；
- `refresh` 集成测试（脚本化 client）：完成后原子替换、被删 tag 不再保留、
  未完成时正式文件保持原样且暂存可续跑。

CLI 的端到端流程（fetch 中断续传 → update → refresh 暂存/崩溃恢复 → verify → reset）
已用真实 API 手工冒烟验证，并在 184 万行的真实数据上跑过 `verify --dups` / `update`。
