"""授权场景目录采集客户端。

模块划分：
- api:       来源 HTTP 客户端，区分 200/400/409/429 等真实协议后果
- storage:   SQLite 持久层，来源原文与归并身份分表保存
- coverage:  仅基于来源 /facets 声明的合法筛选规划原子范围、计算覆盖缺口
- engine:    按单位周期采集、限速避让、版本校验与断点续采
- lifecycle: 需求状态归并、修订留痕与项目经理名单更新
- cli:       命令行入口（周期采集 / 按单位续采 / 覆盖缺口 / 名单更新）
"""

__all__ = ["api", "storage", "coverage", "engine", "lifecycle", "cli"]
