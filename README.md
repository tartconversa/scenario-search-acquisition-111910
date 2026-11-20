# 场景目录来源

这是本地生成的外部来源与独立接收端，仅用于提供数据、页面、协议与回执。仓库提供合成来源服务与配套的授权采集客户端（`collector/`），所有单位、厂商、研究记录和门户内容均为虚构数据，不连接真实业务系统。

## 启动

安装Docker后，在项目根目录运行 docker compose up --build，服务默认监听 http://127.0.0.1:8765。若直接在主机运行，Python 3.11+执行 pip install -r requirements.txt，再运行 python -m source.app --port 8765。浏览器任务通过Playwright 1.56.0提供的Chromium测试来源页面。

输入示例位于 inputs/request.json；业务字段、页面与协议边界见 docs/source-contract.md。示例是项目可读输入格式，原件与来源修订均由演练服务产生。

## 采集客户端

`collector/` 是仅用 Python 标准库实现的授权采集端，设计与约束映射见 docs/collector-design.md。来源服务启动后，四个可运行流程：

- 周期采集：`python -m collector.cli --db ./data/collector.db cycle`（`--watch` 持续、`--unit` 指定单位、`--no-resume` 强制重采）
- 按单位续采：`python -m collector.cli --db ./data/collector.db resume --unit west`
- 覆盖缺口查询：`python -m collector.cli --db ./data/collector.db gaps`（完整退出码 0，存在缺口/限速/版本冲突退出码 2）
- 名单更新：`python -m collector.cli --db ./data/collector.db update-list --request inputs/request.json`
- 观察路径追查：`python -m collector.cli --db ./data/collector.db observe --unit east --id 0`

采集端只使用来源 /facets 声明的合法筛选，按 (unit,id) 归并交叉分类、隔离跨单位同编号；409 版本过期与 429 单位配额区别处理；撤回、完整目录缺席、欠采未见产生 withdrawn/removed/unseen_gap 三种状态。来源原文与归并身份在 SQLite 中分表持久化。

## 来源说明

服务页面 / 简述当前来源。JSON与XML接口描述输入契约，页面展示来源交互；POST /__control 只用于本地演练人员切换来源修订、可用性和延迟场景。状态可用 --state /path/outside/source-state.json 选择性落盘，默认每次启动恢复初始样本。端点行为和合成记录见 source/app.py。

## 检查

运行 python -m unittest discover -s tests -v 检查来源端、原件、门户状态、合成接收端以及采集客户端（tests/test_collector.py 通过真实 HTTP 交互验证截断、交叉分类、限速隔离、版本冲突与目录变化）。涉及页面的数据源测试会启动真实Chromium。
