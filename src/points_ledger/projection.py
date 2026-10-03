"""纯函数投影：从不可变事件流确定性重建积分分录。

积分守恒
========
每次积分变动都生成一张 *借贷平衡* 的复式分录：志愿者账户 ``v:<id>`` 与系统
基金账户 :data:`SYS_FUND` 金额相反、合计为零。因此任意时刻
``sum(所有账户余额) == 0``；冲回、更正、申诉改判都不过是新增配对分录，
余额永远守恒。

确定性复算
==========
:func:`replay` 是纯函数：只依赖事件序列（``seq`` 给出全局唯一顺序），
不读时钟、不做随机选择。在线入账与审计复算调用的是同一份代码，审计人员
复算任一结算期时只需截取事件区间重新重放即可。

重复报送
========
同一 ``(场次, 志愿者)`` 出现学校与场馆两条报送时自动成组 *挂起*：
裁决前不产生任何积分；裁决"重复"则仅采信胜者，裁决"确属不同服务"则双双释放。

封账
====
分录只能落入重放时刻 *处于开放状态* 的期间。原服务期间已封账时，冲回/补录/
更正一律记入当前开放期间，并用 ``service_period`` 标注原始服务年度——
封账期间的合计数永不改变，跨年度调整在新期间留痕。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from . import events as ev

SYS_FUND = "sys_fund"

# --- 计分规则（集中维护，保证全局唯一口径） ---------------------------------
BASE_POINT_PER_HOUR = Decimal(10)
ROLE_MULTIPLIER: dict[str, Decimal] = {
    "讲解": Decimal("1.2"),
    "引导": Decimal("1.0"),
    "安保协助": Decimal("1.1"),
    "库房协助": Decimal("1.0"),
}
DEFAULT_ROLE_MULTIPLIER = Decimal("1.0")


class ProjectionError(RuntimeError):
    """事件流与领域不变量冲突（正常应被服务层提前拦截）。"""


# ---------------------------------------------------------------------------
# 输出结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntryLine:
    account: str
    amount: int  # 整数积分；志愿者账户正数=积分增加，负数=冲回
    note: str = ""


@dataclass(frozen=True, slots=True)
class Entry:
    """一张借贷平衡的积分分录。"""

    entry_id: str
    volunteer_id: str
    period: str  # 记账期间（分录落入的开放期间）
    service_period: str  # 原始服务发生期间（跨年度补录时与 period 不同）
    kind: str  # accrual 计提 / late_accrual 跨年度补提 / reversal 冲回 / correction 更正单
    amount: int  # 志愿者账户净额（带符号）
    reason: str
    evidence: dict  # 逐笔来源证据：report/session/checkin/review/voucher/事件 seq
    breakdown: dict  # 计分拆解：hours/rate/multiplier
    pairs: tuple[str, ...]  # 配对的原分录号（冲回/更正指向被调整分录）
    event_seq: int
    created_at: str
    prev_hash: str
    hash: str

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "volunteer_id": self.volunteer_id,
            "period": self.period,
            "service_period": self.service_period,
            "kind": self.kind,
            "amount": self.amount,
            "reason": self.reason,
            "evidence": self.evidence,
            "breakdown": self.breakdown,
            "pairs": list(self.pairs),
            "event_seq": self.event_seq,
            "created_at": self.created_at,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }


@dataclass
class PeriodState:
    period: str
    status: str = "open"  # open / closed
    opened_seq: int | None = None
    closed_seq: int | None = None
    closed_at: str | None = None
    snapshot: dict | None = None  # 封账瞬间的清单（此后永不改变）


@dataclass
class LedgerState:
    """重放产物：当前全部台账事实。"""

    volunteers: dict = field(default_factory=dict)
    periods: dict[str, PeriodState] = field(default_factory=dict)
    open_periods: set = field(default_factory=set)
    sessions: dict = field(default_factory=dict)
    checkins: dict = field(default_factory=dict)  # (session, volunteer, source) -> dict
    reports: dict = field(default_factory=dict)
    reports_by_session: dict = field(default_factory=lambda: {})
    reviews: dict = field(default_factory=dict)  # report_id -> 最新审核记录
    groups: dict = field(default_factory=dict)  # group_id -> 组状态
    appeals: dict = field(default_factory=dict)
    revocations: dict = field(default_factory=lambda: {})  # report_id -> [记录]
    reinstated: dict = field(default_factory=lambda: {})  # revocation_id -> 已恢复积分
    appeal_overrides: dict = field(default_factory=lambda: {})  # report_id -> 申诉成立(seq)
    corrections: dict = field(default_factory=lambda: {})  # voucher_id -> 分录
    entries: list[Entry] = field(default_factory=list)
    entry_by_id: dict = field(default_factory=dict)
    entries_by_report: dict = field(default_factory=lambda: {})
    balances: dict[str, int] = field(default_factory=lambda: {SYS_FUND: 0})
    last_seq: int = 0
    last_event_at: str | None = None
    _emission_no: int = 0
    _prev_hash: str = "0" * 64

    # --- 便捷读模型 --------------------------------------------------------

    def volunteer_balance(self, volunteer_id: str) -> int:
        return self.balances.get(f"v:{volunteer_id}", 0)

    def period_entries(self, period: str) -> list[Entry]:
        return [e for e in self.entries if e.period == period]

    def conservation(self) -> dict:
        """全账户合计，恒为 0 才算守恒。"""
        total = sum(self.balances.values())
        volunteer_total = sum(v for a, v in self.balances.items() if a.startswith("v:"))
        return {
            "all_accounts_total": total,
            "volunteers_total": volunteer_total,
            "fund_total": self.balances.get(SYS_FUND, 0),
            "entry_count": len(self.entries),
        }


# ---------------------------------------------------------------------------
# 计分
# ---------------------------------------------------------------------------


def points_for(role: str, hours: float) -> tuple[int, dict]:
    """按角色系数与时长计算积分（整数，四舍五入，全程 Decimal 避免浮点误差）。"""
    mult = ROLE_MULTIPLIER.get(role, DEFAULT_ROLE_MULTIPLIER)
    raw = (Decimal(str(hours)) * BASE_POINT_PER_HOUR * mult).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    breakdown = {
        "hours": str(hours),
        "base_points_per_hour": str(BASE_POINT_PER_HOUR),
        "role": role,
        "role_multiplier": str(mult),
        "points": int(raw),
    }
    return int(raw), breakdown


# ---------------------------------------------------------------------------
# 重放
# ---------------------------------------------------------------------------


def replay(events: list[ev.Event], as_of_seq: int | None = None) -> LedgerState:
    """按 ``seq`` 顺序重放事件，重建台账。``as_of_seq`` 用于历史复算。"""
    state = LedgerState()
    ordered = sorted(events, key=lambda e: e.seq)
    for event in ordered:
        if as_of_seq is not None and event.seq > as_of_seq:
            break
        _apply(state, event)
    return state


def _accounting_period(state: LedgerState, service_period: str) -> str:
    """决定一条新分录落入哪个期间：原期间开放则归原期间，否则进最早的开放期间。"""
    if service_period in state.open_periods:
        return service_period
    if not state.open_periods:
        raise ProjectionError(f"服务期间 {service_period} 已封账且当前无开放期间，无法记账")
    return min(state.open_periods)


def _emit(
    state: LedgerState,
    *,
    event: ev.Event,
    volunteer_id: str,
    service_period: str,
    kind: str,
    amount: int,
    reason: str,
    evidence: dict,
    breakdown: dict | None = None,
    pairs: tuple[str, ...] = (),
) -> Entry:
    """生成并过账一张平衡分录；amount 为志愿者账户的带符号净额。"""
    if amount == 0:
        raise ProjectionError("零金额分录不允许入账")
    period = _accounting_period(state, service_period)
    state._emission_no += 1
    entry_id = f"E{state._emission_no:06d}"
    account = f"v:{volunteer_id}"
    lines = [
        EntryLine(account=account, amount=amount, note=reason),
        EntryLine(account=SYS_FUND, amount=-amount, note="系统积分基金对冲"),
    ]
    assert sum(line.amount for line in lines) == 0, "分录借贷不平衡"

    body = {
        "entry_id": entry_id,
        "volunteer_id": volunteer_id,
        "period": period,
        "service_period": service_period,
        "kind": kind,
        "amount": amount,
        "reason": reason,
        "evidence": evidence,
        "breakdown": breakdown or {},
        "pairs": tuple(pairs),
        "event_seq": event.seq,
        "created_at": event.created_at,
    }
    digest = hashlib.sha256(
        (state._prev_hash + "\n" + json.dumps(body, ensure_ascii=False, sort_keys=True)).encode("utf-8")
    ).hexdigest()
    entry = Entry(prev_hash=state._prev_hash, hash=digest, **body)

    state.entries.append(entry)
    state.entry_by_id[entry_id] = entry
    state.balances[account] = state.balances.get(account, 0) + amount
    state.balances[SYS_FUND] = state.balances.get(SYS_FUND, 0) - amount
    state._prev_hash = digest
    return entry


def _active_accrued(state: LedgerState, report_id: str) -> int:
    """某报送当前仍生效的计提净额（计提 - 冲回 + 申诉恢复）。"""
    total = 0
    for e in state.entries_by_report.get(report_id, ()):
        if e.kind in ("accrual", "late_accrual", "reversal", "reinstatement"):
            total += e.amount  # reversal 自身为负
    return total


def _try_accrue(state: LedgerState, report_id: str, event: ev.Event) -> Entry | None:
    """满足全部入账条件时为报送计提积分，否则返回 None（继续等待）。"""
    report = state.reports.get(report_id)
    if report is None:
        return None
    if report["dup"] != "accepted":
        return None  # 重复来源挂起中或已被判败
    review = state.reviews.get(report_id)
    if not review or review["decision"] != "approved":
        return None  # 审核未通过
    session = state.sessions.get(report["session_id"])
    if not session or session["status"] != ev.SESSION_COMPLETED:
        return None  # 场次未完成（取消的场次不得计提）
    if _active_accrued(state, report_id) != 0:
        return None  # 已计提且仍生效，防重复

    base, breakdown = points_for(report["role"], report["hours"])
    # 已查实且未被申诉推翻的部分撤销，不随（重新）计提补回。
    # 以撤销当时实际冲回额为准，与历史红字分录严格一致。
    reinstated = state.reinstated.get(report_id, {})
    deduction = sum(
        rec["reversed_amount"]
        for idx, rec in enumerate(state.revocations.get(report_id, ()))
        if idx not in reinstated
    )
    amount = base - deduction
    if amount <= 0:
        return None
    breakdown["base_points"] = base
    breakdown["deducted_revoked_points"] = deduction
    kind = "accrual" if report["service_period"] in state.open_periods else "late_accrual"
    checkin = state.checkins.get(
        (report["session_id"], report["volunteer_id"], report["source"])
    )
    entry = _emit(
        state,
        event=event,
        volunteer_id=report["volunteer_id"],
        service_period=report["service_period"],
        kind=kind,
        amount=amount,
        reason=f"{session['name']}·{report['role']}服务计提（{report['source']}报送）",
        evidence={
            "report_id": report_id,
            "session_id": report["session_id"],
            "checkin_id": checkin["checkin_id"] if checkin else None,
            "review_seq": review["seq"],
            "reviewer": review["reviewer"],
            "group_id": report["group_id"],
        },
        breakdown=breakdown,
    )
    state.entries_by_report.setdefault(report_id, []).append(entry)
    return entry


def _reverse(state: LedgerState, report_id: str, amount: int, reason: str,
             event: ev.Event, evidence_extra: dict | None = None) -> Entry | None:
    """对指定报送冲回 ``amount``（正数）积分；不足或无生效计提则报错。"""
    report = state.reports[report_id]
    active = _active_accrued(state, report_id)
    if amount > active:
        raise ProjectionError(
            f"冲回 {amount} 分超过报送 {report_id} 仍生效的 {active} 分"
        )
    if amount <= 0:
        raise ProjectionError("冲回金额必须为正")
    original = [
        e for e in state.entries_by_report.get(report_id, ())
        if e.kind in ("accrual", "late_accrual")
    ]
    pair_ids = tuple(e.entry_id for e in original)
    evidence = {
        "report_id": report_id,
        "session_id": report["session_id"],
        "revocation_seq": event.seq,
    }
    if evidence_extra:
        evidence.update(evidence_extra)
    entry = _emit(
        state,
        event=event,
        volunteer_id=report["volunteer_id"],
        service_period=report["service_period"],
        kind="reversal",
        amount=-amount,
        reason=reason,
        evidence=evidence,
        breakdown={"reversed_points": amount, "original_pairs": list(pair_ids)},
        pairs=pair_ids,
    )
    state.entries_by_report.setdefault(report_id, []).append(entry)
    return entry


# ---------------------------------------------------------------------------
# 各类事件
# ---------------------------------------------------------------------------


def _apply(state: LedgerState, event: ev.Event) -> None:
    state.last_seq = event.seq
    state.last_event_at = event.created_at
    p = event.payload
    handler = _HANDLERS.get(event.type)
    if handler is None:
        raise ProjectionError(f"未知事件类型：{event.type}")
    handler(state, event, p)


def _h_volunteer(state, event, p):
    if p["volunteer_id"] in state.volunteers:
        raise ProjectionError(f"志愿者重复注册：{p['volunteer_id']}")
    state.volunteers[p["volunteer_id"]] = {"name": p.get("name", "")}
    state.balances.setdefault(f"v:{p['volunteer_id']}", 0)


def _h_period_open(state, event, p):
    period = p["period"]
    cur = state.periods.get(period)
    if cur and cur.status == "open":
        raise ProjectionError(f"期间 {period} 已处于开放状态")
    if cur and cur.status == "closed":
        # 已封账期间永不重新打开；跨年度调整只能走更正单/补提进入新期间。
        raise ProjectionError(f"期间 {period} 已封账，不得重新开启")
    ps = PeriodState(period=period, status="open", opened_seq=event.seq)
    state.periods[period] = ps
    state.open_periods.add(period)


def _h_period_close(state, event, p):
    period = p["period"]
    cur = state.periods.get(period)
    if cur is None or cur.status != "open":
        raise ProjectionError(f"期间 {period} 未开放，无法封账")
    cur.status = "closed"
    cur.closed_seq = event.seq
    cur.closed_at = event.created_at
    state.open_periods.discard(period)
    # 封账清单：定格的逐志愿者合计与分录哈希链末端，供之后任何时刻核对。
    totals: dict[str, int] = {}
    for e in state.period_entries(period):
        totals[e.volunteer_id] = totals.get(e.volunteer_id, 0) + e.amount
    cur.snapshot = {
        "period": period,
        "closed_seq": event.seq,
        "closed_at": event.created_at,
        "entry_count": len(state.period_entries(period)),
        "volunteer_totals": dict(sorted(totals.items())),
        "entries_hash": state._prev_hash,
    }


def _h_session(state, event, p):
    sid = p["session_id"]
    old = state.sessions.get(sid)
    old_status = old["status"] if old else None
    new_status = p["status"]
    if new_status not in ev.SESSION_STATUSES:
        raise ProjectionError(f"未知场次状态：{new_status}")
    if old is None:
        state.sessions[sid] = {
            "session_id": sid,
            "name": p.get("name", sid),
            "service_date": p["service_date"],
            "service_period": ev.year_of(p["service_date"]),
            "status": new_status,
        }
        state.reports_by_session.setdefault(sid, [])
    else:
        old["status"] = new_status
        if p.get("name"):
            old["name"] = p["name"]

    if old_status == new_status:
        return
    session = state.sessions[sid]
    if new_status == ev.SESSION_COMPLETED:
        # 场次完成：释放所有已满足条件的报送（若期间已封账，自动成为跨年度补提）。
        for rid in list(state.reports_by_session.get(sid, ())):
            _try_accrue(state, rid, event)
    elif new_status == ev.SESSION_CANCELLED:
        # 取消场次：已入账积分必须逐笔同步全额冲回（部分撤销过的只冲剩余部分）。
        for rid in list(state.reports_by_session.get(sid, ())):
            active = _active_accrued(state, rid)
            if active > 0:
                _reverse(
                    state, rid, active,
                    f"场次「{session['name']}」取消，同步冲回已计提积分",
                    event,
                    {"cause": "session_cancelled"},
                )


def _h_report(state, event, p):
    rid = p["report_id"]
    if rid in state.reports:
        raise ProjectionError(f"报送编号重复：{rid}")
    if p["volunteer_id"] not in state.volunteers:
        raise ProjectionError(f"报送引用未注册志愿者：{p['volunteer_id']}")
    if p["source"] not in ev.SOURCES:
        raise ProjectionError(f"未知报送来源：{p['source']}")
    session = state.sessions.get(p["session_id"])
    service_period = session["service_period"] if session else ev.year_of(p["service_date"])
    key = ev.dedup_key(p["session_id"], p["volunteer_id"])
    group_id = ev.group_id_for(p["session_id"], p["volunteer_id"])

    report = {
        "report_id": rid,
        "session_id": p["session_id"],
        "volunteer_id": p["volunteer_id"],
        "source": p["source"],
        "role": p["role"],
        "hours": p["hours"],
        "service_date": p.get("service_date") or (session["service_date"] if session else None),
        "service_period": service_period,
        "dup": "accepted",
        "group_id": None,
        "seq": event.seq,
    }
    state.reports[rid] = report
    state.reports_by_session.setdefault(p["session_id"], []).append(rid)

    group = state.groups.get(group_id)
    if group is None:
        state.groups[group_id] = {
            "group_id": group_id,
            "key": key,
            "report_ids": [rid],
            "status": "single",
            "winner": None,
        }
    else:
        if group["status"] != "single":
            # pending / resolved / distinct / overturned 的组均已锁定，
            # 新报送应指向不同场次或先经更正流程。
            raise ProjectionError(
                f"重复组 {group_id} 状态为 {group['status']}，不接受新报送")
        # 第二条（及以上）来源出现：整组进入待确认队列。组内任何已先行计提的
        # 积分都必须立即冲回挂起——待确认期间不得有积分计入排名与兑换额度。
        for member_id in group["report_ids"]:
            active = _active_accrued(state, member_id)
            if active > 0:
                _reverse(state, member_id, active,
                         f"重复来源待确认（{group_id}），积分挂起冲回",
                         event, {"cause": "duplicate_pending", "group_id": group_id})
        group["report_ids"].append(rid)
        group["status"] = "pending"
        for member_id in group["report_ids"]:
            state.reports[member_id]["dup"] = "held"
        report["dup"] = "held"
        report["group_id"] = group_id
        for member_id in group["report_ids"]:
            state.reports[member_id]["group_id"] = group_id


def _h_checkin(state, event, p):
    key = (p["session_id"], p["volunteer_id"], p["source"])
    if key in state.checkins:
        raise ProjectionError(f"重复签到：{p['checkin_id']}")
    state.checkins[key] = dict(p, seq=event.seq)


def _h_review(state, event, p):
    rid = p["report_id"]
    if rid not in state.reports:
        raise ProjectionError(f"审核引用未知报送：{rid}")
    if p["decision"] not in ("approved", "rejected"):
        raise ProjectionError(f"未知审核结论：{p['decision']}")
    state.reviews[rid] = {
        "report_id": rid,
        "decision": p["decision"],
        "reviewer": p["reviewer"],
        "note": p.get("note", ""),
        "seq": event.seq,
    }
    if p["decision"] == "approved":
        _try_accrue(state, rid, event)


def _h_duplicate(state, event, p):
    gid = p["group_id"]
    group = state.groups.get(gid)
    if group is None:
        raise ProjectionError(f"未知重复组：{gid}")
    if group["status"] == "resolved":
        raise ProjectionError(f"重复组 {gid} 已裁决")
    decision = p["decision"]
    if decision == "duplicate":
        winner = p["winner_report_id"]
        if winner not in group["report_ids"]:
            raise ProjectionError("胜选报送不属于该重复组")
        group["status"] = "resolved"
        group["winner"] = winner
        for rid in group["report_ids"]:
            state.reports[rid]["dup"] = "accepted" if rid == winner else "rejected"
        # 败者若曾（理论上不可能，因挂起机制）先行计提，立即冲回。
        for rid in group["report_ids"]:
            if rid != winner:
                active = _active_accrued(state, rid)
                if active > 0:
                    _reverse(state, rid, active, "重复报送裁决败选，冲回", event,
                             {"cause": "duplicate_loser", "group_id": gid})
        _try_accrue(state, winner, event)
    elif decision == "distinct":
        # 两条报送经核验确属不同服务（不同时段/岗位），双双放行。
        group["status"] = "distinct"
        for rid in group["report_ids"]:
            state.reports[rid]["dup"] = "accepted"
            _try_accrue(state, rid, event)
    else:
        raise ProjectionError(f"未知重复裁决：{decision}")


def _h_appeal_submit(state, event, p):
    aid = p["appeal_id"]
    if aid in state.appeals:
        raise ProjectionError(f"申诉编号重复：{aid}")
    state.appeals[aid] = dict(p, status="pending", submitted_seq=event.seq, decision_seq=None)


def _h_appeal_decide(state, event, p):
    aid = p["appeal_id"]
    appeal = state.appeals.get(aid)
    if appeal is None:
        raise ProjectionError(f"未知申诉：{aid}")
    if appeal["status"] != "pending":
        raise ProjectionError(f"申诉 {aid} 已裁决")
    if p["decision"] not in ("upheld", "denied"):
        raise ProjectionError(f"未知申诉裁决：{p['decision']}")
    appeal["status"] = p["decision"]
    appeal["decision_seq"] = event.seq
    appeal["arbiter"] = p.get("arbiter", "")
    appeal["note"] = p.get("note", "")
    if p["decision"] != "upheld":
        return  # 驳回不动账，余额自然不变

    kind = appeal.get("kind")
    if kind == "duplicate":
        # 撤销"重复"裁决：改为按不同服务处理，原败者报送放行（期间已封账则跨年度补提）。
        gid = appeal["subject_id"]
        group = state.groups.get(gid)
        if group is None or group["status"] != "resolved":
            raise ProjectionError(f"申诉 {aid} 的重复组不存在或未裁决为重复")
        group["status"] = "overturned"
        for rid in group["report_ids"]:
            state.reports[rid]["dup"] = "accepted"
            state.appeal_overrides[rid] = event.seq
            _try_accrue(state, rid, event)
    elif kind == "revocation":
        # 撤销"部分撤销"：把已冲回积分按原口径恢复，恢复单与冲回单一一配对。
        rid = appeal["subject_id"]
        if rid not in state.reports:
            raise ProjectionError(f"申诉 {aid} 引用未知报送")
        session = state.sessions[state.reports[rid]["session_id"]]
        if session["status"] == ev.SESSION_CANCELLED:
            raise ProjectionError(
                f"申诉 {aid}：场次仍处于取消状态，请先通过场次取消申诉恢复")
        revoked = state.revocations.get(rid, [])
        already = state.reinstated.get(rid, {})
        restored_any = False
        for idx, rec in enumerate(revoked):
            if idx in already:
                continue
            amount = rec["reversed_amount"]
            if amount <= 0:
                # 撤销先于计提登记、自身未产生冲回分录：标记后，
                # 后续（重新）计提时不再扣减该比例。
                already[idx] = 0
                continue
            entry = _emit(
                state, event=event,
                volunteer_id=state.reports[rid]["volunteer_id"],
                service_period=state.reports[rid]["service_period"],
                kind="reinstatement",
                amount=amount,
                reason=f"申诉 {aid} 成立，恢复撤销积分：{rec.get('reason', '')}",
                evidence={"appeal_id": aid, "report_id": rid,
                          "revocation_seq": rec["seq"], "arbiter": p.get("arbiter", "")},
                breakdown={"reinstated_points": amount, "fraction": rec["fraction"]},
                pairs=tuple(
                    e.entry_id for e in state.entries_by_report.get(rid, ())
                    if e.kind == "reversal"
                ),
            )
            state.entries_by_report.setdefault(rid, []).append(entry)
            already[idx] = amount
            restored_any = True
        state.reinstated[rid] = already
        if not restored_any and not already:
            raise ProjectionError(f"申诉 {aid} 成立但没有可恢复的冲回积分")
    elif kind in ("cancellation", "review"):
        # 场次取消/审核驳回类申诉成立后，由服务层追加规范的"场次恢复完成"或
        # "审核改判通过"事件入账，投影按常规定则重新计提，无需特殊改账分支。
        state.appeal_overrides[appeal["subject_id"]] = event.seq
    else:
        raise ProjectionError(f"未知申诉类型：{kind}")


def _reversed_net(state: LedgerState, report_id: str) -> int:
    """某报送当前未被恢复的冲回净额（负数或 0）。"""
    total = 0
    for e in state.entries_by_report.get(report_id, ()):
        if e.kind == "reversal":
            total += e.amount
        elif e.kind == "reinstatement":
            total += e.amount
    return total


def _h_revoke(state, event, p):
    rid = p["target_report_id"]
    if rid not in state.reports:
        raise ProjectionError(f"部分撤销引用未知报送：{rid}")
    fraction = p["fraction"]
    if not 0 < fraction <= 1:
        raise ProjectionError("撤销比例必须在 (0, 1] 区间")
    prior = sum(r["fraction"] for r in state.revocations.get(rid, []))
    # 场次取消导致的全额冲回也计入累计撤销比例，避免重复冲减。
    cancelled = state.sessions[state.reports[rid]["session_id"]]["status"] == ev.SESSION_CANCELLED
    used = min(prior, 1.0)
    if used + fraction > 1 + 1e-9:
        raise ProjectionError(f"累计撤销比例超过 100%：{used + fraction}")
    # 以原始计提额为基数计算本次应冲回（整数分摊）。
    original_total = sum(
        e.amount for e in state.entries_by_report.get(rid, ())
        if e.kind in ("accrual", "late_accrual")
    )
    want = int((Decimal(str(original_total)) * Decimal(str(fraction))).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))
    reversed_amount = 0
    if not cancelled and original_total > 0:
        reversed_amount = min(want, _active_accrued(state, rid))
        if reversed_amount > 0:
            _reverse(state, rid, reversed_amount,
                     f"部分撤销（{fraction:.0%}）：{p.get('reason', '')}",
                     event, {"cause": "partial_revocation", "fraction": fraction})
    state.revocations.setdefault(rid, []).append(
        {"fraction": fraction, "seq": event.seq, "reason": p.get("reason", ""),
         "voucher_id": p.get("voucher_id"), "reversed_amount": reversed_amount})


def _h_correction(state, event, p):
    voucher_id = p["voucher_id"]
    if voucher_id in state.corrections:
        raise ProjectionError(f"更正单编号重复：{voucher_id}")
    target = state.entry_by_id.get(p["target_entry_id"])
    if target is None:
        raise ProjectionError(f"更正单引用未知分录：{p['target_entry_id']}")
    if target.volunteer_id != p["volunteer_id"]:
        raise ProjectionError("更正单志愿者与目标分录不一致")
    delta = int(p["delta"])
    if delta == 0:
        raise ProjectionError("更正单金额不能为零")
    target_period = state.periods.get(target.period)
    if target_period is None or target_period.status != "closed":
        raise ProjectionError("仅已封账期间的分录允许使用更正单调整（开放期间请直接冲回/补提）")
    balance = state.volunteer_balance(p["volunteer_id"])
    if balance + delta < 0:
        raise ProjectionError(f"更正后志愿者余额为负：{balance} {delta:+d}")
    entry = _emit(
        state,
        event=event,
        volunteer_id=p["volunteer_id"],
        service_period=target.service_period,
        kind="correction",
        amount=delta,
        reason=f"更正单 {voucher_id}：{p.get('reason', '')}",
        evidence={
            "voucher_id": voucher_id,
            "target_entry_id": target.entry_id,
            "approver": p["approver"],
        },
        breakdown={"delta": delta, "target_period": target.period},
        pairs=(target.entry_id,),
    )
    state.corrections[voucher_id] = entry


_HANDLERS = {
    ev.VOLUNTEER_REGISTERED: _h_volunteer,
    ev.PERIOD_OPENED: _h_period_open,
    ev.PERIOD_CLOSED: _h_period_close,
    ev.SESSION_STATUS_CHANGED: _h_session,
    ev.REPORT_RECEIVED: _h_report,
    ev.CHECKIN_RECORDED: _h_checkin,
    ev.REVIEW_RECORDED: _h_review,
    ev.DUPLICATE_RESOLVED: _h_duplicate,
    ev.APPEAL_SUBMITTED: _h_appeal_submit,
    ev.APPEAL_DECIDED: _h_appeal_decide,
    ev.SERVICE_REVOKED: _h_revoke,
    ev.CORRECTION_VOUCHERED: _h_correction,
}
if False:
    pass
