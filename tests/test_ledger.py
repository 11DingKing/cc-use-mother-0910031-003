"""积分结算核心领域测试：覆盖重复报送、取消冲回、部分撤销、申诉、
跨年度补录、更正单、并发封账、守恒与审计复算。"""
from __future__ import annotations

import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from points_ledger import LedgerService, ServiceError, Store
from points_ledger import events as ev
from points_ledger import projection as P


def bootstrap_2026() -> tuple[Store, LedgerService]:
    store = Store(":memory:")
    s = LedgerService(store)
    s.open_period("2026")
    s.register_volunteer("v1", "张三")
    s.register_session("sess1", "2026-05-01", "展厅引导")
    return store, s


def full_report(s: LedgerService, rid: str, source: str, *,
                session="sess1", vid="v1", role="引导", hours=2.0):
    s.submit_report(report_id=rid, session_id=session, volunteer_id=vid,
                    source=source, role=role, hours=hours)
    s.record_checkin(checkin_id=f"ci-{rid}", session_id=session,
                     volunteer_id=vid, source=source, at="2026-05-01T09:00:00Z")
    s.review_report(rid, "approved", "auditor-li")


class AccrualTest(unittest.TestCase):
    def test_points_formula(self):
        self.assertEqual(P.points_for("引导", 2.0)[0], 20)
        self.assertEqual(P.points_for("讲解", 2.0)[0], 24)  # 1.2 系数
        self.assertEqual(P.points_for("未知角色", 0.3)[0], 3)

    def test_full_flow_accrues_on_completion(self):
        _, s = bootstrap_2026()
        # 审核通过但场次未完成：不计提
        full_report(s, "r1", "school")
        self.assertEqual(s.balance("v1")["balance"], 0)
        # 签到/场次/审核齐备后才入账
        s.mark_session_completed("sess1")
        self.assertEqual(s.balance("v1")["balance"], 20)
        stmt = s.account_statement("v1")
        self.assertEqual(len(stmt["entries"]), 1)
        e = stmt["entries"][0]
        self.assertEqual(e["kind"], "accrual")
        self.assertEqual(e["period"], "2026")
        self.assertEqual(e["service_period"], "2026")
        # 逐笔来源解释完整
        trace = "\n".join(e["source_trace"])
        self.assertIn("school 报送 r1", trace)
        self.assertIn("签到 ci-r1", trace)
        self.assertIn("审核 approved", trace)
        self.assertIn("20", str(e["breakdown"]))
        # 幂等：重复标记完成不产生第二张分录
        s.mark_session_completed("sess1")
        self.assertEqual(len(s.state().entries), 1)

    def test_rejected_review_no_points(self):
        _, s = bootstrap_2026()
        s.submit_report(report_id="r1", session_id="sess1", volunteer_id="v1",
                        source="school", role="引导", hours=2.0)
        s.review_report("r1", "rejected", "auditor-li")
        s.mark_session_completed("sess1")
        self.assertEqual(s.balance("v1")["balance"], 0)


class DuplicateTest(unittest.TestCase):
    def test_duplicates_enter_pending_and_excluded_from_ranking(self):
        _, s = bootstrap_2026()
        full_report(s, "r-school", "school")
        s.mark_session_completed("sess1")
        self.assertEqual(s.balance("v1")["balance"], 20)
        # 场馆重复报送：整组进待确认队列，已入账积分立即挂起冲回
        full_report(s, "r-venue", "venue")
        self.assertEqual(s.balance("v1")["balance"], 0)
        pending = s.pending_duplicates()
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(pending[0]["reports"]), 2)
        rank = s.ranking("2026")
        self.assertEqual(rank["total_points"], 0)  # 挂起积分不污染排名
        self.assertEqual(rank["rows"], [])

        # 裁决为重复，采信学校：仅学校报送入账
        s.resolve_duplicate(pending[0]["group_id"], "duplicate",
                            winner_report_id="r-school", arbiter="center-wang")
        self.assertEqual(s.balance("v1")["balance"], 20)
        rank = s.ranking("2026")
        self.assertEqual(rank["rows"][0]["points"], 20)

    def test_distinct_sources_both_accrue(self):
        _, s = bootstrap_2026()
        full_report(s, "r-school", "school")
        full_report(s, "r-venue", "venue")
        gid = s.pending_duplicates()[0]["group_id"]
        s.resolve_duplicate(gid, "distinct", arbiter="center-wang")
        s.mark_session_completed("sess1")
        # 两条确属不同服务：各自计提
        self.assertEqual(s.balance("v1")["balance"], 40)

    def test_cannot_resolve_twice(self):
        _, s = bootstrap_2026()
        full_report(s, "r-school", "school")
        full_report(s, "r-venue", "venue")
        gid = s.pending_duplicates()[0]["group_id"]
        s.resolve_duplicate(gid, "duplicate", winner_report_id="r-school")
        with self.assertRaises(ServiceError):
            s.resolve_duplicate(gid, "distinct")


