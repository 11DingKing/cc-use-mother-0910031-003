"""积分结算核心规则测试：覆盖契约四条不变量与题目全部场景。"""
from __future__ import annotations

import sys
import threading
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from points_ledger import engine
from points_ledger.errors import (
    Conflict, NotFound, PeriodClosed, PreconditionFailed, ValidationError,
)
from points_ledger.models import ReviewDecision, Role, SessionStatus, SourceKind
from points_ledger.service import LedgerService


class EngineTest(unittest.TestCase):
    def test_points_formula_and_rounding(self) -> None:
        # 2h × 10 × 1.2 = 24；3h × 10 × 1.5 = 45；1h 协助 = 10
        self.assertEqual(engine.compute_points(Role.DOCENT, 2), 24)
        self.assertEqual(engine.compute_points(Role.LEADER, 3), 45)
        self.assertEqual(engine.compute_points(Role.ASSISTANT, 1), 10)

    def test_duplicate_detection(self) -> None:
        from points_ledger.models import Checkin
        a = Checkin("K1", "V1", "S1", Role.DOCENT, "school")
        b = Checkin("K2", "V1", "S1", Role.DOCENT, "venue")
        b_same_source = Checkin("K2", "V1", "S1", Role.DOCENT, "school")
        same = Checkin("K1", "V1", "S1", Role.DOCENT, "school")
        # 不同签到号即为疑点：无论跨渠道还是同渠道重发
        self.assertTrue(engine.detect_duplicates([a, b]))
        self.assertTrue(engine.detect_duplicates([a, b_same_source]))
        # 同一 checkin_id 重复导入幂等，不算疑点
        self.assertFalse(engine.detect_duplicates([a, same]))
        self.assertFalse(engine.detect_duplicates([a]))


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = LedgerService()
        self.svc.register_session("S1", "ACT-A", date(2025, 3, 1), 2)
        self.svc.register_session("S2", "ACT-B", date(2025, 5, 4), 2)
        self.svc.mark_session_held("S1")
        self.svc.mark_session_held("S2")

    def post_one(self, checkin_id: str, vid: str = "V1",
                 session: str = "S1", role: Role = Role.DOCENT,
                 source: str = "school") -> str:
        r = self.svc.ingest_checkin(checkin_id, vid, session, role, source)
        self.assertEqual(r["result"], "posted", r)
        return r["posted"][0]

    def first_positive_entry(self, vid: str, year: int = 2025) -> str:
        return [e["entry_id"] for e in self.svc.ledger(vid, year)
                if e["amount"] > 0 and e["kind"] in ("service", "late")][0]


