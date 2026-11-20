"""采集客户端端到端测试：全部通过真实 HTTP 与合成来源交互。

验证重点（对应任务验收语义）：
- 截断：不可再分范围仍 total>cap 时必须报缺口，不能谎称全量；
- 交叉分类：同需求多行业只归并一个身份；同编号跨单位互不覆盖；
- 限速：429 只阻塞对应单位，其他单位继续，且不立即反复重试；
- 版本：409 后旧页不与新页拼接，续采必须重新核对版本；
- 目录变化：move/withdraw/remove/backdate 产生不同的需求状态后果；
- 持久化：原文与归并身份分表，观察路径可逐条追查。
"""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from source.app import Site
from source.common import server

from collector.api import SourceClient, UnitQuotaExceeded, VersionExpired
from collector.engine import CollectionEngine
from collector.lifecycle import (
    NEW, OPEN, REMOVED, UNSEEN_GAP, WITHDRAWN, reconcile_all, update_list, load_selection,
)
from collector.storage import Store


class CollectorHttpTest(unittest.TestCase):
    def setUp(self):
        self.site = Site()
        self.httpd = server(self.site)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.httpd.server_port)
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "collector.db")
        # 时钟/睡眠保持真实：来源服务按系统时间判定限速，客户端与来源必须共用一个时钟
        self.client = SourceClient(self.base)
        self.store = Store(self.db_path)
        self.engine = CollectionEngine(self.client, self.store)

    def tearDown(self):
        self.store.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        self.tmp.cleanup()

    # ---- 来源控制辅助 --------------------------------------------------
    def control(self, event, **kwargs):
        data = json.dumps({"event": event, **kwargs}).encode()
        req = urllib.request.Request(
            self.base + "/__control", data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)

    def run_cycle(self, **kw):
        summary = self.engine.run_cycle(trigger="test", **kw)
        coverages = self._coverages(summary["run_id"])
        reconcile_all(self.store, summary["run_id"], coverages,
                      revision_events=self.client.revisions())
        return summary, coverages

    def _coverages(self, run_id):
        from collector.cli import _coverages_from_run
        return _coverages_from_run(self.store, run_id, self.engine)

    def requirement(self, unit, item_id):
        return self.store.get_requirement(unit, item_id)

    # ---- 1. 截断必须如实报缺口 -----------------------------------------
    def test_truncation_reports_gap_never_pretend_complete(self):
        summary, coverages = self.run_cycle()
        # 单位全目录 total=9 > cap=4，最细的 robot/2025 仍有 5>4，结论只能是 gap
        self.assertEqual(summary["overall"], "gap_or_blocked")
        for unit in ("east", "west"):
            self.assertEqual(summary["units"][unit]["verdict"], "gap")
            self.assertIn("cap=4", summary["units"][unit]["reason"])
        # 各行业范围分别截断后取并集：id 0..7 至少在一个范围可见，
        # id=8 在所有匹配范围都落在 cap 之外，从未经任何查询返回
        observed_ids = set(coverages["east"].unique_observed.keys())
        self.assertEqual(observed_ids, {"0", "1", "2", "3", "4", "5", "6", "7"})
        self.assertNotIn("8", observed_ids)
        # scope_coverage 明确登记缺口依据
        gaps = [r for r in self.store.scope_coverage(summary["run_id"], "east") if r["gap_count"] > 0]
        self.assertTrue(gaps)
        robot_2025 = next(
            r for r in gaps if r["industry"] == "robot" and r["year"] == 2025
        )
        self.assertEqual((robot_2025["total"], robot_2025["gap_count"]), (5, 1))
        self.assertTrue(robot_2025["truncated"])

    # ---- 2. 交叉分类去重 + 跨单位同编号隔离 ----------------------------
    def test_overlap_dedup_and_unit_scoped_identity(self):
        summary, coverages = self.run_cycle()
        east = coverages["east"]
        # 交叉行业去重：id=0 同时出现在 robot/shared 两个行业及多个年份范围，
        # 归并后每个 id 只有一个身份（并集为 id 0..7，共 8 个）
        self.assertEqual(len(east.unique_observed), 8)
        obs = self.store.observations_of("east", "0")
        self.assertGreater(len(obs), 1, "应能看到同一需求经多条查询被观察")
        industries_seen = {q["industry"] for q in obs}
        self.assertIn("robot", industries_seen)
        self.assertIn("shared", industries_seen)
        # 同编号 id=0 在 east/west 是两个互不覆盖的需求
        self.assertTrue(self.requirement("east", "0"))
        self.assertTrue(self.requirement("west", "0"))
        east_reqs = {r["id"] for r in self.store.requirements_of_unit("east")}
        west_reqs = {r["id"] for r in self.store.requirements_of_unit("west")}
        self.assertEqual(east_reqs, west_reqs)
        self.assertEqual(len(self.store.all_requirements()), 16)

    # ---- 3. 限速单位隔离 + 不立即重试 ----------------------------------
    def test_throttle_isolates_unit_and_avoids_retry_storm(self):
        # 先正常采一轮，west 有成功原文
        summary1, _ = self.run_cycle()
        self.control("throttle", unit="east", seconds=2)
        # 禁用续采复用，强制真实 HTTP 交互以触发 429 路径
        summary2 = self.engine.run_cycle(trigger="test-throttled", resumable=False)
        self.assertEqual(summary2["units"]["east"]["verdict"], "throttled")
        # west 不受 east 限速影响
        self.assertEqual(summary2["units"]["west"]["verdict"], "gap")
        # 冷却期内再次调度 east：直接读 blocked_until，不发任何 catalog 请求
        blocked = self.store.get_blocked_until("east")
        self.assertGreater(blocked, time.time())
        summary3 = self.engine.run_cycle(trigger="test-still-blocked")
        new_quota = [
            q for q in self.store.query_runs(summary3["run_id"], "east") if q["status"] == "unit_quota"
        ]
        self.assertEqual(new_quota, [], "冷却期内不得再打来源、不得消耗配额")
        self.assertEqual(summary3["units"]["east"]["verdict"], "throttled")
        # 冷却结束后 east 可以重新采集
        time.sleep(blocked - time.time() + 0.2)
        summary4 = self.engine.run_cycle(trigger="test-recovered")
        self.assertEqual(summary4["units"]["east"]["verdict"], "gap")

    # ---- 4. 429 异常协议字段可区分 -------------------------------------
    def test_quota_exception_carries_retry_after(self):
        self.control("throttle", unit="east", seconds=2)
        with self.assertRaises(UnitQuotaExceeded) as ctx:
            self.client.catalog_page("east", version=1)
        self.assertEqual(ctx.exception.unit, "east")
        self.assertEqual(ctx.exception.retry_after, 2.0)
        # west 仍然可访问，证明配额按单位隔离
        page = self.client.catalog_page("west", version=1)
        self.assertEqual(page.version, 1)

    # ---- 5. 版本冲突：409 旧页不拼接，续采核对版本 ---------------------
    def test_version_conflict_invalidates_old_pages(self):
        summary1, _ = self.run_cycle()
        east_run1 = self.store.latest_finished_unit_run("east")
        self.assertEqual(east_run1["observed_version"], 1)
        # 来源推进 east 版本（move 事件），west 版本不变
        self.control("move", unit="east", id="0")
        with self.assertRaises(VersionExpired) as ctx:
            self.client.catalog_page("east", version=1)
        self.assertEqual(ctx.exception.current_version, 2)
        # 续采时发现版本不一致，整单位重采且结论基于新版本，不拼接旧页
        summary2 = self.engine.run_cycle(trigger="test-version-bump", units=["east"])
        self.assertEqual(summary2["units"]["east"]["version"], 2)
        reused = [q for q in self.store.query_runs(summary2["run_id"], "east") if q["status"] == "reused"]
        self.assertEqual(reused, [], "版本变化后不得复用上一轮任何页")
        # west 版本未变，续采应复用已保存原文
        summary3 = self.engine.run_cycle(trigger="test-resume-west", units=["west"])
        reused_west = [q for q in self.store.query_runs(summary3["run_id"], "west") if q["status"] == "reused"]
        self.assertTrue(reused_west, "版本一致时续采应复用已落盘原文而非重打 HTTP")

    # ---- 6. 分类移动只改关系，不撤需求 ---------------------------------
    def test_move_changes_industry_but_keeps_identity(self):
        self.run_cycle()
        before = self.requirement("east", "0")
        self.assertIn("robot", json.loads(before["industries_json"]))
        self.control("move", unit="east", id="0")
        run2 = self.engine.run_cycle(trigger="test-move")
        coverages2 = self._coverages(run2["run_id"])
        reconcile_all(self.store, run2["run_id"], coverages2,
                      revision_events=self.client.revisions())
        after = self.requirement("east", "0")
        # 身份键不变、修订推进、行业更新、仍是开放需求
        self.assertEqual(after["first_seen_run"], before["first_seen_run"])
        self.assertEqual(json.loads(after["industries_json"]), ["medical"])
        self.assertEqual(after["status"], OPEN)
        moves = [c for c in self.store.changes(run2["run_id"]) if c["change"] == "moved"]
        self.assertTrue(any(c["unit"] == "east" and c["id"] == "0" for c in moves))

    # ---- 7. 撤回：剔除可联系名单但保留库与快照修订 ---------------------
    def test_withdraw_vs_saved_snapshot(self):
        request = {"saved_selection": [{"unit": "east", "id": "0", "observed_revision": 1}]}
        load_selection(self.store, request)
        self.run_cycle()
        self.control("withdraw", unit="east", id="0")
        run2 = self.engine.run_cycle(trigger="test-withdraw")
        coverages2 = self._coverages(run2["run_id"])
        reconcile_all(self.store, run2["run_id"], coverages2,
                      revision_events=self.client.revisions())
        req = self.requirement("east", "0")
        self.assertEqual(req["status"], WITHDRAWN)
        report = update_list(self.store, run2["run_id"])
        saved = next(x for x in report["saved_selection"] if x["unit"] == "east" and x["id"] == "0")
        self.assertFalse(saved["contactable"])
        self.assertEqual(saved["saved_observed_revision"], 1, "经理保存的原修订必须保留")
        snap = self.store.selection_snapshot()[0]
        self.assertEqual(snap["observed_revision"], 1)
        self.assertNotIn(
            ("east", "0"),
            {(r["unit"], r["id"]) for r in self.store.list_snapshot(run2["run_id"]) if r["contactable"]},
        )

    # ---- 8. 完整目录缺席 = removed，区别于欠采未见 ---------------------
    def test_remove_conclusive_vs_unseen_gap(self):
        self.run_cycle()
        # id=1 能在 medical/shared 的完整范围中观察到；remove 后相关范围仍完整
        self.control("remove", unit="east", id="1")
        run2 = self.engine.run_cycle(trigger="test-remove")
        coverages2 = self._coverages(run2["run_id"])
        reconcile_all(self.store, run2["run_id"], coverages2,
                      revision_events=self.client.revisions())
        self.assertEqual(self.requirement("east", "1")["status"], REMOVED)

        # 限速制造欠采：已知需求未观察到但覆盖不成立，只能记 unseen_gap
        self.control("throttle", unit="east", seconds=2)
        run3 = self.engine.run_cycle(trigger="test-gap-unseen", resumable=False)
        coverages3 = self._coverages(run3["run_id"])
        reconcile_all(self.store, run3["run_id"], coverages3,
                      revision_events=self.client.revisions())
        # id=2 在上一轮是 open，本轮 east 整体欠采，身份必须保留
        self.assertEqual(self.requirement("east", "2")["status"], UNSEEN_GAP)
        self.assertIsNotNone(self.requirement("east", "2"))

    # ---- 9. 补登旧需求：只按更新时间推进必然漏掉 -----------------------
    def test_backdate_captures_older_requirement(self):
        self.run_cycle()
        self.control("backdate", unit="west")
        run2 = self.engine.run_cycle(trigger="test-backdate")
        coverages2 = self._coverages(run2["run_id"])
        reconcile_all(self.store, run2["run_id"], coverages2,
                      revision_events=self.client.revisions())
        older = self.requirement("west", "older-added")
        self.assertIsNotNone(older)
        self.assertEqual(older["year"], 2024)
        self.assertEqual(older["status"], NEW)

    # ---- 10. 不支持的筛选条件被来源拒绝，客户端不自造查询 --------------
    def test_unsupported_filter_is_rejected(self):
        import urllib.request
        req = urllib.request.Request(self.base + "/catalog?unit=east&prefix=x")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)
        # 规划器产出的全部查询键都在来源声明的 filters 内
        facets = self.client.facets()
        from collector.coverage import plan_scopes
        allowed = set(facets["filters"]) | {"page", "version"}
        for scope in plan_scopes(facets, "east"):
            params = {"unit": scope.unit, "page": "0", "version": "1"}
            if scope.industry is not None:
                params["industry"] = scope.industry
            if scope.year is not None:
                params["year"] = str(scope.year)
            self.assertTrue(set(params) <= allowed)

    # ---- 11. 原文与归并身份分表、原文不可变 ----------------------------
    def test_raw_payload_persisted_and_immutable(self):
        summary, _ = self.run_cycle()
        raws = self.store.conn.execute("SELECT COUNT(*) c FROM raw_response").fetchone()["c"]
        self.assertGreater(raws, 0)
        # 同版本再跑一轮，原文去重不新增
        before = self.store.conn.execute("SELECT COUNT(*) c FROM raw_response").fetchone()["c"]
        self.engine.run_cycle(trigger="test-raw-dedupe", units=["west"])
        after = self.store.conn.execute("SELECT COUNT(*) c FROM raw_response").fetchone()["c"]
        self.assertEqual(before, after)
        # 抽查一条原文确为来源 JSON 且可解析出 items
        row = self.store.conn.execute("SELECT body FROM raw_response LIMIT 1").fetchone()
        parsed = json.loads(row["body"])
        self.assertIn("items", parsed)
        self.assertIn("version", parsed)

    # ---- 12. 未知单位被拒绝，不能凭空采集 ------------------------------
    def test_unknown_unit_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.run_cycle(units=["north"])

    # ---- 13. 多轮生命周期：new->open 转移、撤回黏滞 --------------------
    def test_state_transitions_and_withdrawn_stickiness(self):
        s1, _ = self.run_cycle()
        # 首轮全部新观察
        self.assertEqual(self.requirement("east", "0")["status"], NEW)
        # 第二轮无变化：new 应转 open
        self.run_cycle()
        self.assertEqual(self.requirement("east", "0")["status"], OPEN)
        # 撤回后限速再采一轮：撤回事实不得被欠采未见覆盖
        self.control("withdraw", unit="east", id="0")
        run3 = self.engine.run_cycle(trigger="test-sticky-withdraw")
        cov3 = self._coverages(run3["run_id"])
        reconcile_all(self.store, run3["run_id"], cov3,
                      revision_events=self.client.revisions())
        self.assertEqual(self.requirement("east", "0")["status"], WITHDRAWN)
        self.control("throttle", unit="east", seconds=2)
        run4 = self.engine.run_cycle(trigger="test-sticky-under-gap", resumable=False)
        cov4 = self._coverages(run4["run_id"])
        reconcile_all(self.store, run4["run_id"], cov4,
                      revision_events=self.client.revisions())
        self.assertEqual(
            self.requirement("east", "0")["status"], WITHDRAWN,
            "欠采轮次不得把已确认撤回改写成 unseen_gap/removed",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