class CancellationTest(unittest.TestCase):
    def test_cancel_reverses_accrued_points(self):
        _, s = bootstrap_2026()
        full_report(s, "r1", "school")
        s.mark_session_completed("sess1")
        self.assertEqual(s.balance("v1")["balance"], 20)
        # 取消场次：积分同步冲回
        s.cancel_session("sess1")
        self.assertEqual(s.balance("v1")["balance"], 0)
        entries = s.account_statement("v1")["entries"]
        self.assertEqual([e["kind"] for e in entries], ["accrual", "reversal"])
        self.assertEqual(entries[1]["pairs"], ["E000001"])

    def test_cancel_before_completion_never_accrues(self):
        _, s = bootstrap_2026()
        full_report(s, "r1", "school")
        s.cancel_session("sess1")
        self.assertEqual(s.state().entries, [])
        # 取消状态不能直接标记完成（必须走申诉）
        with self.assertRaises(ServiceError):
            s.mark_session_completed("sess1")


class PartialRevocationTest(unittest.TestCase):
    def test_partial_revocation_keeps_balance_consistent(self):
        _, s = bootstrap_2026()
        full_report(s, "r1", "school", hours=4.0)  # 40 分
        s.mark_session_completed("sess1")
        s.revoke_service(target_report_id="r1", fraction=0.5,
                         reason="服务时长核减", voucher_id="RV-1")
        self.assertEqual(s.balance("v1")["balance"], 20)
        with self.assertRaises(ServiceError):
            s.revoke_service(target_report_id="r1", fraction=0.6, reason="超额撤销")
        s.revoke_service(target_report_id="r1", fraction=0.5, reason="全部核减")
        self.assertEqual(s.balance("v1")["balance"], 0)

    def test_revocation_appeal_upheld_restores(self):
        _, s = bootstrap_2026()
        full_report(s, "r1", "school", hours=4.0)
        s.mark_session_completed("sess1")
        s.revoke_service(target_report_id="r1", fraction=0.5, reason="核减")
        self.assertEqual(s.balance("v1")["balance"], 20)
        s.submit_appeal(appeal_id="a1", kind="revocation", subject_id="r1",
                        appellant="v1", reason="时长记录有误")
        s.decide_appeal("a1", "upheld", arbiter="arbiter-zhao")
        # 申诉成立：恢复单与冲回单配对，余额回到 40
        self.assertEqual(s.balance("v1")["balance"], 40)
        kinds = [e["kind"] for e in s.account_statement("v1")["entries"]]
        self.assertEqual(kinds, ["accrual", "reversal", "reinstatement"])
        # 已裁决申诉不能二次裁决
        with self.assertRaises(ServiceError):
            s.decide_appeal("a1", "upheld", arbiter="arbiter-zhao")

    def test_denied_appeal_does_not_move_balance(self):
        _, s = bootstrap_2026()
        full_report(s, "r1", "school")
        s.mark_session_completed("sess1")
        s.revoke_service(target_report_id="r1", fraction=0.5, reason="核减")
        before = s.balance("v1")["balance"]
        s.submit_appeal(appeal_id="a1", kind="revocation", subject_id="r1",
                        appellant="v1", reason="不服")
        s.decide_appeal("a1", "denied", arbiter="arbiter-zhao")
        self.assertEqual(s.balance("v1")["balance"], before)


