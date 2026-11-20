"""命令行入口：四个可运行流程。

python -m collector.cli cycle     [--unit east ...]   周期采集（可 --watch 持续）
python -m collector.cli resume    --unit west          按单位续采（版本核对后复用原文）
python -m collector.cli gaps      [--run ID] [--unit] 覆盖缺口查询
python -m collector.cli update-list --request inputs/request.json
                                      名单更新（撤回剔除、快照保留）
另有 observe 子命令追查单条需求的全部观察路径。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .api import SourceClient
from .engine import CollectionEngine, new_run_id
from .lifecycle import reconcile_all, update_list, load_selection
from .storage import Store


def _build(args: argparse.Namespace) -> tuple[SourceClient, Store, CollectionEngine]:
    client = SourceClient(args.base_url, timeout=args.timeout)
    store = Store(args.db)
    engine = CollectionEngine(client, store)
    return client, store, engine


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False))


def cmd_cycle(args: argparse.Namespace) -> int:
    client, store, engine = _build(args)
    try:
        if args.watch:
            return _watch(args, client, store, engine)
        summary = engine.run_cycle(units=args.unit or None, trigger="cycle",
                                   note=args.note, resumable=not args.no_resume)
        coverages = _coverages_from_run(store, summary["run_id"], engine)
        events = _safe_revisions(client)
        reconcile_all(store, summary["run_id"], coverages, revision_events=events)
        _print(summary)
        return 0 if summary["overall"] == "complete" else 2
    finally:
        store.close()


def _watch(args: argparse.Namespace, client: SourceClient, store: Store,
           engine: CollectionEngine) -> int:
    interval = args.interval
    print(f"持续采集模式：每 {interval}s 一轮，Ctrl+C 退出", file=sys.stderr)
    try:
        while True:
            run_id = new_run_id()
            summary = engine.run_cycle(run_id=run_id, units=args.unit or None,
                                       trigger="watch", resumable=not args.no_resume)
            coverages = _coverages_from_run(store, run_id, engine)
            reconcile_all(store, run_id, coverages, revision_events=_safe_revisions(client))
            _print(summary)
            # 限速感知：取所有单位 blocked_until 的最近时刻，避免无谓轮询
            now = time.time()
            next_due = max(
                [store.get_blocked_until(u) for u in summary["units"]] + [now]
            )
            time.sleep(max(interval, next_due - now))
    except KeyboardInterrupt:
        print("已停止持续采集", file=sys.stderr)
        return 0
    finally:
        store.close()


def cmd_resume(args: argparse.Namespace) -> int:
    client, store, engine = _build(args)
    try:
        run_id = new_run_id()
        store.start_run(run_id, "resume", f"按单位续采: {args.unit}")
        coverages = {}
        for unit in args.unit:
            coverages[unit] = engine.run_unit(run_id, unit, resumable=True)
        store.finish_run(run_id)
        reconcile_all(store, run_id, coverages, revision_events=_safe_revisions(client))
        summary = engine.run_summary(run_id, coverages)
        _print(summary)
        return 0 if summary["overall"] == "complete" else 2
    finally:
        store.close()


def cmd_gaps(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        run_id = args.run or _latest_run_id(store)
        if run_id is None:
            _print({"error": "尚无采集运行，请先执行 cycle"})
            return 1
        rows = store.scope_coverage(run_id, args.unit)
        unit_runs = {
            r["unit"]: r for r in store.conn.execute(
                "SELECT * FROM unit_run WHERE run_id=?", (run_id,)
            ).fetchall()
        }
        report: dict[str, Any] = {"run_id": run_id, "units": {}}
        for r in rows:
            u = r["unit"]
            bucket = report["units"].setdefault(u, {
                "verdict": unit_runs[u]["verdict"] if u in unit_runs else None,
                "reason": unit_runs[u]["reason"] if u in unit_runs else None,
                "version": unit_runs[u]["observed_version"] if u in unit_runs else None,
                "scopes": [],
            })
            bucket["scopes"].append({
                "scope": f"unit={u}" + (f"&industry={r['industry']}" if r["industry"] else "")
                         + (f"&year={r['year']}" if r["year"] is not None else ""),
                "total": r["total"], "observed": r["observed_count"],
                "truncated": bool(r["truncated"]), "gap_count": r["gap_count"],
                "pages_fetched": r["pages_fetched"], "basis": r["basis"],
            })
        for u, ur in unit_runs.items():
            report["units"].setdefault(u, {
                "verdict": ur["verdict"], "reason": ur["reason"],
                "version": ur["observed_version"], "scopes": [],
            })
        _print(report)
        has_gap = any(
            s["gap_count"] > 0 or u["verdict"] in ("throttled", "version_conflict", "failed", "gap")
            for u in report["units"].values() for s in u["scopes"]
        ) or any(u["verdict"] != "complete" for u in report["units"].values())
        return 0 if not has_gap else 2
    finally:
        store.close()


def cmd_update_list(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        if args.request:
            payload = json.loads(Path(args.request).read_text(encoding="utf-8"))
            n = load_selection(store, payload)
            print(f"已导入/保留项目经理筛选快照 {n} 条（既有快照不覆盖）", file=sys.stderr)
        run_id = args.run or _latest_run_id(store)
        if run_id is None:
            _print({"error": "尚无采集运行，请先执行 cycle"})
            return 1
        report = update_list(store, run_id)
        _print(report)
        return 0
    finally:
        store.close()


def cmd_observe(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        req = store.get_requirement(args.unit, args.id)
        if req is None:
            _print({"error": f"未找到需求 {args.unit}/{args.id}"})
            return 1
        observations = [dict(o) for o in store.observations_of(args.unit, args.id)]
        _print({
            "identity": {"unit": args.unit, "id": args.id},
            "requirement": dict(req),
            "observation_paths": observations,
            "observed_through_queries": len(observations),
        })
        return 0
    finally:
        store.close()


# ---- 辅助 ---------------------------------------------------------------
def _coverages_from_run(store: Store, run_id: str, engine: CollectionEngine):
    """从已落盘的 query_run 重建本轮 UnitCoverage，供生命周期归并使用。"""
    from .coverage import Scope, ScopeResult, UnitCoverage
    facets = engine.facets()
    coverages = {}
    units = {q["unit"] for q in store.query_runs(run_id)}
    for unit in units:
        scopes = {}
        version = None
        for q in store.query_runs(run_id, unit):
            version = q["version"]
            key = (q["industry"], q["year"])
            role = "directory" if q["industry"] is None and q["year"] is None else "atomic"
            scope = Scope(unit, q["industry"], q["year"], role)
            entry = scopes.setdefault(key, {
                "scope": scope, "items": [], "pages": 0, "total": q["total"] or 0,
                "cap": q["cap"] or facets["cap"], "error": None, "error_detail": None,
            })
            if q["status"] in ("unit_quota", "version_expired", "unsupported_filter"):
                entry["error"] = q["status"]
                entry["error_detail"] = q["error_code"]
            if q["response_digest"]:
                entry["items"].extend(engine._items_from_digest(q["response_digest"]))
            entry["pages"] += 1
        results = [
            ScopeResult(
                scope=e["scope"], total=e["total"], observed_items=e["items"],
                pages_fetched=e["pages"], page_size=int(facets["page_size"]), cap=e["cap"],
                error=e["error"], error_detail=e["error_detail"], version=version,
            )
            for e in scopes.values()
        ]
        coverages[unit] = UnitCoverage(unit, version, results)
    return coverages


def _safe_revisions(client: SourceClient) -> list[dict[str, Any]]:
    try:
        return client.revisions()
    except Exception:
        return []


def _latest_run_id(store: Store) -> Optional[str]:
    rows = store.runs()
    return rows[-1]["run_id"] if rows else None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="collector", description="授权场景目录采集客户端")
    p.add_argument("--base-url", default="http://127.0.0.1:8765")
    p.add_argument("--db", default="./collector-data/collector.db")
    p.add_argument("--timeout", type=float, default=10.0)
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("cycle", help="周期采集（全部或指定单位）")
    c.add_argument("--unit", action="append", help="可重复指定，缺省采集 facets 中全部单位")
    c.add_argument("--note")
    c.add_argument("--no-resume", action="store_true", help="禁止复用历史已保存原文")
    c.add_argument("--watch", action="store_true", help="持续周期采集")
    c.add_argument("--interval", type=int, default=60)
    c.set_defaults(func=cmd_cycle)

    r = sub.add_parser("resume", help="按单位续采（核对版本与覆盖依据后复用）")
    r.add_argument("--unit", action="append", required=True)
    r.set_defaults(func=cmd_resume)

    g = sub.add_parser("gaps", help="覆盖缺口查询")
    g.add_argument("--run")
    g.add_argument("--unit")
    g.set_defaults(func=cmd_gaps)

    u = sub.add_parser("update-list", help="名单更新与撤回剔除")
    u.add_argument("--request", help="inputs/request.json 路径，导入经理筛选快照")
    u.add_argument("--run")
    u.set_defaults(func=cmd_update_list)

    o = sub.add_parser("observe", help="追查单条需求的观察路径")
    o.add_argument("--unit", required=True)
    o.add_argument("--id", required=True)
    o.set_defaults(func=cmd_observe)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