class DuplicateReportTest(Fixture):
    def _make_duplicate(self) -> str:
        self.post_one("K1")
        r = self.svc.ingest_checkin("K2", "V1", "S1", Role.DOCENT, "venue")
        self.assertEqual(r["result"], "pending")
        # 挂起后净额归 0
        self.assertEqual(self.svc.balance("V1", 2025)["balance"], 0)
        pending = self.svc.list_pending()
        self.assertEqual(len(pending), 1)
        return pending[0]["pending_id"]

    def test_confirm_one_source(self) -> None:
        pid = self._make_duplicate()
        r = self.svc.resolve_pending(
            pid, ReviewDecision.CONFIRM, "审计员", chosen_checkin_id="K1")
        self.assertEqual(r["target_points"], 24)
        self.assertEqual(self.svc.balance("V1", 2025)["balance"], 24)

    def test_reject_all(self) -> None:
        pid = self._make_duplicate()
        self.svc.resolve_pending(pid, ReviewDecision.REJECT, "审计员")
        self.assertEqual(self.svc.balance("V1", 2025)["balance"], 0)

    def test_keep_both(self) -> None:
        pid = self._make_duplicate()
        self.svc.resolve_pending(pid, ReviewDecision.KEEP_BOTH, "审计员")
        # 两笔各 24
        self.assertEqual(self.svc.balance("V1", 2025)["balance"], 48)

    def test_resolution_is_idempotent_and_order_independent(self) -> None:
        # 先有重复队列、后补第三笔渠道，合并后结论仍唯一决定净额
        pid = self._make_duplicate()
        r = self.svc.ingest_checkin("K3", "V1", "S1", Role.DOCENT, "bureau")
        self.assertEqual(r["pending"], [pid])
        item = self.svc.list_pending()[0]
        self.assertEqual(set(item["checkin_ids"]), {"K1", "K2", "K3"})
        self.svc.resolve_pending(pid, ReviewDecision.REJECT, "审计员")
        with self.assertRaises(Conflict):
            self.svc.resolve_pending(pid, ReviewDecision.CONFIRM, "审计员")
        self.assertEqual(self.svc.balance("V1", 2025)["balance"], 0)

    def test_duplicate_before_session_held(self) -> None:
        self.svc.register_session("S9", "ACT-X", date(2025, 6, 1), 2)
        self.svc.ingest_checkin("A1", "V9", "S9", Role.ASSISTANT, "school")
        self.svc.ingest_checkin("A2", "V9", "S9", Role.ASSISTANT, "venue")
        r = self.svc.mark_session_held("S9")
        self.assertEqual(len(r["pending"]), 1)
        self.assertEqual(r["posted"], [])
        self.assertEqual(self.svc.balance("V9", 2025)["balance"], 0)

    def test_open_pending_blocks_close(self) -> None:
        self._make_duplicate()
        with self.assertRaises(PreconditionFailed):
            self.svc.close_period(2025)


class CancelSessionTest(Fixture):
    def test_cancel_reverses_points(self) -> None:
        self.post_one("K1", "V2", "S2", role=Role.ASSISTANT)
        self.assertEqual(self.svc.balance("V2", 2025)["balance"], 20)
        r = self.svc.cancel_session("S2", "暴雨取消", approver="负责人")
        self.assertEqual(len(r["reversals"]), 1)
        self.assertEqual(self.svc.balance("V2", 2025)["balance"], 0)
        # 重复取消幂等无新增冲回
        r2 = self.svc.cancel_session("S2", "再次确认", approver="负责人")
        self.assertEqual(r2["reversals"], [])

    def test_cancel_after_close_uses_correction(self) -> None:
        self.post_one("K1", "V2", "S2", role=Role.ASSISTANT)
        self.svc.close_period(2025, closed_on=date(2026, 1, 5))
        r = self.svc.cancel_session("S2", "复查取消",
                                    approver="审计", as_of=date(2026, 2, 1))
        self.assertEqual(len(r["corrections"]), 1)
        # 快照不变，冲减体现在 2026 开放期
        self.assertEqual(self.svc.get_period(2025)["snapshot"]["V2"], 20)
        corrections = self.svc.recompute_period(2025)[
            "corrections_attributable_to_period"]["rows"]
        self.assertTrue(all(c["origin"] == "cancel" for c in corrections))