class ReviewAppealTest(unittest.TestCase):
    def test_review_appeal_upheld_accrues(self):
        _, s = bootstrap_2026()
        s.submit_report(report_id="r1", session_id="sess1", volunteer_id="v1",
                        source="school", role="引导", hours=2.0)
        s.review_report("r1", "rejected", "auditor-li", note="材料不足")
        s.mark_session_completed("sess1")
        self.assertEqual(s.balance("v1")["balance"], 0)
        s.submit_appeal(appeal_id="a1", kind="review", subject_id="r1",
                        appellant="v1", reason="补传签到凭证")
        s.decide_appeal("a1", "upheld", arbiter="arbiter-zhao")
        self.assertEqual(s.balance("v1")["balance"], 20)


class CrossYearAndCloseTest(unittest.TestCase):
    def _two_years_with_accrual(self):
        store = Store(":memory:")
        s = LedgerService(store)
        s.open_period("2025")
        s.register_volunteer("v1", "张三")
        s.register_session("sess1", "2025-12-20", "年末讲解")
        full_report(s, "r1", "school", role="讲解", hours=2.0)  # 24 分
        s.mark_session_completed("sess1")
        s.open_period("2026")
        return s

    def test_sealed_period_immutable_and_recomputable(self):
        s = self._two_years_with_accrual()
        result = s.close_period("2025")
        self.assertFalse(result["idempotent"])
        snap = result["snapshot"]
        self.assertEqual(snap["volunteer_totals"], {"v1": 24})
        # 封账后不能再封/重开
        with self.assertRaises(ServiceError):
            s.open_period("2025")

        rec = s.recompute_period("2025")
        self.assertTrue(rec["snapshot_matches_recalculation"])
        self.assertTrue(rec["recalculated"]["balanced"])
        self.assertTrue(rec["global_conservation"])
        self.assertTrue(rec["hash_chain_ok"])

    def test_post_close_reversal_lands_in_open_period_as_late_entry(self):
        s = self._two_years_with_accrual()
        s.close_period("2025")
        # 年度封账后发现场次取消：冲回进入 2026，但标注服务期 2025
        s.cancel_session("sess1")
        self.assertEqual(s.balance("v1")["balance"], 0)
        rec2025 = s.recompute_period("2025")
        self.assertTrue(rec2025["totals_unchanged_since_close"])
        self.assertEqual(rec2025["recalculated"]["volunteer_totals"], {"v1": 24})
        reversal = [e for e in s.state().entries if e.kind == "reversal"][0]
        self.assertEqual(reversal.period, "2026")
        self.assertEqual(reversal.service_period, "2025")

        # 申诉取消成立：场次恢复，积分作为跨年度补提回到 2026
        s.submit_appeal(appeal_id="a1", kind="cancellation", subject_id="sess1",
                        appellant="v1", reason="取消系误操作")
        s.decide_appeal("a1", "upheld", arbiter="arbiter-zhao")
        self.assertEqual(s.balance("v1")["balance"], 24)
        late = [e for e in s.state().entries if e.kind == "late_accrual"]
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0].period, "2026")
        self.assertEqual(late[0].service_period, "2025")
        # 2025 年合计依旧定格不变
        self.assertTrue(s.recompute_period("2025")["totals_unchanged_since_close"])

    def test_close_without_open_period_rejects_late_changes(self):
        s = self._two_years_with_accrual()
        s.close_period("2025")
        s.close_period("2026")
        with self.assertRaises(ServiceError):
            s.cancel_session("sess1")  # 无开放期间承接冲回

    def test_concurrent_close_is_idempotent(self):
        s = self._two_years_with_accrual()
        outcomes = []

        def close():
            outcomes.append(s.close_period("2025"))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: close(), range(8)))
        close_events = [e for e in s.store.read_events() if e.type == ev.PERIOD_CLOSED]
        self.assertEqual(len(close_events), 1)  # 只有一条封账事件
        self.assertEqual(len({o["snapshot"]["closed_seq"] for o in outcomes}), 1)


