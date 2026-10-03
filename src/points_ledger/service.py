"""领域服务：命令处理、守恒守卫与查询接口。

所有写命令都遵循同一流程：获取串行化写事务 → 重放最新事件 → 校验 →
在同一事务内追加事件 → 提交。读接口（余额、逐笔来源、排名、复算）
全部基于纯函数重放，不缓存可变状态。
"""
from __future__ import annotations

import threading
from collections.abc import Callable

from . import events as ev
from . import projection as P
from .events import Event
from .projection import LedgerState, ProjectionError
from .store import Store


class ServiceError(ValueError):
    """请求违反领域规则（调用方应视为 4xx）。"""


class ConflictError(ServiceError):
    """状态已被并发事务改变（如封账竞争），调用方可重试。"""


class LedgerService:
    def __init__(self, store: Store):
        self.store = store
        # 与存储层锁配合：命令级临界区保证"重放-校验-追加"原子完成。
        self._cmd_lock = threading.RLock()

    # ======================================================================
    # 内部：状态与事务
    # ======================================================================

    def state(self) -> LedgerState:
        return P.replay(self.store.read_events())

    def _commit(self, build: Callable[[LedgerState], list[tuple[str, dict]]],
                ) -> list[Event]:
        """串行化事务：重放最新状态 → build 做服务级校验并产出事件 →
        逐条落库并喂给投影做不变量校验，任一失败整体回滚。"""
        with self._cmd_lock, self.store.write_batch() as append:
            state = P.replay(self.store.read_events())
            produced = build(state)
            committed: list[Event] = []
            for event_type, payload in produced:
                event = append(event_type, payload)
                try:
                    P._apply(state, event)
                except ProjectionError as exc:
                    raise ServiceError(str(exc)) from exc
                committed.append(event)
            return committed

    # ======================================================================
    # 基础档案与期间
    # ======================================================================

    def register_volunteer(self, volunteer_id: str, name: str = "") -> list[Event]:
        def build(state: LedgerState):
            if volunteer_id in state.volunteers:
                raise ServiceError(f"志愿者已存在：{volunteer_id}")
            return [(ev.VOLUNTEER_REGISTERED,
                     {"volunteer_id": volunteer_id, "name": name})]
        return self._commit(build)

    def open_period(self, period: str) -> list[Event]:
        def build(state: LedgerState):
            cur = state.periods.get(period)
            if cur and cur.status == "open":
                raise ServiceError(f"期间 {period} 已开放")
            if cur and cur.status == "closed":
                raise ServiceError(f"期间 {period} 已封账，不得重新开启")
            return [(ev.PERIOD_OPENED, {"period": period})]
        return self._commit(build)

    def close_period(self, period: str) -> dict:
        """封账。并发封账串行排队：先到者封账，后到者得到同一份定格清单。"""
        with self._cmd_lock:
            existing = self.state().periods.get(period)
            if existing is not None and existing.status == "closed":
                # 幂等：重复/并发封账返回同一份清单，不制造第二条封账事件。
                assert existing.snapshot is not None
                return {"period": period, "idempotent": True, "snapshot": existing.snapshot}
            self._commit(lambda s: self._require_open(s, period))
            return {"period": period, "idempotent": False,
                    "snapshot": self.state().periods[period].snapshot}

    @staticmethod
    def _require_open(state: LedgerState, period: str):
        cur = state.periods.get(period)
        if cur is None or cur.status != "open":
            raise ConflictError(f"期间 {period} 未开放，无法封账")
        return [(ev.PERIOD_CLOSED, {"period": period})]

    # ======================================================================
    # 场次 / 报送 / 签到 / 审核
    # ======================================================================

    def register_session(self, session_id: str, service_date: str, name: str = "") -> list[Event]:
        def build(state: LedgerState):
            if session_id in state.sessions:
                raise ServiceError(f"场次已存在：{session_id}")
            ev.year_of(service_date)
            return [(ev.SESSION_STATUS_CHANGED, {
                "session_id": session_id, "service_date": service_date,
                "name": name, "status": ev.SESSION_SCHEDULED})]
        return self._commit(build)

    def mark_session_completed(self, session_id: str) -> list[Event]:
        return self._set_session_status(session_id, ev.SESSION_COMPLETED)

    def cancel_session(self, session_id: str) -> list[Event]:
        """取消场次：投影自动把该场次已入账积分同步冲回。"""
        return self._set_session_status(session_id, ev.SESSION_CANCELLED)

    def _set_session_status(self, session_id: str, status: str) -> list[Event]:
        def build(state: LedgerState):
            session = state.sessions.get(session_id)
            if session is None:
                raise ServiceError(f"未知场次：{session_id}")
            if session["status"] == status:
                return []  # 幂等
            if status == ev.SESSION_SCHEDULED:
                raise ServiceError("场次不能回退到报备状态")
            if session["status"] == ev.SESSION_CANCELLED and status == ev.SESSION_COMPLETED:
                # 仅在申诉成立流程内允许取消后恢复（见 decide_appeal）。
                raise ServiceError("取消场次恢复完成必须经申诉流程")
            return [(ev.SESSION_STATUS_CHANGED,
                     {"session_id": session_id, "status": status,
                      "service_date": session["service_date"]})]
        return self._commit(build)

    def submit_report(self, *, report_id: str, session_id: str, volunteer_id: str,
                      source: str, role: str, hours: float,
                      service_date: str | None = None) -> list[Event]:
        """学校或场馆报送一条服务记录；同一(场次,志愿者)两条来源自动进待确认队列。"""
        def build(state: LedgerState):
            if report_id in state.reports:
                raise ServiceError(f"报送编号已存在：{report_id}")
            if volunteer_id not in state.volunteers:
                raise ServiceError(f"未知志愿者：{volunteer_id}")
            if session_id not in state.sessions:
                raise ServiceError(f"未知场次：{session_id}")
            if source not in ev.SOURCES:
                raise ServiceError("来源必须是 school 或 venue")
            if not isinstance(role, str) or not role:
                raise ServiceError("缺少服务角色")
            try:
                hours_f = float(hours)
            except (TypeError, ValueError):
                raise ServiceError("服务时长必须是数字")
            if hours_f <= 0:
                raise ServiceError("服务时长必须为正")
            return [(ev.REPORT_RECEIVED, {
                "report_id": report_id, "session_id": session_id,
                "volunteer_id": volunteer_id, "source": source,
                "role": role, "hours": hours_f,
                "service_date": service_date or state.sessions[session_id]["service_date"]})]
        return self._commit(build)

    def record_checkin(self, *, checkin_id: str, session_id: str,
                       volunteer_id: str, source: str, at: str) -> list[Event]:
        def build(state: LedgerState):
            key = (session_id, volunteer_id, source)
            if key in state.checkins:
                raise ServiceError("该来源的签到已存在")
            if session_id not in state.sessions:
                raise ServiceError(f"未知场次：{session_id}")
            if volunteer_id not in state.volunteers:
                raise ServiceError(f"未知志愿者：{volunteer_id}")
            if source not in ev.SOURCES:
                raise ServiceError("来源必须是 school 或 venue")
            return [(ev.CHECKIN_RECORDED, {
                "checkin_id": checkin_id, "session_id": session_id,
                "volunteer_id": volunteer_id, "source": source, "at": at})]
        return self._commit(build)

    def review_report(self, report_id: str, decision: str, reviewer: str,
                      note: str = "") -> list[Event]:
        def build(state: LedgerState):
            if report_id not in state.reports:
                raise ServiceError(f"未知报送：{report_id}")
            if decision not in ("approved", "rejected"):
                raise ServiceError("审核结论必须是 approved 或 rejected")
            if not reviewer:
                raise ServiceError("缺少审核人")
            return [(ev.REVIEW_RECORDED, {
                "report_id": report_id, "decision": decision,
                "reviewer": reviewer, "note": note})]
        return self._commit(build)

    # ======================================================================
    # 重复来源裁决
    # ======================================================================

    def pending_duplicates(self) -> list[dict]:
        state = self.state()
        out = []
        for gid, group in state.groups.items():
            if group["status"] != "pending":
                continue
            reports = [self._report_view(state, rid) for rid in group["report_ids"]]
            out.append({"group_id": gid, "status": "pending", "reports": reports})
        return out

    def resolve_duplicate(self, group_id: str, decision: str,
                          winner_report_id: str | None = None,
                          arbiter: str = "", note: str = "") -> list[Event]:
        def build(state: LedgerState):
            group = state.groups.get(group_id)
            if group is None:
                raise ServiceError(f"未知重复组：{group_id}")
            if group["status"] != "pending":
                raise ServiceError(f"重复组 {group_id} 状态为 {group['status']}，不可裁决")
            if decision not in ("duplicate", "distinct"):
                raise ServiceError("裁决必须是 duplicate 或 distinct")
            if decision == "duplicate":
                if not winner_report_id or winner_report_id not in group["report_ids"]:
                    raise ServiceError("裁决 duplicate 必须指定组内有效报送为采信方")
            payload = {"group_id": group_id, "decision": decision,
                       "winner_report_id": winner_report_id,
                       "arbiter": arbiter, "note": note}
            return [(ev.DUPLICATE_RESOLVED, payload)]
        return self._commit(build)

    # ======================================================================
    # 申诉
    # ======================================================================

    def submit_appeal(self, *, appeal_id: str, kind: str, subject_id: str,
                      appellant: str, reason: str) -> list[Event]:
        def build(state: LedgerState):
            if appeal_id in state.appeals:
                raise ServiceError(f"申诉编号已存在：{appeal_id}")
            if kind not in ("duplicate", "revocation", "cancellation", "review"):
                raise ServiceError("申诉类型必须是 duplicate/revocation/cancellation/review")
            self._require_appeal_subject(state, kind, subject_id)
            if not reason:
                raise ServiceError("申诉必须写明理由")
            return [(ev.APPEAL_SUBMITTED, {
                "appeal_id": appeal_id, "kind": kind, "subject_id": subject_id,
                "appellant": appellant, "reason": reason})]
        return self._commit(build)

    @staticmethod
    def _require_appeal_subject(state: LedgerState, kind: str, subject_id: str) -> None:
        if kind == "duplicate":
            group = state.groups.get(subject_id)
            if group is None or group["status"] != "resolved":
                raise ServiceError("只能对已裁决为重复的组提出申诉")
        elif kind == "revocation":
            if subject_id not in state.revocations:
                raise ServiceError("只能对已部分撤销的报送提出申诉")
        elif kind == "cancellation":
            session = state.sessions.get(subject_id)
            if session is None or session["status"] != ev.SESSION_CANCELLED:
                raise ServiceError("只能对已取消的场次提出申诉")
        elif kind == "review":
            review = state.reviews.get(subject_id)
            if review is None or review["decision"] != "rejected":
                raise ServiceError("只能对审核驳回的报送提出申诉")

    def decide_appeal(self, appeal_id: str, decision: str,
                      arbiter: str, note: str = "") -> list[Event]:
        """裁决申诉。成立时余额变动全部走平衡分录，驳回则不动账。"""
        def build(state: LedgerState):
            appeal = state.appeals.get(appeal_id)
            if appeal is None:
                raise ServiceError(f"未知申诉：{appeal_id}")
            if appeal["status"] != "pending":
                raise ServiceError(f"申诉 {appeal_id} 已裁决")
            if decision not in ("upheld", "denied"):
                raise ServiceError("裁决必须是 upheld 或 denied")
            events_out = [(ev.APPEAL_DECIDED, {
                "appeal_id": appeal_id, "decision": decision,
                "arbiter": arbiter, "note": note})]
            if decision == "upheld":
                kind, subject = appeal["kind"], appeal["subject_id"]
                if kind == "cancellation":
                    # 申诉成立：场次恢复完成（取消时的冲回由重新计提补回；
                    # 若服务年度已封账，则自动作为跨年度补提进入开放期间）。
                    events_out.append((ev.SESSION_STATUS_CHANGED, {
                        "session_id": subject, "status": ev.SESSION_COMPLETED}))
                elif kind == "review":
                    events_out.append((ev.REVIEW_RECORDED, {
                        "report_id": subject, "decision": "approved",
                        "reviewer": arbiter,
                        "note": f"申诉 {appeal_id} 成立后审核改判通过"}))
                # duplicate / revocation 的入账由 APPEAL_DECIDED 投影直接完成。
            return events_out
        return self._commit(build)

    # ======================================================================
    # 部分撤销 与 更正单
    # ======================================================================

    def revoke_service(self, *, target_report_id: str, fraction: float,
                       reason: str, voucher_id: str | None = None) -> list[Event]:
        def build(state: LedgerState):
            if target_report_id not in state.reports:
                raise ServiceError(f"未知报送：{target_report_id}")
            try:
                f = float(fraction)
            except (TypeError, ValueError):
                raise ServiceError("撤销比例必须是数字")
            if not 0 < f <= 1:
                raise ServiceError("撤销比例必须在 (0, 1] 区间")
            prior = sum(r["fraction"] for r in state.revocations.get(target_report_id, []))
            if min(prior, 1.0) + f > 1 + 1e-9:
                raise ServiceError("累计撤销比例不得超过 100%")
            active = P._active_accrued(state, target_report_id)
            if active <= 0:
                # 撤销必须立即产生红字冲回分录；场次取消已全额冲回，
                # 若需再撤销应先经申诉恢复场次。
                raise ServiceError("该报送尚无已生效积分，无法撤销")
            if voucher_id and any(
                r.get("voucher_id") == voucher_id
                for rs in state.revocations.values() for r in rs
            ):
                raise ServiceError(f"撤销凭证号重复：{voucher_id}")
            return [(ev.SERVICE_REVOKED, {
                "target_report_id": target_report_id, "fraction": f,
                "reason": reason, "voucher_id": voucher_id})]
        return self._commit(build)

    def issue_correction(self, *, voucher_id: str, target_entry_id: str,
                         volunteer_id: str, delta: int, approver: str,
                         reason: str) -> list[Event]:
        """封账后唯一的调整通道：更正单（自身也是平衡分录）。"""
        def build(state: LedgerState):
            if voucher_id in state.corrections:
                raise ServiceError(f"更正单编号重复：{voucher_id}")
            target = state.entry_by_id.get(target_entry_id)
            if target is None:
                raise ServiceError(f"目标分录不存在：{target_entry_id}")
            period = state.periods.get(target.period)
            if period is None or period.status != "closed":
                raise ServiceError("目标分录所在期间尚未封账，请直接使用冲回/撤销")
            if not isinstance(delta, int) or delta == 0:
                raise ServiceError("更正金额必须是非零整数")
            if state.volunteer_balance(volunteer_id) + delta < 0:
                raise ServiceError("更正后志愿者余额不能为负")
            if not approver or not reason:
                raise ServiceError("更正单需要审批人与理由")
            return [(ev.CORRECTION_VOUCHERED, {
                "voucher_id": voucher_id, "target_entry_id": target_entry_id,
                "volunteer_id": volunteer_id, "delta": delta,
                "approver": approver, "reason": reason})]
        return self._commit(build)

    # ======================================================================
    # 查询：余额 / 逐笔解释 / 排名 / 守恒
    # ======================================================================

    def balance(self, volunteer_id: str) -> dict:
        state = self.state()
        if volunteer_id not in state.volunteers:
            raise ServiceError(f"未知志愿者：{volunteer_id}")
        return {"volunteer_id": volunteer_id,
                "name": state.volunteers[volunteer_id].get("name", ""),
                "balance": state.volunteer_balance(volunteer_id)}

    def account_statement(self, volunteer_id: str, period: str | None = None) -> dict:
        """逐笔解释积分来源：每张分录都给出依据、计分拆解、配对分录与哈希链。"""
        state = self.state()
        if volunteer_id not in state.volunteers:
            raise ServiceError(f"未知志愿者：{volunteer_id}")
        entries = [
            e for e in state.entries
            if e.volunteer_id == volunteer_id and (period is None or e.period == period)
        ]
        return {
            "volunteer_id": volunteer_id,
            "period": period,
            "balance": state.volunteer_balance(volunteer_id),
            "period_total": sum(e.amount for e in entries),
            "entries": [self._explain_entry(state, e) for e in entries],
        }

    def _explain_entry(self, state: LedgerState, entry: P.Entry) -> dict:
        """把一张分录还原成"为什么有这几分"的完整证据链。"""
        report = None
        report_id = entry.evidence.get("report_id")
        if report_id:
            report = state.reports.get(report_id)
        checkin = None
        if report:
            checkin = state.checkins.get(
                (report["session_id"], report["volunteer_id"], report["source"]))
        session = state.sessions.get(entry.evidence.get("session_id")) if report else None
        review = state.reviews.get(report_id) if report_id else None
        source_trace = []
        if session:
            source_trace.append(f"场次「{session['name']}」日期 {session['service_date']} 状态 {session['status']}")
        if checkin:
            source_trace.append(f"{checkin['source']} 签到 {checkin['checkin_id']} @ {checkin['at']}")
        if report:
            source_trace.append(
                f"{report['source']} 报送 {report['report_id']}：角色 {report['role']}，"
                f"{report['hours']} 小时，重复标记 {report['dup']}")
        if review:
            source_trace.append(f"审核 {review['decision']}（{review['reviewer']}，事件#{review['seq']}）")
        if entry.kind == "correction":
            source_trace.append(
                f"更正单 {entry.evidence['voucher_id']}，审批人 {entry.evidence['approver']}，"
                f"调整封账分录 {entry.evidence['target_entry_id']}")
        return {
            **entry.to_dict(),
            "source_trace": source_trace,
            "paired_entries": [
                self._entry_brief(state.entry_by_id[p])
                for p in entry.pairs if p in state.entry_by_id
            ],
        }

    @staticmethod
    def _entry_brief(entry: P.Entry) -> dict:
        return {"entry_id": entry.entry_id, "kind": entry.kind,
                "amount": entry.amount, "period": entry.period,
                "event_seq": entry.event_seq}

    def _report_view(self, state: LedgerState, report_id: str) -> dict:
        r = state.reports[report_id]
        review = state.reviews.get(report_id)
        return {
            "report_id": report_id, "session_id": r["session_id"],
            "source": r["source"], "role": r["role"], "hours": r["hours"],
            "service_period": r["service_period"], "dup_status": r["dup"],
            "review": review["decision"] if review else None,
            "active_points": P._active_accrued(state, report_id),
        }

    def ranking(self, period: str) -> dict:
        """某结算期的排名（只统计已确认积分；挂起/驳回/已冲回不进排名）。"""
        state = self.state()
        totals: dict[str, int] = {}
        for e in state.period_entries(period):
            totals[e.volunteer_id] = totals.get(e.volunteer_id, 0) + e.amount
        rows = [
            {"rank": 0, "volunteer_id": vid,
             "name": state.volunteers.get(vid, {}).get("name", ""),
             "points": pts}
            for vid, pts in totals.items()
            if pts != 0  # 挂起/全额冲回后净额为 0 的志愿者不进排名
        ]
        rows.sort(key=lambda r: (-r["points"], r["volunteer_id"]))
        for i, row in enumerate(rows, 1):
            row["rank"] = i
        ps = state.periods.get(period)
        return {"period": period,
                "period_status": ps.status if ps else "missing",
                "rows": rows, "total_points": sum(r["points"] for r in rows)}

    def conservation_report(self) -> dict:
        """全局守恒报告：所有账户合计必须为 0，哈希链必须连续。"""
        state = self.state()
        info = state.conservation()
        info["hash_chain_ok"] = self._verify_hash_chain(state.entries)
        return info

    # ======================================================================
    # 审计复算
    # ======================================================================

    def recompute_period(self, period: str) -> dict:
        """审计人员复算任一结算期。

        独立重放全部事件（与在线入账同一套纯函数），输出该期逐笔分录、
        逐志愿者合计、封账快照比对、守恒校验与哈希链校验。
        """
        events = self.store.read_events()
        state = P.replay(events)
        ps = state.periods.get(period)
        if ps is None:
            raise ServiceError(f"未知结算期：{period}")

        entries = [e for e in state.entries if e.period == period]
        totals: dict[str, int] = {}
        for e in entries:
            totals[e.volunteer_id] = totals.get(e.volunteer_id, 0) + e.amount

        snapshot = ps.snapshot
        snapshot_match = None
        if snapshot is not None:
            snapshot_match = (
                snapshot["entry_count"] == len(entries)
                and snapshot["volunteer_totals"] == dict(sorted(totals.items()))
            )

        # 仅该期分录的借贷平衡复核（志愿者净额之和 + 基金对冲之和 == 0）。
        period_volunteer_sum = sum(e.amount for e in entries)
        fund_delta = sum(-e.amount for e in entries)

        # 按封账时刻 seq 截止复算：封账后该期合计不应再有任何变化。
        closed_totals: dict[str, int] = {}
        if ps.closed_seq is not None:
            at_close = P.replay(events, as_of_seq=ps.closed_seq)
            for e in at_close.period_entries(period):
                closed_totals[e.volunteer_id] = closed_totals.get(e.volunteer_id, 0) + e.amount

        return {
            "period": period,
            "status": ps.status,
            "opened_seq": ps.opened_seq,
            "closed_seq": ps.closed_seq,
            "recalculated": {
                "entry_count": len(entries),
                "volunteer_totals": dict(sorted(totals.items())),
                "period_sum": period_volunteer_sum,
                "fund_offset": fund_delta,
                "balanced": period_volunteer_sum + fund_delta == 0,
            },
            "sealed_snapshot": snapshot,
            "snapshot_matches_recalculation": snapshot_match,
            "totals_unchanged_since_close": (
                closed_totals == totals if ps.status == "closed" else None),
            "hash_chain_ok": self._verify_hash_chain(state.entries),
            "global_conservation": state.conservation()["all_accounts_total"] == 0,
            "entries": [self._explain_entry(state, e) for e in entries],
        }

    @staticmethod
    def _verify_hash_chain(entries: list[P.Entry]) -> bool:
        prev = "0" * 64
        for e in entries:
            if e.prev_hash != prev:
                return False
            prev = e.hash
        return True
