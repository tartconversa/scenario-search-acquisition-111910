"""周期采集引擎。

关键约束的落地位置：
- 每个单位先拉 /facets 取得版本与合法筛选，查询范围只来自 facets 声明；
- 同单位所有 /catalog 请求钉住首轮看到的版本；中途收到 409 立即停止该单位，
  本轮该单位作废，旧页不与新页拼接，其他单位继续；
- 429 只阻塞对应单位：Retry-After 落盘为 blocked_until，同轮不立即重试，
  其他单位照常采集；
- 断点续采按“上一轮成功单位运行 + 版本一致 + scope 完整”复用已保存原文，
  不是记一个裸页码；版本变化则整单位重采；
- 每条观察都保存 query_id 与原文摘要，保证可追查观察路径。
"""
from __future__ import annotations

import json
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .api import CatalogPage, SourceClient, UnitQuotaExceeded, UnsupportedFilter, VersionExpired
from .coverage import Scope, ScopeResult, UnitCoverage, plan_scopes
from .storage import Store


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]


class CollectionEngine:
    def __init__(
        self,
        client: SourceClient,
        store: Store,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client
        self.store = store
        self.clock = clock
        self.sleep = sleep
        self._facets: Optional[dict[str, Any]] = None

    # ---- 来源元数据 ----------------------------------------------------
    def facets(self, force: bool = False) -> dict[str, Any]:
        if self._facets is None or force:
            self._facets = self.client.facets()
        return self._facets

    def units(self, requested: Optional[list[str]] = None) -> list[str]:
        """以来源 /facets 中的单位目录为准；请求里的单位必须真实存在。"""
        declared = list(self.facets()["units"].keys())
        if not requested:
            return declared
        unknown = [u for u in requested if u not in declared]
        if unknown:
            raise ValueError(f"单位不在来源完整目录中: {unknown}；合法单位: {declared}")
        return requested

    # ---- 单范围分页 ----------------------------------------------------
    def _fetch_scope(
        self,
        run_id: str,
        scope: Scope,
        version: int,
        prior_pages: dict[int, dict[str, Any]],
    ) -> ScopeResult:
        """把一个范围的可取页全部拉完，返回 ScopeResult。

        prior_pages: 断点续采时来自上一成功轮次、同版本的已保存页 {page: row字段}。
        """
        page = 0
        pages_fetched = 0
        items: list[dict[str, Any]] = []
        cap = page_size = 0
        declared_page_size = int(self.facets()["page_size"])
        total = 0
        error = error_detail = None
        while True:
            next_page_flag: Optional[bool] = None
            prior = prior_pages.get(page)
            if prior is not None:
                # 续采：复用的是已落盘原文，凭据是版本一致而非页码记忆
                total = prior["total"]
                cap = prior["cap"]
                page_size = declared_page_size
                page_items = prior["items"]
                digest = prior["digest"]
                next_page_flag = bool(prior["next_page"])
                query_id = self.store.save_query(
                    run_id, scope.unit, scope.industry, scope.year, page, version, "reused",
                    total=total, cap=cap, truncated=bool(prior["truncated"]),
                    next_page=next_page_flag, scope_status="reused",
                    response_digest=digest,
                )
                for item in page_items:
                    self.store.save_observation(scope.unit, item["id"], query_id, run_id, digest, item)
            else:
                try:
                    cp: CatalogPage = self.client.catalog_page(
                        scope.unit,
                        industry=scope.industry,
                        year=scope.year,
                        page=page,
                        version=version,
                    )
                except VersionExpired as exc:
                    error, error_detail = "version_expired", str(exc.payload)
                    self.store.save_query(
                        run_id, scope.unit, scope.industry, scope.year, page, version, "version_expired",
                        error_code="version_expired",
                    )
                    break
                except UnitQuotaExceeded as exc:
                    error, error_detail = "unit_quota", str(exc.payload)
                    blocked_until = self.clock() + max(exc.retry_after, 0.0)
                    self.store.set_blocked_until(scope.unit, blocked_until)
                    self.store.save_query(
                        run_id, scope.unit, scope.industry, scope.year, page, version, "unit_quota",
                        error_code="unit_quota", retry_after=exc.retry_after,
                    )
                    break
                except UnsupportedFilter as exc:  # 契约保护：规划器不应触发
                    error, error_detail = "unsupported_filter", str(exc.payload)
                    self.store.save_query(
                        run_id, scope.unit, scope.industry, scope.year, page, version, "unsupported_filter",
                        error_code="unsupported_filter",
                    )
                    break
                # 原文先落盘，再记查询与观察
                url = self.client.base_url + "/catalog"
                self.store.save_raw(url, 200, cp.raw, cp.digest)
                page_size = declared_page_size
                cap = cp.cap
                total = cp.total
                digest = cp.digest
                page_items = cp.items
                next_page_flag = cp.next
                query_id = self.store.save_query(
                    run_id, scope.unit, scope.industry, scope.year, page, version, "ok",
                    total=cp.total, cap=cp.cap, truncated=cp.truncated, next_page=cp.next,
                    response_digest=digest,
                )
                for item in page_items:
                    self.store.save_observation(scope.unit, item["id"], query_id, run_id, digest, item)

            items.extend(page_items)
            pages_fetched += 1
            # 来源分页语义：next=false 只说明 cap 内的页取完，截断由 truncated/total 判定
            if not next_page_flag:
                break
            page += 1

        return ScopeResult(
            scope=scope,
            total=total,
            observed_items=items,
            pages_fetched=pages_fetched,
            page_size=page_size or declared_page_size,
            cap=cap or self.facets()["cap"],
            error=error,
            error_detail=error_detail,
            version=version,
        )

    # ---- 单单位整轮 ----------------------------------------------------
    def run_unit(self, run_id: str, unit: str, *, resumable: bool = True) -> UnitCoverage:
        facets = self.facets()
        planned_version = facets["units"][unit]
        self.store.start_unit_run(run_id, unit, planned_version)

        blocked_until = self.store.get_blocked_until(unit)
        if blocked_until > self.clock():
            reason = f"单位 {unit} 处于配额冷却期，需等待至 {blocked_until:.0f}（相对当前 {blocked_until - self.clock():.1f}s）"
            self.store.finish_unit_run(run_id, unit, "throttled", reason, planned_version, blocked_until)
            return UnitCoverage(unit, planned_version, [])

        scopes = plan_scopes(facets, unit)
        prior_index, prior_version = self._resume_index(unit, resumable)

        version = planned_version
        results: list[ScopeResult] = []
        for scope in scopes:
            prior_pages = prior_index.get(self._scope_key(scope), {}) if prior_version == version else {}
            result = self._fetch_scope(run_id, scope, version, prior_pages)
            results.append(result)
            if result.error == "version_expired":
                # 版本凭据过期：立即停止本单位，已取旧页保留原文但不参与完整结论
                self.store.save_scope_coverage(run_id, unit, [r.as_row() for r in results])
                cov = UnitCoverage(unit, version, results)
                verdict, reason = cov.verdict()
                self.store.finish_unit_run(run_id, unit, verdict, reason, version)
                return cov
            if result.error == "unit_quota":
                # 单位配额：停止本单位，等待 Retry-After；其他单位不受影响
                self.store.save_scope_coverage(run_id, unit, [r.as_row() for r in results])
                cov = UnitCoverage(unit, version, results)
                verdict, reason = cov.verdict()
                self.store.finish_unit_run(
                    run_id, unit, verdict, reason, version, self.store.get_blocked_until(unit)
                )
                return cov

        coverage = UnitCoverage(unit, version, results)
        self.store.save_scope_coverage(run_id, unit, [r.as_row() for r in coverage.results])
        verdict, reason = coverage.verdict()
        self.store.clear_blocked(unit)
        self.store.finish_unit_run(run_id, unit, verdict, reason, version)
        return coverage

    def _resume_index(self, unit: str, resumable: bool) -> tuple[dict[str, dict[int, dict[str, Any]]], Optional[int]]:
        """找到上一成功单位运行；版本一致才允许复用其已保存原文。"""
        if not resumable:
            return {}, None
        prior = self.store.latest_finished_unit_run(unit)
        if prior is None or prior["verdict"] not in ("complete", "gap"):
            return {}, None
        prior_version = prior["observed_version"]
        index: dict[str, dict[int, dict[str, Any]]] = {}
        # 仅复用完整取满、无错误的范围；欠采范围重新拉取
        for q in self.store.successful_queries(prior["run_id"], unit):
            if q["status"] != "ok" or q["version"] != prior_version:
                continue
            key = self._scope_key(Scope(unit, q["industry"], q["year"], "atomic"))
            page_items = self._items_from_digest(q["response_digest"])
            index.setdefault(key, {})[q["page"]] = {
                "total": q["total"], "cap": q["cap"], "truncated": bool(q["truncated"]),
                "next_page": bool(q["next_page"]), "digest": q["response_digest"], "items": page_items,
            }
        return index, prior_version

    def _items_from_digest(self, digest: str) -> list[dict[str, Any]]:
        row = self.store.conn.execute(
            "SELECT body FROM raw_response WHERE response_digest=?", (digest,)
        ).fetchone()
        if row is None:
            return []
        return json.loads(row["body"]).get("items", [])

    @staticmethod
    def _scope_key(scope: Scope) -> str:
        return f"{scope.industry or '*'}|{scope.year if scope.year is not None else '*'}"

    # ---- 整轮调度 ------------------------------------------------------
    def run_cycle(
        self,
        *,
        run_id: Optional[str] = None,
        units: Optional[list[str]] = None,
        trigger: str = "cycle",
        note: Optional[str] = None,
        resumable: bool = True,
    ) -> dict[str, Any]:
        run_id = run_id or new_run_id()
        self.store.start_run(run_id, trigger, note)
        self.facets(force=True)
        unit_names = self.units(units)
        coverages: "OrderedDict[str, UnitCoverage]" = OrderedDict()
        for unit in unit_names:
            # 每个单位独立 try：一个单位的网络异常不能拖垮其他单位
            try:
                coverages[unit] = self.run_unit(run_id, unit, resumable=resumable)
            except Exception as exc:  # pragma: no cover - 防御性兜底
                self.store.finish_unit_run(run_id, unit, "failed", f"未预期异常: {exc!r}", None)
                coverages[unit] = UnitCoverage(unit, None, [])
        self.store.finish_run(run_id)
        return self.run_summary(run_id, coverages)

    def run_summary(self, run_id: str, coverages: "OrderedDict[str, UnitCoverage]") -> dict[str, Any]:
        unit_summaries = {}
        for unit, cov in coverages.items():
            verdict, reason = cov.verdict() if cov.results else (
                self.store.get_unit_run(run_id, unit)["verdict"],
                self.store.get_unit_run(run_id, unit)["reason"],
            )
            unit_summaries[unit] = {
                "verdict": verdict,
                "reason": reason,
                "version": cov.version,
                "observed_unique": len(cov.unique_observed),
                "scope_count": len(cov.results),
                "gap_scopes": [r.scope.label for r in cov.gap_scopes],
            }
        overall = (
            "complete"
            if all(u["verdict"] == "complete" for u in unit_summaries.values()) and unit_summaries
            else "gap_or_blocked"
        )
        return {"run_id": run_id, "overall": overall, "units": unit_summaries}
