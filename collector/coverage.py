"""查询范围规划与覆盖判定。

规则（对应 docs/source-contract.md）：
- 合法筛选只有 unit/industry/year，规划器只输出这些组合，绝不自造前缀等条件；
- industry 之间互相重叠，同一需求会在多个行业范围重复出现，覆盖判定按
  (unit, id) 归并后计数，不能把重复出现当成多个项目；
- 编号仅在 unit 内唯一：身份键永远带 unit，两个单位同编号互不覆盖；
- 来源对一次查询最多返回 cap 条（页内再分页）。total 是该范围真实量，
  next=false 只代表 cap 内的页取完，不代表范围无截断；
- 一个不可再分的 (industry, year) 范围仍然 total>cap 时，gap_count 如实记账，
  单位结论只能是 gap，不得报全量完成。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Scope:
    unit: str
    industry: Optional[str]
    year: Optional[int]
    role: str  # 'atomic' = 不可再分的行业×年份范围；'directory' = 单位全目录

    @property
    def label(self) -> str:
        parts = [f"unit={self.unit}"]
        if self.industry is not None:
            parts.append(f"industry={self.industry}")
        if self.year is not None:
            parts.append(f"year={self.year}")
        return "&".join(parts)


def plan_scopes(facets: dict[str, Any], unit: str) -> list[Scope]:
    """根据来源 facets 声明的行业、年份穷举合法范围。

    行业×年份的每个组合都是来源支持的最细范围；额外保留单位全目录查询，
    用于取得单位真实总量做交叉验证。所有组合都只使用合法筛选键。
    """
    industries = list(facets["industries"])
    years = list(facets["years"])
    scopes = [
        Scope(unit, industry, year, "atomic")
        for industry in industries
        for year in years
    ]
    scopes.append(Scope(unit, None, None, "directory"))
    return scopes


@dataclass
class ScopeResult:
    scope: Scope
    total: int
    observed_items: list[dict[str, Any]]
    pages_fetched: int
    page_size: int
    cap: int
    error: Optional[str] = None  # 'unit_quota' / 'version_expired' / ...
    error_detail: Optional[str] = None
    version: Optional[int] = None

    @property
    def observed_count(self) -> int:
        return len(self.observed_items)

    @property
    def reachable(self) -> int:
        """该范围下来源最多愿意返回的条数（cap 截断）。"""
        return min(self.cap, self.total)

    @property
    def truncated(self) -> bool:
        return self.total > self.cap

    @property
    def complete(self) -> bool:
        """可分页部分是否已取满（错误时为 False；截断范围取满 cap 也算 True）。"""
        if self.error is not None:
            return False
        return self.observed_count == self.reachable

    @property
    def gap_count(self) -> int:
        """该范围无法通过此接口观察到的条数。"""
        if self.error is not None:
            return self.total - self.observed_count
        return max(0, self.total - self.observed_count)

    def as_row(self) -> dict[str, Any]:
        if self.error is not None:
            basis = f"查询失败({self.error})，仅观察到 {self.observed_count}/{self.total} 条"
        elif self.truncated:
            basis = (
                f"范围真实量 total={self.total} 超过 cap={self.cap}，"
                f"仅前 {self.observed_count} 条可分页，缺口 {self.gap_count} 条不可观察"
            )
        else:
            basis = f"total={self.total}≤cap={self.cap}，已取满 {self.observed_count} 页内条目"
        return {
            "industry": self.scope.industry,
            "year": self.scope.year,
            "total": self.total,
            "observed_count": self.observed_count,
            "complete": self.error is None and not self.truncated and self.observed_count == self.total,
            "gap_count": self.gap_count,
            "pages_fetched": self.pages_fetched,
            "truncated": self.truncated,
            "basis": basis,
        }


@dataclass
class UnitCoverage:
    unit: str
    version: Optional[int]
    results: list[ScopeResult]

    # ---- 结论 ----------------------------------------------------------
    @property
    def errors(self) -> list[ScopeResult]:
        return [r for r in self.results if r.error is not None]

    @property
    def unique_observed(self) -> dict[str, dict[str, Any]]:
        """按 (unit, id) 归并；同编号跨单位天然隔离，行业重叠自动去重。"""
        merged: dict[str, dict[str, Any]] = {}
        for r in self.results:
            for item in r.observed_items:
                merged.setdefault(item["id"], item)
        return merged

    @property
    def directory_result(self) -> Optional[ScopeResult]:
        return next((r for r in self.results if r.scope.role == "directory"), None)

    @property
    def gap_scopes(self) -> list[ScopeResult]:
        return [r for r in self.results if r.gap_count > 0]

    def verdict(self) -> tuple[str, str]:
        """返回 (verdict, reason)。verdict: complete/gap/throttled/version_conflict/failed。"""
        errors = self.errors
        if any(r.error == "version_expired" for r in errors):
            r = next(r for r in errors if r.error == "version_expired")
            return "version_conflict", f"单位 {self.unit} 版本凭据过期：{r.error_detail or '来源版本已推进，旧页不能与新页拼接'}"
        if any(r.error == "unit_quota" for r in errors):
            r = next(r for r in errors if r.error == "unit_quota")
            return "throttled", f"单位 {self.unit} 触发共享配额限速：{r.error_detail or 'Retry-After 冷却后再续采'}"
        if errors:
            r = errors[0]
            return "failed", f"范围 {r.scope.label} 查询失败：{r.error} {r.error_detail or ''}".strip()
        if self.gap_scopes:
            details = "; ".join(
                f"{r.scope.label} 缺 {r.gap_count} 条(total={r.total},cap={r.cap})" for r in self.gap_scopes
            )
            return "gap", f"存在不可再分但仍超上限的范围，覆盖不完整：{details}"
        # 与单位全目录总量交叉验证
        d = self.directory_result
        if d is not None and d.total != len(self.unique_observed):
            return "gap", (
                f"单位全目录 total={d.total} 与归并观察到 {len(self.unique_observed)} 条不一致，"
                "存在未观察条目"
            )
        return "complete", f"全部 {len(self.results)} 个合法范围取满，归并后 {len(self.unique_observed)} 条与目录总量一致"

    def scope_conclusively_complete(self, industries: list[str], year: Optional[int],
                                    all_industries: list[str]) -> bool:
        """判断一条已知需求（按其最近已知行业/年份）缺席是否可下定论。

        只有它可能落入的每个原子范围都真实存在、且完整（无截断、无错误、
        取满）时，缺席才是“完整目录中的缺席”；相关范围欠采或本轮根本没采到，
        缺席只能记为未见。
        """
        relevant = [
            r for r in self.results
            if r.scope.role == "atomic"
            and r.scope.industry in (industries or all_industries)
            and (year is None or r.scope.year == year)
        ]
        if not relevant:
            return False
        return all(
            r.error is None and not r.truncated and r.observed_count == r.total
            for r in relevant
        )
