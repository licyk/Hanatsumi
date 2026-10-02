<div align="center">

# Hanatsumi

</div>

抓取 [Danbooru Tags API](https://danbooru.donmai.us/tags.json) 的**全部 tag**，保存为本地 CSV，
支持断点续传、增量更新与全量刷新。匿名访问、按 1 请求/秒限速，2026-10 实测 184 万行约 31 分钟。

- **断点续传** —— ID 游标降序扫描（不用会被限死在 100 万条的偏移分页），每页 flush CSV
  并原子写断点，Ctrl-C / 断网后重跑同一条命令接着抓。
- **增量更新** —— `hanatsumi update` 只抓上次之后新增的 tag，秒级完成。
- **全量刷新** —— `hanatsumi refresh` 全量重扫到暂存文件，**真正到达底部才原子替换**，
  期间旧文件可正常读取；用于同步 `post_count`、改名、废弃与删除。
- **校验恢复** —— `hanatsumi verify` 对账行数 / id 范围 / 坏行 / 重复 id，状态文件丢失可按 CSV 重建。

分页与限速的实测依据见 [docs/api.md](docs/api.md)，完整设计（状态不变量、
`update` 与 `refresh` 的分工、日志与输出策略、退出码）见 [docs/design.md](docs/design.md)。

## 安装

Python 3.10 及以上：

```bash
pip install hanatsumi
```

从源码（含开发工具 ruff / ty / pytest）：

```bash
git clone git@github.com:licyk/temp.git && cd temp
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## 快速开始

```bash
hanatsumi fetch       # 全量抓取：约 1837 页 / 31 分钟，随时 Ctrl-C，重跑同命令续传
hanatsumi status      # 看断点与 CSV 概况
hanatsumi update      # 之后每次同步新增的 tag（秒级）
hanatsumi refresh     # 让 post_count / 改名 / 废弃 / 删除也同步进来（约 1 小时）
```

`hanatsumi` 装好后是独立命令；也可以用 `python -m hanatsumi fetch`。

## 命令行

```text
hanatsumi
├── fetch    [--out --state --delay --page-size --timeout --max-retries --max-pages -q]
├── update   [同上]                      增量抓取上次之后新增的 tag（需先全量完成）
├── refresh  [同上]                      全量重扫到暂存文件，成功后原子替换
├── status   [--out --state]             查看断点状态与 CSV 概况
├── verify   [--out --state --dups --init-state]   校验 CSV 与状态文件
├── reset    [--out --state --yes]       删除 CSV、状态文件与暂存文件
└── version                              显示版本与运行环境
```

抓取类命令（`fetch` / `update` / `refresh`）共用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--out` | `data/tags.csv` | CSV 路径（状态文件默认同名 `.state.json`） |
| `--state` | 由 `--out` 推导 | 显式指定状态文件 |
| `--delay` | `1.0` | 请求最小间隔（秒），匿名建议 ≥ 1.0 |
| `--page-size` | `1000` | 单页条数（1~1000，API 上限） |
| `--timeout` | `30.0` | 单次请求超时（秒） |
| `--max-retries` | `8` | 失败重试次数 |
| `--max-pages` | `0` | 本次最多抓几页，`0`=不限（调试用） |
| `-q` / `--quiet` | 关 | 不打印逐页进度 |

输出约定：**逐页进度与日志在 stderr**（不污染管道，日志等级带颜色），
`status` / `verify` / `version` 的表格在 stdout；`--debug` 在任何层级都可用。

```text
[0:12] pages=13 rows=13,000 rate=1105/s cursor=2730104 eta=41:20   ← stderr，原地刷新
INFO  完成（max_pages）：pages=13 fetched=13,000 written=13,000 …  ← stderr，日志
INFO  状态：rows=13,000 range=[2730104, 2743978] complete=False
INFO  CSV：data/tags.csv
```

## 输出文件

| 文件 | 内容 |
|---|---|
| `data/tags.csv` | UTF-8（新建时写 BOM，Excel 可直接打开），8 列：`id, name, post_count, category, is_deprecated, created_at, updated_at, words` |
| `data/tags.state.json` | 断点状态：`min_id` 续传游标、`max_id` 增量下界、`rows` 行数、`complete` 是否全量完成 |
| `data/tags.new.csv` / `.state.json` | `refresh` 的暂存文件，只在刷新期间存在，`reset --yes` 一并清理 |

## 数据源更新了怎么办

| 你要同步的变化 | 命令 | 耗时 |
|---|---|---|
| 新增 tag | `hanatsumi update` | 秒级 |
| `post_count` 变化、改名、废弃、tag 被删除 | `hanatsumi refresh` | 约 1 小时 |

`update` 只追加新 id，无法发现已有行的字段变化（Danbooru 不支持按更新时间过滤，
见 [docs/api.md](docs/api.md)），所以需要准确 `post_count` 时跑 `refresh`；
推荐节奏：**每天 `update`，每周 `refresh`**。刷新完成后再跑一次 `update` 可补齐刷新期间新建的 tag。

## 故障恢复

| 情况 | 处理 |
|---|---|
| 中途 Ctrl-C / 断网 | 直接重跑 `hanatsumi fetch`（自动续传） |
| 状态文件丢失 / 损坏 | `hanatsumi verify --init-state` 后再 `fetch`（从 CSV 最小 id 续扫到底） |
| 疑似数据不一致 | `hanatsumi verify --dups`（行数 / id 范围 / 坏行 / 重复 id 全面对账） |
| `refresh` 中断 | 重跑 `hanatsumi refresh`（续跑暂存文件，正式文件不受影响） |
| `refresh` 暂存状态丢失 | 重跑 `refresh` 会按 `tags.new.csv` 自动重建状态并续跑 |
| 从头再来 | `hanatsumi reset --yes`（连暂存文件一起清理） |

## 开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

python -m pytest -q                      # 30 个离线用例
python -m ruff check hanatsumi tests     # lint
python -m ruff format --check hanatsumi tests
python -m ty check hanatsumi             # 类型检查（自动发现 .venv）
```

## 许可证

GPL-3.0
