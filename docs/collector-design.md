# 场景目录采集客户端设计说明

本客户端面向 `docs/source-contract.md` 描述的合成来源，解决“检索页前一百条不等于完整
名单”的问题：**只交付覆盖可证明的需求，覆盖不成立时如实报缺口，绝不谎报全量。**
仅依赖 Python 3.11 标准库（`urllib` + `sqlite3`），无第三方运行时依赖。

## 1. 模块与职责

| 模块 | 职责 |
| --- | --- |
| `collector/api.py` | HTTP 客户端。区分 200 / 400 / 409 / 429，分别抛 `UnsupportedFilter`、`VersionExpired`、`UnitQuotaExceeded`，429 携带 `Retry-After`，409 携带来源当前版本 |
| `collector/coverage.py` | 只用 `/facets` 声明的合法筛选规划范围；按 `(unit,id)` 归并去重；计算每个范围与每个单位的覆盖结论和缺口依据 |
| `collector/engine.py` | 分页采集、版本钉住、单位级限速避让、断点续采、按单位独立调度 |
| `collector/storage.py` | SQLite 持久层。来源原文（`raw_response`）与归并身份（`requirement`/`observation`）分表 |
| `collector/lifecycle.py` | 需求状态归并（open/new/withdrawn/removed/unseen_gap）、修订与分类变化留痕、经理名单更新 |
| `collector/cli.py` | 四个可运行流程 + 观察路径追查 |

## 2. 覆盖如何被“证明”，而不是假设

1. **范围只来自来源声明。** `plan_scopes` 读取 `/facets` 的 `industries`、`years`、
   `filters`，穷举 `industry × year` 原子范围，并加一个单位全目录范围做总量交叉验证。
   规划器永远不产生 `prefix` 之类来源未声明的条件（来源对自造条件回 400，测试固定该行为）。
2. **截断按来源语义判定。** 一次查询最多返回 `cap=4` 条、页大小 `page_size=2`。
   `next=false` 只表示 cap 内的页取完；是否截断由 `total > cap` 判定。
   不可再分的 `(industry, year)` 仍 `total>cap` 时，`gap_count=total-observed` 如实记账，
   单位结论只能是 `gap`，退出码 2，不允许报全量完成。
3. **交叉分类靠归并去重。** 同一需求出现在多个行业/年份范围时，`UnitCoverage.unique_observed`
   以 `(unit, id)` 为身份键去重；编号仅单位内唯一，跨单位同编号天然是两条身份，互不覆盖。
4. **总量交叉验证。** 单位全目录范围的 `total` 与归并后的唯一身份数不一致时同样判 `gap`。

当前合成样本下，各行业范围分别截断后取并集，每单位可观察 id 0–7；id=8 在其所有匹配
范围都落在 cap 之外，属于结构性缺口，`gaps` 命令可逐条列出依据。

## 3. 版本与限速的不同后果

- **409 `version_expired`（版本凭据过期）。** 同单位所有请求钉住首轮看到的版本；中途收到
  409 立即停止该单位，已取旧页保留原文但不参与完整结论，单位判 `version_conflict`。
  其他单位不受影响。断点续采时若来源版本已推进，**整单位重采、零页复用**。
- **429 `unit_quota`（单位共享配额）。** 只阻塞命中的单位：`Retry-After` 换算为绝对时间
  写入 `kv` 表 `blocked:<unit>`，同轮不立即重试；冷却期内再次调度直接读落盘时间跳过，
  不发任何 catalog 请求、不消耗配额；其他单位照常采集。限速状态跨进程持久，中断后新进程
  仍会遵守。

## 4. 断点续采核对的是版本与覆盖依据，不是页码

`resume` / 下一轮 `cycle` 先找该单位上一 `complete/gap` 的成功单位运行：

- 该轮 `observed_version` 与来源当前版本**一致**，且对应范围无错误，才复用已落盘原文，
  查询标记 `reused` 并重新登记 observation（可追溯到新 run）；
- 版本不一致则该单位全部重采；欠采范围不满足复用条件，重新拉取。

系统不保存“下次从第几页开始”这类脱离版本的裸页码。

## 5. 三种“看不到”产生三种状态

| 情况 | 判定条件 | 需求状态 |
| --- | --- | --- |
| 明确撤回 | 条目 `status=withdrawn` 或 `/revisions` 出现 withdraw 事件 | `withdrawn`：保留库与历史，踢出可联系名单 |
| 完整目录缺席 | 所有相关原子范围都完整（存在、无错误、无截断、取满）仍观察不到 | `removed`：区别于撤回 |
| 欠采未见 | 相关范围存在截断/限速/版本冲突，或本轮根本没采到 | `unseen_gap`：**保留身份，禁止撤下**，待续采 |

分类移动（move）只改行业关系：身份 `(unit,id)` 不变、修订号推进、`requirement_change`
记录 `moved`，不会因从某行业消失而删除整条需求。补登旧需求（backdate，year=2024）由每轮
全量重扫发现，不依赖“最后更新时间”增量。

## 6. 项目经理名单

- `selection_snapshot` 保存经理已筛选的 `(unit,id,observed_revision)`，`INSERT OR IGNORE`
  保证后续轮次永不覆盖其原始修订；
- 每次 `update-list` 生成 `list_snapshot`：开放类状态（open/new）可联系；withdrawn/removed/
  unseen_gap 不可联系并附原因。撤回项不再被当成开放项目，但经理解释所需的历史修订仍可查。

## 7. 持久化与可追溯

- `raw_response` 以响应体 SHA256 为主键，同版本重复采集自动去重、原文不可变；
- `query_run` 记录每次查询的范围、页、版本、total/cap/truncated/next、状态与原文摘要；
- `observation` 关联需求与具体 query，`observe --unit X --id Y` 可列出该需求被哪些查询、
  在哪个版本、通过哪份原文观察到；
- `scope_coverage` 落盘每个范围的覆盖依据文案，`gaps` 直接回答“本轮覆盖为什么成立/不成立”。

## 8. 命令行流程

```bash
# 周期采集（全部单位；--unit 可重复；--watch 持续，限速感知避让）
python -m collector.cli --db ./data/collector.db cycle
# 按单位续采（版本核对后复用原文）
python -m collector.cli --db ./data/collector.db resume --unit west
# 覆盖缺口查询（可按 run / 单位过滤；完整时退出码 0，有缺口/阻塞时 2）
python -m collector.cli --db ./data/collector.db gaps --unit east
# 名单更新（首次可带 --request 导入经理筛选快照）
python -m collector.cli --db ./data/collector.db update-list --request inputs/request.json
# 追查单条需求的全部观察路径
python -m collector.cli --db ./data/collector.db observe --unit east --id 0
```

## 9. 测试

`tests/test_collector.py` 全部通过真实 HTTP（启动 `source.app` 的线程内服务器）验证，
覆盖：截断报缺口、交叉去重、跨单位同编号隔离、429 单位隔离与冷却不重试、409 旧页不拼接、
续采版本核对、move/withdraw/remove/backdate 的状态分化、原文持久化与去重、非法筛选拒绝、
未知单位拒绝。运行方式（隔离模式，路径显式注入）：

```bash
python -I -c "import sys,unittest; sys.path.insert(0,'.'); \
unittest.TextTestRunner().run(unittest.TestLoader().discover('tests', top_level_dir='.'))"
```