class AppealTest(Fixture):
    def test_appeal_in_open_year_posts_entry(self) -> None:
        self.svc.file_appeal("AP1", "V1", 2025, 10, "设备故障")
        r = self.svc.approve_appeal("AP1", "运营员")
        self.assertEqual(r["status"], "approved")
        self.assertEqual(self.svc.balance("V1", 2025)["balance"], 10)

    def test_appeal_rejected(self) -> None:
        self.svc.file_appeal("AP1", "V1", 2025, 10, "无凭证")
        self.svc.approve_appeal("AP1", "运营员", reject=True)
        self.assertEqual(self.svc.balance("V1", 2025)["balance"], 0)

    def test_appeal_after_close_uses_correction(self) -> None:
        self.svc.close_period(2025)
        self.svc.file_appeal("AP1", "V1", 2025, 8, "漏报已核证")
        r = self.svc.approve_appeal("AP1", "运营员", as_of=date(2026, 2, 2))
        self.assertEqual(r["status"], "approved_via_correction")
        self.assertEqual(self.svc.get_period(2025)["snapshot"].get("V1", 0), 0)
        # 2026 账上 +8
        self.assertEqual(self.svc.balance("V1", 2026)["balance"], 8)

    def test_appeal_validation(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.file_appeal("AP", "V1", 2025, 0, "x")


class PartialReversalTest(Fixture):
    def test_partial_and_full_reversal_bounds(self) -> None:
        self.post_one("K1", "V3", "S1")
        eid = self.first_positive_entry("V3")
        self.svc.reverse_partial(eid, 10, "请假", "负责人")
        self.assertEqual(self.svc.balance("V3", 2025)["balance"], 14)
        self.svc.reverse_partial(eid, None, "剩余全撤", "负责人")
        self.assertEqual(self.svc.balance("V3", 2025)["balance"], 0)
        with self.assertRaises(ValidationError):
            self.svc.reverse_partial(eid, 1, "超额", "负责人")

    def test_reversal_after_close_rejected(self) -> None:
        self.post_one("K1", "V3", "S1")
        eid = self.first_positive_entry("V3")
        self.svc.close_period(2025)
        with self.assertRaises(PeriodClosed):
            self.svc.reverse_partial(eid, 5, "封存后撤销", "负责人",
                                     as_of=date(2026, 1, 10))


class LateEntryTest(Fixture):
    def test_late_entry_open_plan_year(self) -> None:
        # 2025 未封账，2026 年补录 → LATE 分录归 2025
        self.svc.register_session("S8", "ACT-L", date(2025, 11, 1), 2,
                                  status=SessionStatus.HELD, plan_year=2025)
        r = self.svc.record_late_batch([{
            "checkin_id": "L1", "volunteer_id": "V8", "session_id": "S8",
            "role": Role.ASSISTANT, "source": "audit",
        }], recorded_on=date(2026, 1, 2))
        self.assertEqual(len(r["posted"]), 1)
        entries = self.svc.ledger("V8", 2025)
        self.assertEqual(entries[0]["kind"], "late")
        self.assertEqual(self.svc.balance("V8", 2025)["balance"], 20)

    def test_late_entry_closed_plan_year_uses_correction(self) -> None:
        self.svc.close_period(2025, closed_on=date(2026, 1, 5))
        self.svc.register_session("S8", "ACT-L", date(2025, 11, 1), 2,
                                  status=SessionStatus.HELD, plan_year=2025)
        r = self.svc.record_late_batch([{
            "checkin_id": "L1", "volunteer_id": "V8", "session_id": "S8",
            "role": Role.ASSISTANT, "source": "audit",
        }], recorded_on=date(2026, 3, 1))
        self.assertEqual(r["posted"], [])
        # 20 分以更正单入 2026，快照不变
        self.assertEqual(self.svc.balance("V8", 2026)["balance"], 20)
        self.assertEqual(self.svc.get_period(2025)["snapshot"].get("V8", 0), 0)

    def test_late_batch_requires_earlier_plan_year(self) -> None:
        self.svc.register_session("S7", "ACT-M", date(2026, 1, 1), 2,
                                  plan_year=2026)
        with self.assertRaises(ValidationError):
            self.svc.record_late_batch([{
                "checkin_id": "L2", "volunteer_id": "V8", "session_id": "S7",
                "role": Role.ASSISTANT, "source": "audit",
            }], recorded_on=date(2026, 3, 1))


class CloseAndCorrectionTest(Fixture):
    def test_concurrent_close_single_writer(self) -> None:
        self.post_one("K1", "V1")
        outcomes: list[dict] = []
        barrier = threading.Barrier(6)

        def close() -> None:
            barrier.wait()
            outcomes.append(self.svc.close_period(2025))

        threads = [threading.Thread(target=close) for _ in range(5)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for o in outcomes if not o["already_closed"]), 1)
        snapshots = {tuple(sorted(o["snapshot"].items())) for o in outcomes}
        self.assertEqual(len(snapshots), 1)

    def test_close_is_idempotent_and_snapshot_immutable(self) -> None:
        self.post_one("K1", "V1")
        first = self.svc.close_period(2025, closed_on=date(2026, 1, 5))
        second = self.svc.close_period(2025, closed_on=date(2026, 1, 9))
        self.assertTrue(second["already_closed"])
        self.assertEqual(second["closed_date"], first["closed_date"])

    def test_closed_year_rejects_normal_post(self) -> None:
        self.post_one("K1", "V1")
        self.svc.close_period(2025)
        with self.assertRaises(PeriodClosed):
            self.svc.ingest_checkin("K9", "V9", "S1", Role.ASSISTANT, "school")

    def test_manual_correction_red_blue_pair(self) -> None:
        self.post_one("K1", "V1")  # 24
        eid = self.first_positive_entry("V1")
        self.svc.close_period(2025, closed_on=date(2026, 1, 5))
        r = self.svc.create_correction(
            2025, "V1", "时长复核应为 20", "审计组",
            entry_id=eid, new_amount=20, as_of=date(2026, 2, 1))
        self.assertTrue(r["red_entry_id"])
        self.assertTrue(r["blue_entry_id"])
        self.assertEqual(r["origin"], "manual")
        bal = self.svc.balance("V1", 2025)
        self.assertEqual(bal["settled_snapshot"], 24)
        self.assertEqual(bal["effective_balance"], 20)

    def test_pure_red_correction(self) -> None:
        self.post_one("K1", "V1")
        eid = self.first_positive_entry("V1")
        self.svc.close_period(2025)
        r = self.svc.create_correction(
            2025, "V1", "虚报名次", "审计组",
            entry_id=eid, new_amount=0, as_of=date(2026, 2, 1))
        self.assertTrue(r["red_entry_id"])
        self.assertIsNone(r["blue_entry_id"])
        self.assertEqual(self.svc.balance("V1", 2025)["effective_balance"], 0)

    def test_correction_requires_closed_year_and_open_booking_period(self) -> None:
        self.post_one("K1", "V1")
        eid = self.first_positive_entry("V1")
        with self.assertRaises(PreconditionFailed):
            self.svc.create_correction(
                2025, "V1", "x", "审计组", entry_id=eid, new_amount=10)
        self.svc.close_period(2025)
        with self.assertRaises(PeriodClosed):
            # 不能记回被封的同一年度
            self.svc.create_correction(
                2025, "V1", "x", "审计组", entry_id=eid, new_amount=10,
                as_of=date(2025, 12, 31))

    def test_duplicate_discovered_after_close_settles_via_corrections(self) -> None:
        self.post_one("K1", "V1")  # 24 已封进快照
        self.svc.close_period(2025, closed_on=date(2026, 1, 5))
        # 次年发现场馆对同一服务又报了一次 → 进待确认，已封积分按更正单挂起
        r = self.svc.ingest_checkin(
            "K2", "V1", "S1", Role.DOCENT, "venue")
        self.assertEqual(r["result"], "pending")
        pid = self.svc.list_pending()[0]["pending_id"]
        # 挂起更正单使开放期净额为 -24，快照仍是 24
        self.assertEqual(self.svc.balance("V1", 2026)["balance"], -24)
        # 核对驳回：维持净额 0（快照 24 − 挂起 24）
        self.svc.resolve_pending(pid, ReviewDecision.REJECT, "审计",
                                 on_date=date(2026, 2, 5))
        self.assertEqual(self.svc.balance("V1", 2026)["balance"], -24)
        self.assertEqual(self.svc.get_period(2025)["snapshot"]["V1"], 24)
        rec25 = self.svc.recompute_period(2025)
        self.assertTrue(rec25["matches"], rec25["checks"])
        rec26 = self.svc.recompute_period(2026)
        self.assertTrue(rec26["matches"], rec26["checks"])

    def test_duplicate_after_close_keep_both_restores_and_adds(self) -> None:
        self.post_one("K1", "V1")
        self.svc.close_period(2025, closed_on=date(2026, 1, 5))
        r = self.svc.ingest_checkin("K2", "V1", "S1", Role.DOCENT, "venue")
        pid = r["pending"][0]
        self.svc.resolve_pending(pid, ReviewDecision.KEEP_BOTH, "审计",
                                 on_date=date(2026, 2, 5))
        # -24 挂起 + 48 补记 = 24 净增，有效余额 = 24(快照) + 24 = 48
        self.assertEqual(self.svc.balance("V1", 2026)["balance"], 24)
        self.assertTrue(self.svc.verify_conservation()["ok"])


