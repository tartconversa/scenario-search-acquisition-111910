"""需求生命周期与项目经理名单。

三类“看不到”必须分开（对应契约要求）：
1. withdrawn 明确撤回：来源条目 status=withdrawn，或 /revisions 出现 withdraw 事件；
   需求保留在库里但不再是开放项目，当前可联系名单必须剔除；
2. removed 完整目录缺席：上一轮已知、本轮在所有相关原子范围都完整（无截断、
   无错误、取满）的情况下仍观察不到，才认定从完整单位目录缺席；
3. unseen_gap 欠采未见：相关范围存在截断/限速/版本冲突，缺席不能下定论，
   保留原身份并标明“未见但覆盖不成立”，绝不能直接撤下。

分类移动（move）只改行业关系：需求身份 (unit,id) 不变，记录 change，
不能因为从某个行业消失就删除整条需求。同编号跨单位在 requirement 表中
天然是两行，互不覆盖。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .coverage import UnitCoverage
from .storage import Store

OPEN = "open"
WITHDRAWN = "withdrawn"
REMOVED = "removed"
UNSEEN_GAP = "unseen_gap"
NEW = "new"


def reconcile_unit(
    store: Store,
    run_id: str,
    coverage: UnitCoverage,
    *,
    revision_events: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """根据本轮单位覆盖结果，把观察归并进 requirement 表并产出状态结论。"""
    unit = coverage.unit
    events = [e for e in (revision_events or []) if e.get("unit") == unit]
    withdrawn_ids = {
        e.get("id") for e in events
        if e.get("event") == "withdraw" or e.get("status") == "withdrawn"
    }
    observed = coverage.unique_observed  # id -> 最新一条 item（dict 去重保留首次出现）
    known = {row["id"]: row for row in store.requirements_of_unit(unit)}
    # 从 coverage 的原子范围取来源声明的行业集合
    facet_industries = sorted({
        r.scope.industry for r in coverage.results if r.scope.role == "atomic" and r.scope.industry
    })

    stats = {"new": 0, "open": 0, "withdrawn": 0, "removed": 0, "unseen_gap": 0, "moved": 0, "updated": 0}

    # 1) 本轮观察到的条目：归并身份、检测修订与分类变化
    for item_id, item in observed.items():
        existing = known.get(item_id)
        if existing is None:
            state = NEW
            reason = f"本轮 {run_id} 首次观察到"
            store.upsert_requirement(run_id, unit, item_id, item, state, reason)
            store.record_change(run_id, unit, item_id, "new", reason)
            stats["new"] += 1
            continue

        prev_industries = json.loads(existing["industries_json"])
        prev_revision = existing["latest_revision"]
        new_industries = item.get("industries", [])
        changes = []
        if prev_industries != new_industries:
            changes.append(f"行业关系变化 {prev_industries} -> {new_industries}（仅分类移动，身份不变）")
            store.record_change(
                run_id, unit, item_id, "moved",
                f"industries: {prev_industries} -> {new_industries}",
            )
            stats["moved"] += 1
        if prev_revision != int(item.get("revision", 0)):
            changes.append(f"修订号 {prev_revision} -> {item.get('revision')}")
            stats["updated"] += 1

        if item.get("status") == "withdrawn" or item_id in withdrawn_ids:
            state = WITHDRAWN
            reason = "来源明确标记撤回（status=withdrawn / revisions withdraw 事件）"
            # 仅在状态首次转为撤回时留痕，避免每轮重复变更记录
            if existing["status"] != WITHDRAWN:
                store.record_change(run_id, unit, item_id, "withdrawn", reason)
            stats["withdrawn"] += 1
        else:
            # 首次观察轮记为 new，之后仍开放即转 open
            state = OPEN
            if existing["status"] == NEW:
                reason = f"首轮 new 之后持续可观察，转为开放（{run_id}）"
            else:
                reason = "；".join(changes) if changes else f"本轮 {run_id} 仍在完整目录中观察到"
            stats["open"] += 1
        store.upsert_requirement(run_id, unit, item_id, item, state, reason)

    # 2) 本轮未观察到的已知条目：撤回事实黏滞，其余区分完整缺席与欠采未见
    for item_id, row in known.items():
        if item_id in observed:
            continue
        # 明确撤回是终态事实：即使本轮因截断/欠采未再看到，也不能被改写为其他状态
        if row["status"] == WITHDRAWN:
            store.mark_requirement_state(
                unit, item_id, run_id, WITHDRAWN,
                "撤回状态黏滞：本轮未观察到不改变已确认的撤回事实",
            )
            stats["withdrawn"] += 1
            continue
        prev_industries = json.loads(row["industries_json"])
        prev_year = row["year"]
        conclusive = bool(facet_industries) and coverage.scope_conclusively_complete(
            [i for i in prev_industries if i in facet_industries] or facet_industries,
            prev_year,
            facet_industries,
        )
        if not conclusive:
            store.mark_requirement_state(
                unit, item_id, run_id, UNSEEN_GAP,
                "相关原子范围存在截断/限速/版本冲突，未观察不能认定撤下，保留身份待续采",
            )
            store.record_change(run_id, unit, item_id, "unseen_gap", "覆盖不成立下的未见，身份保留")
            stats["unseen_gap"] += 1
        else:
            store.mark_requirement_state(
                unit, item_id, run_id, REMOVED,
                "所有相关原子范围完整取满仍缺席，认定从完整单位目录移除（非分类退出）",
            )
            store.record_change(run_id, unit, item_id, "removed", "完整目录缺席，区别于撤回与欠采未见")
            stats["removed"] += 1

    return {"unit": unit, "stats": stats, "observed_unique": len(observed)}


def reconcile_all(
    store: Store,
    run_id: str,
    coverages: dict[str, UnitCoverage],
    *,
    revision_events: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    # 注意：限速/版本冲突单位可能 results 为空，仍需归并（已知需求转 unseen_gap），不能跳过
    return [
        reconcile_unit(store, run_id, cov, revision_events=revision_events)
        for cov in coverages.values()
    ]


# ---- 项目经理名单 -------------------------------------------------------
def update_list(store: Store, run_id: str) -> dict[str, Any]:
    """生成当前可联系名单，并对已保存筛选名单保留原始修订快照。"""
    saved = store.selection_snapshot()
    requirements = {(r["unit"], r["id"]): r for r in store.all_requirements()}
    rows: list[dict[str, Any]] = []
    saved_report: list[dict[str, Any]] = []

    for s in saved:
        key = (s["unit"], s["id"])
        req = requirements.get(key)
        if req is None:
            state, contactable = REMOVED, False
            detail = "保存名单中的需求已不在归并库（见 requirement_change 审计）"
        else:
            state = req["status"]
            contactable = state in (OPEN, NEW)
            reason = req["state_reason"] or ""
            if state == WITHDRAWN:
                detail = f"已撤回，不再作为开放项目；保存修订仍为 {s['observed_revision']}。{reason}"
            elif state == REMOVED:
                detail = f"完整目录缺席；保存修订仍为 {s['observed_revision']}。{reason}"
            elif state == UNSEEN_GAP:
                detail = f"欠采未见，覆盖不成立，暂不可联系但不撤下；保存修订 {s['observed_revision']}。{reason}"
            else:
                detail = f"开放可联系；保存修订 {s['observed_revision']}，当前修订 {req['latest_revision']}"
        saved_report.append({
            "unit": s["unit"], "id": s["id"], "state": state,
            "contactable": contactable, "detail": detail,
            "saved_observed_revision": s["observed_revision"],
        })
        rows.append({"unit": s["unit"], "id": s["id"], "state": state,
                     "contactable": contactable, "detail": detail})

    # 当前可联系名单：所有开放（open/new）需求（含未被经理保存的新需求）
    contactable = [
        {"unit": r["unit"], "id": r["id"], "state": r["status"],
         "contactable": r["status"] in (OPEN, NEW), "detail": r["state_reason"] or ""}
        for r in store.all_requirements()
    ]
    for r in contactable:
        if not any(x["unit"] == r["unit"] and x["id"] == r["id"] for x in rows):
            rows.append(r)

    store.save_list_snapshot(run_id, rows)
    return {
        "run_id": run_id,
        "saved_selection": saved_report,
        "contactable_count": sum(1 for r in rows if r["contactable"]),
        "excluded": [
            {"unit": r["unit"], "id": r["id"], "state": r["state"], "detail": r["detail"]}
            for r in rows if not r["contactable"]
        ],
    }


def load_selection(store: Store, request_payload: dict[str, Any]) -> int:
    """从项目可读输入（inputs/request.json）导入经理已保存筛选名单，快照不可变。"""
    entries = request_payload.get("saved_selection", [])
    return store.save_selection_snapshot(entries)
