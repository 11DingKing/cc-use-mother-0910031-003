"""端到端人工冒烟：题目业务叙事全流程。"""
from __future__ import annotations

import sys
import threading
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from points_ledger.models import Role, SessionStatus, ReviewDecision
from points_ledger.service import LedgerService


def main() -> None:
    svc = LedgerService()

    # ---------- 2025 年度业务 ----------
    svc.register_session("S1", "ACT-A", date(2025, 3, 1), 2)
    svc.register_session("S2", "ACT-B", date(2025, 5, 4), 2)
    svc.register_session("S3", "ACT-C", date(2025, 9, 9), 3)
    svc.mark_session_held("S1")
    svc.mark_session_held("S2")

    # V1 讲解员被学校和场馆重复报送同一场
    print(svc.ingest_checkin("K1", "V1", "S1", Role.DOCENT, "school"))
    print(svc.ingest_checkin("K2", "V1", "S1", Role.DOCENT, "venue"))
    # V2 正常签到
    svc.ingest_checkin("K3", "V2", "S2", Role.ASSISTANT, "school")

    pending = svc.list_pending()
    assert len(pending) == 1, pending
    p = pending[0]
    print("待确认：", p["pending_id"], p["reason"], "提议分", p["proposed"]["amount"])
    # 挂起后 V1 净额必须为 0（红冲成对）
    assert svc.balance("V1", 2025)["balance"] == 0

    # 核对：确认学校那一笔
    r = svc.resolve_pending(p["pending_id"], ReviewDecision.CONFIRM, "审计员甲",
                            chosen_checkin_id="K1", reason="场馆为误报")
    print("核对结论：", r["resolution"], "目标分", r["target_points"])
    assert svc.balance("V1", 2025)["balance"] == 24  # 2h*10*1.2

    # S2 取消：V2 的 20 分必须同步冲回
    c = svc.cancel_session("S2", "活动因暴雨取消", approver="场馆负责人")
    print("取消场次：", c)
    assert svc.balance("V2", 2025)["balance"] == 0

    # V3 领队 S3 45 分，后部分撤销 15 分
    svc.mark_session_held("S3")
    svc.ingest_checkin("K4", "V3", "S3", Role.LEADER, "venue")
    assert svc.balance("V3", 2025)["balance"] == 45
    eid = [e["entry_id"] for e in svc.ledger("V3", 2025) if e["amount"] > 0][0]
    svc.reverse_partial(eid, 15, "中途请假 1 小时", "场馆负责人")
    assert svc.balance("V3", 2025)["balance"] == 30

    # 超额撤销必须被拒
    try:
        svc.reverse_partial(eid, 999, "x", "y")
        raise AssertionError("超额撤销未被拒绝")
    except Exception as exc:
        print("超额撤销被拒：", type(exc).__name__)

    # V1 申诉少计 10 分，成立
    svc.file_appeal("AP1", "V1", 2025, 10, "签到设备故障少计一场")
    ap = svc.approve_appeal("AP1", "文博中心运营员")
    print("申诉：", ap)
    assert svc.balance("V1", 2025)["balance"] == 34

    print("封账前排名：", svc.rankings(2025)["rankings"])

    # ---------- 封账 2025（含并发） ----------
    results: list[dict] = []
    errors: list[Exception] = []

    def close() -> None:
        try:
            results.append(svc.close_period(2025, closed_on=date(2026, 1, 5)))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=close) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len({json_safe(r["snapshot"]) for r in results}) == 1
    assert sum(1 for r in results if not r["already_closed"]) == 1
    print("并发封账：5 个请求，快照一致，仅 1 次真正封账")
    print("快照：", results[0]["snapshot"])

    # 封账后普通写入被拒
    try:
        svc.ingest_checkin("KX", "V9", "S1", Role.ASSISTANT, "school")
        # S1 已 HELD → 会尝试入账封存年
        raise AssertionError("封账后普通分录未被拒绝")
    except Exception as exc:
        print("封账后写入被拒：", type(exc).__name__, str(exc)[:40])

    # ---------- 2026 开放期：封账后调整只能走更正单 ----------
    # 1) 封账后发现 2025 年 S1 的 V1 其实还应再核减（场馆报送确为重复，
    #    但学校记录多算了时长）——更正单：红冲原剩余 24，蓝字重记 20
    v1_entry = [e for e in svc.ledger("V1", 2025)
                if e["kind"] == "service" and e["amount"] == 24][0]
    corr1 = svc.create_correction(
        2025, "V1", "时长复核为 100 分钟，应 20 分", "审计组",
        entry_id=v1_entry["entry_id"], new_amount=20,
        as_of=date(2026, 2, 1))
    print("更正单1：", corr1["red_entry_id"], corr1["blue_entry_id"])

    # 2) V2 申诉：2025 少计 8 分 → 纯蓝字更正单
    svc.file_appeal("AP2", "V2", 2025, 8, "漏报一场已核证")
    ap2 = svc.approve_appeal("AP2", "运营员", as_of=date(2026, 2, 2))
    assert ap2["status"] == "approved_via_correction"
    print("封账后申诉：", ap2)

    # 3) 跨年度补录：2026 年补录 2025 年遗漏的 S4（V4 协助员 2h = 20）
    svc.register_session("S4", "ACT-D", date(2025, 11, 11), 2,
                         status=SessionStatus.HELD, plan_year=2025)
    late = svc.record_late_batch([{
        "checkin_id": "K5", "volunteer_id": "V4", "session_id": "S4",
        "role": Role.ASSISTANT, "source": "late-audit",
    }], recorded_on=date(2026, 3, 1))
    print("跨年度补录：", late)
    # 2025 快照不变；20 分以更正单入 2026
    assert svc.get_period(2025)["snapshot"].get("V4", 0) == 0

    # 4) 封账后取消另一场 2025 年 S3（V3 剩 30 在账）→ 追冲更正单
    c2 = svc.cancel_session("S3", "复查发现场次未实际开展",
                            approver="审计组", as_of=date(2026, 3, 5))
    print("封账后取消：", c2)
    assert c2["corrections"]

    # 快照原封不动
    snap = svc.get_period(2025)["snapshot"]
    print("2025 不可变快照：", snap)
    assert snap == {"V1": 34, "V3": 30}, snap

    # 有效余额 = 快照 + 封账后更正
    b1 = svc.balance("V1", 2025)
    print("V1 2025：", b1)
    assert b1["settled_snapshot"] == 34
    assert b1["effective_balance"] == 34 - 24 + 20

    # ---------- 审计复算 ----------
    rec2025 = svc.recompute_period(2025)
    print("复算 2025 matches =", rec2025["matches"], rec2025["checks"])
    assert rec2025["matches"], rec2025["checks"]
    rec2026 = svc.recompute_period(2026)
    print("复算 2026 matches =", rec2026["matches"], rec2026["checks"])
    assert rec2026["matches"], rec2026["checks"]
    assert svc.verify_conservation()["ok"]

    # ---------- 逐笔解释 ----------
    any_entry = svc.ledger("V1")[0]["entry_id"]
    explanation = svc.explain_entry(any_entry)
    print("逐笔解释示例：", explanation["basis"])
    for ev in explanation["evidence"][:4]:
        print("   ", ev["key"], "=", ev["value"])

    print("\n全部冒烟断言通过 ✔")


def json_safe(obj):
    import json
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


if __name__ == "__main__":
    main()