class RecomputeAndConservationTest(Fixture):
    def test_recompute_open_year_matches(self) -> None:
        self.post_one("K1", "V1")
        self.post_one("K2", "V2", "S2", role=Role.ASSISTANT)
        self.svc.cancel_session("S2", "暴雨")
        rec = self.svc.recompute_period(2025)
        self.assertTrue(rec["matches"], rec["checks"])
        self.assertEqual(rec["fact_totals"], {"V1": 24})

    def test_recompute_closed_year_with_all_adjustment_kinds(self) -> None:
        # V1 重复后确认 24；V2 正常 20；V3 领队 3h=45 部分撤 15
        self.post_one("K1", "V1")
        self.svc.ingest_checkin("K1b", "V1", "S1", Role.DOCENT, "venue")
        pid = self.svc.list_pending()[0]["pending_id"]
        self.svc.resolve_pending(pid, ReviewDecision.CONFIRM, "审计",
                                 chosen_checkin_id="K1")
        self.post_one("K2", "V2", "S2", role=Role.ASSISTANT)
        self.svc.register_session("S3", "ACT-C", date(2025, 9, 9), 3)
        self.svc.mark_session_held("S3")
        self.post_one("K3", "V3", "S3", role=Role.LEADER)
        v3 = self.first_positive_entry("V3")
        self.svc.reverse_partial(v3, 15, "请假", "负责人")
        self.svc.file_appeal("AP1", "V2", 2025, 5, "少计")
        self.svc.approve_appeal("AP1", "运营员")

        self.svc.close_period(2025, closed_on=date(2026, 1, 5))

        # 封账后：取消 S2 追冲、申诉补记、跨年度补录、手工更正 V3
        self.svc.cancel_session("S2", "复查取消",
                                approver="审计", as_of=date(2026, 2, 1))
        self.svc.file_appeal("AP2", "V1", 2025, 4, "漏报已核")
        self.svc.approve_appeal("AP2", "运营员", as_of=date(2026, 2, 2))
        self.svc.register_session("S8", "ACT-L", date(2025, 11, 1), 2,
                                  status=SessionStatus.HELD, plan_year=2025)
        self.svc.record_late_batch([{
            "checkin_id": "L1", "volunteer_id": "V8", "session_id": "S8",
            "role": Role.ASSISTANT, "source": "audit",
        }], recorded_on=date(2026, 3, 1))
        v3_entry = [e for e in self.svc.ledger("V3", 2025)
                    if e["kind"] == "service"][0]["entry_id"]
        self.svc.create_correction(
            2025, "V3", "复核应为 40", "审计组",
            entry_id=v3_entry, new_amount=40, as_of=date(2026, 3, 2))

        rec25 = self.svc.recompute_period(2025)
        self.assertTrue(rec25["matches"], rec25["checks"])
        rec26 = self.svc.recompute_period(2026)
        self.assertTrue(rec26["matches"], rec26["checks"])
        self.assertTrue(self.svc.verify_conservation()["ok"])

    def test_explain_entry_carries_evidence(self) -> None:
        eid = self.post_one("K1", "V1")
        detail = self.svc.explain_entry(eid)
        keys = {ev["key"] for ev in detail["evidence"]}
        self.assertIn("签到", keys)
        self.assertIn("场次", keys)
        self.assertIn("规则", keys)


if __name__ == "__main__":
    unittest.main()