class CorrectionVoucherTest(unittest.TestCase):
    def test_correction_only_way_after_close(self):
        store = Store(":memory:")
        s = LedgerService(store)
        s.open_period("2025")
        s.register_volunteer("v1", "张三")
        s.register_session("sess1", "2025-06-01", "引导")
        s.register_session("sess2", "2026-06-01", "新年活动")
        full_report(s, "r1", "school")
        s.mark_session_completed("sess1")
        s.open_period("2026")
        s.close_period("2025")
        entry_id = s.state().entries[0].entry_id

        # 开放期间（2026）入账的分录不能用更正单，应走冲回/撤销
        full_report(s, "r2", "school", session="sess2")
        s.mark_session_completed("sess2")
        open_entry_id = [e for e in s.state().entries if e.period == "2026"][0].entry_id
        with self.assertRaises(ServiceError):
            s.issue_correction(voucher_id="C0", target_entry_id=open_entry_id,
                               volunteer_id="v1", delta=5, approver="boss",
                               reason="开放期不能用更正单")
        # 负余额保护
        with self.assertRaises(ServiceError):
            s.issue_correction(voucher_id="C1", target_entry_id=entry_id,
                               volunteer_id="v1", delta=-100, approver="boss",
                               reason="超扣")
        # 正常更正：封账期 -5 分调整，落在 2026，2025 定格不变
        s.issue_correction(voucher_id="C2", target_entry_id=entry_id,
                           volunteer_id="v1", delta=-5, approver="boss",
                           reason="复核时长扣减")
        self.assertEqual(s.balance("v1")["balance"], 35)  # 20(2025) + 20(2026) - 5
        rec2025 = s.recompute_period("2025")
        self.assertTrue(rec2025["totals_unchanged_since_close"])
        corr = [e for e in s.state().entries if e.kind == "correction"][0]
        self.assertEqual(corr.period, "2026")
        self.assertEqual(corr.pairs, (entry_id,))
        self.assertEqual(corr.evidence["voucher_id"], "C2")
        # 更正单编号不可重复
        with self.assertRaises(ServiceError):
            s.issue_correction(voucher_id="C2", target_entry_id=entry_id,
                               volunteer_id="v1", delta=-1, approver="boss",
                               reason="重复")


class ConservationTest(unittest.TestCase):
    def test_conservation_under_mixed_scenario(self):
        """复杂混合场景后逐账户合计恒为零，哈希链连续。"""
        store = Store(":memory:")
        s = LedgerService(store)
        s.open_period("2025")
        s.register_volunteer("v1", "张三")
        s.register_volunteer("v2", "李四")
        s.register_session("a", "2025-03-01", "活动甲")
        s.register_session("b", "2025-04-01", "活动乙")
        # v1 正常 20 分；v2 两条重复来源后裁决 distinct 各 20
        full_report(s, "ra", "school", session="a", vid="v1")
        full_report(s, "rb1", "school", session="b", vid="v2")
        full_report(s, "rb2", "venue", session="b", vid="v2")
        s.mark_session_completed("a")
        gid = s.pending_duplicates()[0]["group_id"]
        s.resolve_duplicate(gid, "distinct", arbiter="w")
        s.mark_session_completed("b")
        # v1 部分撤销 50%
        s.revoke_service(target_report_id="ra", fraction=0.5, reason="核减")
        # 申诉恢复
        s.submit_appeal(appeal_id="ap", kind="revocation", subject_id="ra",
                        appellant="v1", reason="误核")
        s.decide_appeal("ap", "upheld", arbiter="z")
        # 跨年：封账后更正
        s.open_period("2026")
        s.close_period("2025")
        target = [e for e in s.state().entries if e.volunteer_id == "v2"][0].entry_id
        s.issue_correction(voucher_id="C1", target_entry_id=target,
                           volunteer_id="v2", delta=3, approver="boss", reason="补3分")
        report = s.conservation_report()
        self.assertEqual(report["all_accounts_total"], 0)
        self.assertTrue(report["hash_chain_ok"])
        # 余额：v1 = 20，v2 = 40 + 3
        self.assertEqual(s.balance("v1")["balance"], 20)
        self.assertEqual(s.balance("v2")["balance"], 43)

    def test_replay_is_deterministic(self):
        _, s = bootstrap_2026()
        full_report(s, "r1", "school")
        s.mark_session_completed("sess1")
        events = s.store.read_events()
        run1 = P.replay(events)
        run2 = P.replay(list(reversed(events)))  # 顺序由 seq 决定，与输入次序无关
        self.assertEqual(
            [(e.entry_id, e.amount, e.hash) for e in run1.entries],
            [(e.entry_id, e.amount, e.hash) for e in run2.entries],
        )


if __name__ == "__main__":
    unittest.main()
