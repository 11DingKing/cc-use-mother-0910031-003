"""应用服务层：积分结算的全部业务规则与余额守恒保障。

所有写操作都在存储锁内完成"读取—校验—落账"，可并发调用；
封账通过条件变量串行化，重复封账幂等返回同一张快照。

记账模型
--------

1. 分录不可变；冲回、撤销、更正永远通过新增对冲分录实现，
   余额 = 已入账分录之和，任意时点可逐笔重放。
2. 同一 (志愿者, 场次) 出现多个报送渠道即判定为重复报送：
   该组现有积分立即以红字"挂起"（净额归 0），整组进入待确认队列；
   核对结论（确认其一 / 驳回 / 各自成立）落审核记录后，按目标净额
   与当前净额的差额做一次性"结算补记/红冲"，因此任何处理顺序下
   最终净额都只取决于核对结论。
3. 未封账年度可直接红冲、补记；封账时拍不可变快照。封账后的调整
   只能走更正单：红字/蓝字成对、需审批，一律记入之后的开放期，
   原年度快照保持不变。
4. 审计复算从签到、场次状态、审核结论等原始事实独立重算每个
   (志愿者, 场次) 组的净额，与逐笔账本、封账快照双向核对，
   并单独复核全部更正单的红/蓝算术与引用完整性。
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Optional

from . import engine
from .errors import Conflict, NotFound, PeriodClosed, PreconditionFailed, ValidationError
from .models import (
    Checkin,
    Correction,
    Entry,
    EntryStatus,
    PendingItem,
    Review,
    ReviewDecision,
    ReversalRecord,
    Role,
    Session,
    SessionStatus,
    SourceKind,
)
from .storage import Store


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class LedgerService:
    """积分结算应用服务，线程安全。"""

    def __init__(self, store: Optional[Store] = None) -> None:
        # 允许调用方传入自定义存储；缺省使用线程安全内存存储
        if store is None:
            from .storage import InMemoryStore
            store = InMemoryStore()
        self.store: Store = store
        # 运行期索引：哪些签到已经生成过正向分录（简单路径幂等用）
        self._posted_checkins: dict[str, str] = {}
        # 申诉档：appeal_id -> 申诉字典
        self._appeals: dict[str, dict] = {}

    # ================================================================ 场次

    def register_session(
        self,
        session_id: str,
        activity_id: str,
        service_date: date,
        hours: int,
        *,
        status: SessionStatus = SessionStatus.PLANNED,
        plan_year: Optional[int] = None,
    ) -> Session:
        if hours <= 0:
            raise ValidationError("服务小时数必须为正整数")
        if plan_year is not None and plan_year > service_date.year:
            raise ValidationError("计划年度不能晚于服务日期所在年度")
        with self.store.lock:
            old = self.store.get_session(session_id)
            if old is not None and (
                old.activity_id != activity_id
                or old.service_date != service_date
                or old.hours != hours
            ):
                raise Conflict(f"场次 {session_id} 的活动/日期/小时数不可变更")
            session = Session(session_id, activity_id, service_date, hours, status, plan_year)
            self.store.upsert_session(session)
            return session

    def mark_session_held(
        self,
        session_id: str,
        *,
        on_date: Optional[date] = None,
    ) -> dict:
        """场次正常举办：为其下全部签到做计提评估。"""
        with self.store.lock:
            session = self._require_session(session_id)
            if session.status is SessionStatus.CANCELLED:
                raise Conflict("已取消的场次不能标记为举办")
            session = Session(
                session.session_id, session.activity_id, session.service_date,
                session.hours, SessionStatus.HELD, session.plan_year,
            )
            self.store.upsert_session(session)
            # 处理日期默认取"今天"而非历史服务日期：若归属年度已封账，
            # 迟到的举办确认必须把积分经更正单记入当前开放期。
            return self._evaluate_session(session, on_date or date.today())

    def cancel_session(
        self,
        session_id: str,
        reason: str,
        *,
        approver: str = "system",
        as_of: Optional[date] = None,
    ) -> dict:
        """取消场次，已发积分必须同步冲回。

        - 归属年度未封账：逐笔红字冲回；
        - 归属年度已封账：逐笔生成更正单（纯红字），记入当前开放期，
          原年度快照不变；
        - 尚在待确认队列的组直接按取消结案（净额本就为 0）。
        """
        on_date = as_of or date.today()
        if not reason:
            raise ValidationError("取消场次必须填写原因")
        with self.store.lock:
            session = self._require_session(session_id)
            session = Session(
                session.session_id, session.activity_id, session.service_date,
                session.hours, SessionStatus.CANCELLED, session.plan_year,
            )
            self.store.upsert_session(session)

            reversals: list[str] = []
            corrections: list[str] = []
            for entry in list(self.store.iter_entries()):
                if entry.session_id != session_id:
                    continue
                if entry.kind not in (SourceKind.SERVICE, SourceKind.LATE):
                    continue
                remainder = self._entry_live_remainder(entry)
                if remainder <= 0:
                    continue
                period = self.store.get_period(str(entry.year))
                if period is not None and period["closed"]:
                    corr = self._create_correction_locked(
                        volunteer_id=entry.volunteer_id,
                        original_entry=entry,
                        red_amount=remainder,
                        reason=f"场次 {session_id} 取消，追冲积分：{reason}",
                        approver=approver,
                        as_of=on_date,
                        origin="cancel",
                    )
                    corrections.append(corr.correction_id)
                else:
                    rec = self._reverse_entries_locked(
                        [(entry, remainder)],
                        reason=f"场次 {session_id} 取消：{reason}",
                        on_date=on_date, approver=approver,
                        origin="cancel",
                    )[0]
                    reversals.append(rec.reversal_id)

            cancelled_pending = []
            for item in self.store.list_pending():
                if item.session_id == session_id:
                    self.store.resolve_pending(
                        item.pending_id, (),
                        json.dumps({"decision": "cancelled", "effective": []},
                                   ensure_ascii=False))
                    cancelled_pending.append(item.pending_id)

            return {
                "session_id": session_id,
                "status": session.status.value,
                "reversals": reversals,
                "corrections": corrections,
                "pending_cancelled": cancelled_pending,
            }

    # ================================================================ 签到与重复报送

    def ingest_checkin(
        self,
        checkin_id: str,
        volunteer_id: str,
        session_id: str,
        role: Role,
        source: str,
        checkin_time: Optional[str] = None,
    ) -> dict:
        """录入一笔签到报送。

        返回结果：
        - ``posted``          场次已举办、无重复，直接入账；
        - ``pending``         疑似重复报送，整组已挂起进入待确认队列；
        - ``waiting_session`` 场次尚未举办，先登记后等场次状态；
        - ``duplicate_import`` 同一 checkin_id 重复导入（幂等）。
        """
        if not source:
            raise ValidationError("报送渠道不能为空")
        with self.store.lock:
            old = self.store.get_checkin(checkin_id)
            if old is not None:
                if old.volunteer_id != volunteer_id or old.session_id != session_id:
                    raise Conflict(f"签到 {checkin_id} 已存在且归属不同")
                return {"checkin_id": checkin_id, "result": "duplicate_import"}
            session = self._require_session(session_id)
            # 封存年度的已举办场次，普通补报必须在落库前拒绝；
            # 唯一例外：该 (志愿者,场次) 已有其他报送记录，新报送构成
            # "封账后才被发现的重复疑点"——允许进入待确认队列，系统以
            # 更正单把已封积分挂起，核对结论再定案。
            already_reported = bool(self._group_checkins(volunteer_id, session_id))
            if (session.status is SessionStatus.HELD
                    and self._year_closed(session.accounting_year)
                    and not already_reported):
                raise PeriodClosed(
                    f"{session.accounting_year} 年度已封账，补报签到不可入账，"
                    "须由有权人开具更正单；以前年度遗漏请走跨年度补录流程")
            checkin = Checkin(checkin_id, volunteer_id, session_id, role, source, checkin_time)
            self.store.add_checkin(checkin)
            if session.status is not SessionStatus.HELD:
                return {"checkin_id": checkin_id, "result": "waiting_session"}
            # 报送实际处理时间决定"更正单入哪个开放期"，不能用历史服务日期
            result = self._evaluate_group(volunteer_id, session, date.today())
            if result["pending"]:
                result["result"] = "pending"
            elif result["posted"]:
                result["result"] = "posted"
            else:
                result["result"] = "noop"
            return result

    def _evaluate_session(self, session: Session, on_date: date,
                          allow_closed_post: bool = False) -> dict:
        posted: list[str] = []
        pending: list[str] = []
        volunteer_ids = sorted({
            c.volunteer_id for c in self.store.list_checkins()
            if c.session_id == session.session_id
        })
        for vid in volunteer_ids:
            r = self._evaluate_group(vid, session, on_date,
                                     allow_closed_post=allow_closed_post)
            posted.extend(r.get("posted", []))
            pending.extend(r.get("pending", []))
        return {"session_id": session.session_id, "posted": posted, "pending": pending}

    def _group_checkins(self, volunteer_id: str, session_id: str) -> list[Checkin]:
        return [
            c for c in self.store.list_checkins(volunteer_id)
            if c.session_id == session_id
        ]

    def _evaluate_group(self, volunteer_id: str, session: Session, on_date: date,
                        *, allow_closed_post: bool = False) -> dict:
        """对某 (志愿者, 场次) 的全部报送做幂等计提评估。

        ``allow_closed_post`` 为 True 时（仅跨年度补录流程），归属年度已
        封账的正向积分自动以更正单入账；普通签到/举办流程不允许，必须由
        有权人走显式更正单。重复报送的"挂起"不授予积分，封存后仍可进行。
        """
        items = self._group_checkins(volunteer_id, session.session_id)
        dup = engine.detect_duplicates(items)
        key = (volunteer_id, session.session_id)
        year_closed = self._year_closed(session.accounting_year)

        if key in dup:
            sources = dup[key]
            open_pending = self._find_open_pending(key)
            if open_pending is None:
                # 新疑点：把该组当前积分全部红冲挂起，进待确认队列
                suspend = self._suspend_group_locked(session, sources, on_date)
                pending_id = f"P-{uuid.uuid4().hex[:12]}"
                proposed = self._build_service_entry(
                    _new_id("EP"), sources[0], session, on_date, pending_id=pending_id)
                item = PendingItem(
                    pending_id=pending_id,
                    volunteer_id=volunteer_id,
                    session_id=session.session_id,
                    checkin_ids=tuple(c.checkin_id for c in sources),
                    proposed_entry=proposed,
                    reason="同一志愿服务存在多笔报送："
                           + "、".join(f"{c.checkin_id}({c.source})" for c in sources),
                    created_date=on_date,
                )
                self.store.add_pending(item)
                return {"posted": [], "pending": [pending_id],
                        "suspended_entries": suspend}
            # 队列中又来一笔新报送：并入既有待确认单（净额已挂起，无需再冲）
            merged = tuple(dict.fromkeys(
                list(open_pending.checkin_ids) + [c.checkin_id for c in sources]))
            if merged != open_pending.checkin_ids:
                self.store.add_pending(PendingItem(
                    pending_id=open_pending.pending_id,
                    volunteer_id=open_pending.volunteer_id,
                    session_id=open_pending.session_id,
                    checkin_ids=merged,
                    proposed_entry=open_pending.proposed_entry,
                    reason=open_pending.reason,
                    created_date=open_pending.created_date,
                ))
            return {"posted": [], "pending": [open_pending.pending_id],
                    "suspended_entries": []}

        # 无重复：逐笔入账（幂等）
        if year_closed and not allow_closed_post:
            raise PeriodClosed(
                f"{session.accounting_year} 年度已封账，补报签到不可直接入账，"
                "须由有权人开具更正单；跨年度补录请走 /late-entries 流程")
        posted: list[str] = []
        corrections: list[str] = []
        for checkin in items:
            if checkin.checkin_id in self._posted_checkins:
                continue
            entry = self._build_service_entry(
                _new_id("E"), checkin, session, on_date)
            if year_closed and allow_closed_post:
                # 仅跨年度补录流程：封存年度自动转纯蓝字更正单
                corr = self._create_correction_locked(
                    volunteer_id=entry.volunteer_id, original_entry=None,
                    blue_amount=entry.amount,
                    reason=f"跨年度补录（原归属 {entry.year} 年度）：{entry.basis}",
                    approver="late-entry", as_of=on_date,
                    original_year=entry.year, origin="late",
                    session_id=entry.session_id)
                corrections.append(corr.correction_id)
                self._posted_checkins[checkin.checkin_id] = corr.blue_entry_id
                continue
            self._stamp_and_post(entry)
            self._posted_checkins[checkin.checkin_id] = entry.entry_id
            posted.append(entry.entry_id)
        return {"posted": posted, "pending": [], "corrections": corrections}

    # ================================================================ 待确认核对

    def resolve_pending(
        self,
        pending_id: str,
        decision: ReviewDecision,
        reviewer: str,
        *,
        chosen_checkin_id: Optional[str] = None,
        reason: str = "",
        on_date: Optional[date] = None,
    ) -> dict:
        """核对结论定案。系统把该组积分结算到"目标净额"，与处理顺序无关。"""
        on_date = on_date or date.today()
        if decision not in (ReviewDecision.CONFIRM, ReviewDecision.REJECT,
                            ReviewDecision.KEEP_BOTH):
            raise ValidationError("待确认核对只支持 confirm / reject / keep_both")
        with self.store.lock:
            item = self.store.get_pending(pending_id)
            if item is None:
                raise NotFound(f"待确认单不存在：{pending_id}")
            if self.store.pending_resolution(pending_id) is not None:
                raise Conflict(f"待确认单 {pending_id} 已核对结案")
            session = self._require_session(item.session_id)
            if session.status is SessionStatus.CANCELLED:
                raise Conflict("场次已取消，该重复报送已随取消结案")

            checkins = {
                c.checkin_id: c
                for c in self._group_checkins(item.volunteer_id, item.session_id)
            }
            for cid in item.checkin_ids:
                if cid not in checkins:
                    raise PreconditionFailed(f"待确认单引用的签到 {cid} 缺失")

            if decision is ReviewDecision.CONFIRM:
                chosen_id = chosen_checkin_id or item.checkin_ids[0]
                if chosen_id not in item.checkin_ids:
                    raise ValidationError("指定的签到不在该待确认单内")
                effective = [checkins[chosen_id]]
                tag = f"confirm:{chosen_id}"
            elif decision is ReviewDecision.KEEP_BOTH:
                effective = [checkins[cid] for cid in item.checkin_ids]
                tag = "keep_both"
            else:
                effective = []
                tag = "reject"

            target = sum(
                engine.compute_points(c.role, session.hours) for c in effective)
            settled = self._settle_group_locked(
                session, item.volunteer_id, target, on_date,
                pending_id=pending_id,
                effective_ids=[c.checkin_id for c in effective])

            for c in effective:
                self._posted_checkins[c.checkin_id] = "(settled)"

            review = Review(
                review_id=_new_id("R"), ref_kind="pending", ref_id=pending_id,
                decision=decision, reviewer=reviewer, reason=reason,
                created_at=on_date.isoformat(),
            )
            self.store.add_review(review)
            resolution = json.dumps(
                {"decision": tag,
                 "effective": [c.checkin_id for c in effective],
                 "target": target},
                ensure_ascii=False)
            entry_ids = tuple(settled.get("posted", []) + settled.get("reversals", []))
            self.store.resolve_pending(pending_id, entry_ids, resolution)
            return {
                "pending_id": pending_id,
                "resolution": tag,
                "effective_checkins": [c.checkin_id for c in effective],
                "target_points": target,
                "settlement": settled,
                "review_id": review.review_id,
            }

    def list_pending(self, include_resolved: bool = False) -> list[dict]:
        with self.store.lock:
            result = []
            for item in self.store.list_pending(include_resolved):
                done = self.store.pending_resolution(item.pending_id)
                result.append(self._pending_to_dict(item, done))
            return result

    # ================================================================ 申诉

    def file_appeal(
        self,
        appeal_id: str,
        volunteer_id: str,
        year: int,
        amount: int,
        reason: str,
        *,
        session_id: Optional[str] = None,
    ) -> dict:
        """登记申诉。申诉本身不产生积分，审批成立后才补记。"""
        if amount <= 0:
            raise ValidationError("申诉补记金额必须为正整数")
        if not reason:
            raise ValidationError("申诉必须填写理由")
        with self.store.lock:
            if appeal_id in self._appeals:
                raise Conflict(f"申诉已存在：{appeal_id}")
            self._appeals[appeal_id] = {
                "appeal_id": appeal_id,
                "volunteer_id": volunteer_id,
                "year": year,
                "amount": amount,
                "reason": reason,
                "session_id": session_id,
                "status": "filed",
                "entry_id": None,
                "correction_id": None,
            }
            self.store.add_review(Review(
                review_id=_new_id("R"), ref_kind="appeal", ref_id=appeal_id,
                decision=ReviewDecision.PENDING, reviewer="(pending)",
                reason=f"申诉登记：{reason}",
            ))
            return {"appeal_id": appeal_id, "status": "filed"}

    def approve_appeal(
        self,
        appeal_id: str,
        reviewer: str,
        *,
        as_of: Optional[date] = None,
        reject: bool = False,
    ) -> dict:
        on_date = as_of or date.today()
        with self.store.lock:
            appeal = self._appeals.get(appeal_id)
            if appeal is None:
                raise NotFound(f"申诉不存在：{appeal_id}")
            if appeal["status"] != "filed":
                raise Conflict("申诉已审结")

            if reject:
                appeal["status"] = "rejected"
                self.store.add_review(Review(
                    review_id=_new_id("R"), ref_kind="appeal", ref_id=appeal_id,
                    decision=ReviewDecision.REJECT, reviewer=reviewer,
                    reason="申诉驳回", created_at=on_date.isoformat(),
                ))
                return {"appeal_id": appeal_id, "status": "rejected"}

            year = appeal["year"]
            period = self.store.get_period(str(year))
            if period is not None and period["closed"]:
                # 已封账年度的申诉成立 → 纯蓝字更正单，入当前开放期
                corr = self._create_correction_locked(
                    volunteer_id=appeal["volunteer_id"],
                    original_entry=None,
                    blue_amount=appeal["amount"],
                    reason=f"申诉 {appeal_id} 成立（原归属 {year} 年度）：{appeal['reason']}",
                    approver=reviewer,
                    as_of=on_date,
                    original_year=year,
                    origin="appeal",
                )
                appeal["status"] = "approved"
                appeal["correction_id"] = corr.correction_id
                self._appeal_decision_review(appeal_id, reviewer, on_date, True)
                return {"appeal_id": appeal_id, "status": "approved_via_correction",
                        "correction_id": corr.correction_id}

            entry = engine.build_appeal_entry(
                _new_id("E"), appeal["volunteer_id"], year, appeal["amount"],
                appeal["reason"], appeal_id,
                session_id=appeal["session_id"], on_date=on_date,
            )
            entry = self._stamp_and_post(entry)
            appeal["status"] = "approved"
            appeal["entry_id"] = entry.entry_id
            self._appeal_decision_review(appeal_id, reviewer, on_date, True)
            return {"appeal_id": appeal_id, "status": "approved",
                    "entry_id": entry.entry_id}

    def _appeal_decision_review(self, appeal_id: str, reviewer: str,
                                on_date: date, approved: bool) -> None:
        self.store.add_review(Review(
            review_id=_new_id("R"), ref_kind="appeal", ref_id=appeal_id,
            decision=ReviewDecision.CONFIRM if approved else ReviewDecision.REJECT,
            reviewer=reviewer,
            reason="申诉成立，补记积分" if approved else "申诉驳回",
            created_at=on_date.isoformat(),
        ))

    def list_appeals(self) -> list[dict]:
        with self.store.lock:
            return [dict(a) for a in self._appeals.values()]

    # ================================================================ 部分撤销

    def reverse_partial(
        self,
        entry_id: str,
        amount: Optional[int],
        reason: str,
        approver: str,
        *,
        as_of: Optional[date] = None,
    ) -> dict:
        """部分或全额撤销一笔已入账分录。

        - ``amount`` 为撤销积分数（1..剩余可撤额），省略表示全额；
        - 归属年度已封账时拒绝直接冲回，提示改走更正单；
        - 累计撤销不得超过原分录额，超额请求直接拒绝（守恒红线）。
        """
        on_date = as_of or date.today()
        if not reason:
            raise ValidationError("撤销必须填写原因")
        with self.store.lock:
            original = self.store.get_entry(entry_id)
            if original is None:
                raise NotFound(f"分录不存在：{entry_id}")
            if original.kind in (SourceKind.REVERSAL, SourceKind.CORRECTION):
                raise ValidationError("红冲/更正分录不能再撤销")
            period = self.store.get_period(str(original.year))
            if period is not None and period["closed"]:
                raise PeriodClosed(
                    f"{original.year} 年度已封账，撤销须使用更正单；"
                    f"原分录 {entry_id} 净额 {original.amount}，"
                    f"剩余可撤 {self._entry_live_remainder(original)}")
            left = self._entry_live_remainder(original)
            target = amount if amount is not None else left
            if target <= 0 or target > left:
                raise ValidationError(f"撤销额必须在 1..{left} 之间")
            rec = self._reverse_entries_locked(
                [(original, target)],
                reason=f"部分撤销：{reason}" if target < original.amount
                       else f"撤销：{reason}",
                on_date=on_date, approver=approver,
            )[0]
            return {"reversal_id": rec.reversal_id,
                    "reversal_entry_id": rec.reversal_entry_id,
                    "reversed_amount": target}

    # ================================================================ 跨年度补录

    def record_late_batch(
        self,
        items: list[dict],
        *,
        recorded_on: Optional[date] = None,
    ) -> dict:
        """跨年度补录一批以前年度遗漏的签到。

        每项字段：``checkin_id / volunteer_id / session_id / role / source``。
        对应场次必须显式设置早于补录年度的 ``plan_year``：

        - 计划年度未封账：以 ``LATE`` 分录补入计划年度；
        - 计划年度已封账：自动转为更正单（纯蓝字），记入补录发生时的
          开放年度，原年度快照不动；
        - 与既有报送重复的，先挂起进入待确认队列，核对结论定案后
          同样按"开放期直接补 / 封存期走更正单"路由。
        """
        on_date = recorded_on or date.today()
        if not items:
            raise ValidationError("补录清单不能为空")
        posted, pending, corrections = [], [], []
        with self.store.lock:
            for raw in items:
                session = self._require_session(raw["session_id"])
                if session.plan_year is None or session.plan_year >= on_date.year:
                    raise ValidationError(
                        f"场次 {session.session_id} 不是以前年度补录对象"
                        "（需设置更早的 plan_year）")
                checkin_id = raw["checkin_id"]
                if self.store.get_checkin(checkin_id) is None:
                    self.store.add_checkin(Checkin(
                        checkin_id, raw["volunteer_id"], raw["session_id"],
                        raw["role"], raw.get("source", "late"),
                        raw.get("checkin_time")))
                if session.status is not SessionStatus.HELD:
                    session = Session(
                        session.session_id, session.activity_id,
                        session.service_date, session.hours,
                        SessionStatus.HELD, session.plan_year)
                    self.store.upsert_session(session)
                result = self._evaluate_group(
                    raw["volunteer_id"], session, on_date,
                    allow_closed_post=True)
                posted.extend(result.get("posted", []))
                pending.extend(result.get("pending", []))
            return {"posted": posted, "pending": pending, "corrections": corrections,
                    "recorded_on": on_date.isoformat()}

    # ================================================================ 结算期 / 封账

    def ensure_period(self, year: int, label: Optional[str] = None) -> dict:
        with self.store.lock:
            period = self.store.get_period(str(year))
            if period is None:
                period = self.store.create_period(
                    str(year), year, label or f"{year}年度结算期")
            return dict(period)

    def close_period(self, year: int, *, closed_on: Optional[date] = None) -> dict:
        """封账并拍不可变快照。

        并发封账经条件变量串行化：首个调用真正封账，其余调用幂等
        返回同一张快照（``already_closed=True``）。尚有未结案待确认项
        时拒绝封账，避免把不确定积分封进快照。
        """
        closed_date = closed_on or date.today()
        with self.store.lock:
            period = self.store.get_period(str(year))
            already = bool(period and period["closed"])
            open_pending = [i.pending_id for i in self.store.list_pending()]
            if open_pending:
                raise PreconditionFailed(
                    "仍有未核对的待确认项，不能封账：" + "、".join(open_pending))
            snapshot = self._snapshot_year(year)
            if period is None:
                self.store.create_period(str(year), year, f"{year}年度结算期")
            period = self.store.close_period(str(year), closed_date, snapshot)
            return {
                "period_id": str(year),
                "year": year,
                "closed": True,
                "closed_date": period["closed_date"].isoformat(),
                "snapshot": dict(period["snapshot"]),
                "already_closed": already,
            }

    def get_period(self, year: int) -> Optional[dict]:
        with self.store.lock:
            period = self.store.get_period(str(year))
            return dict(period) if period else None

    def list_periods(self) -> list[dict]:
        with self.store.lock:
            return [dict(p) for p in self.store.list_periods()]

    # ================================================================ 更正单（封账后唯一调整入口）

    def create_correction(
        self,
        year: int,
        volunteer_id: str,
        reason: str,
        approver: str,
        *,
        entry_id: Optional[str] = None,
        new_amount: Optional[int] = None,
        additive_amount: Optional[int] = None,
        session_id: Optional[str] = None,
        as_of: Optional[date] = None,
    ) -> dict:
        """封账后调整。

        - ``entry_id`` + ``new_amount``：红字冲销原分录、蓝字按新额重记，
          ``new_amount=0`` 即纯红字冲销（如取消场次追冲）；
        - ``additive_amount``：以前年度少计的纯蓝字补记；
        - 必须 ``year`` 已封账，且入账期（``as_of`` 所在年度）仍开放；
        - 红 + 蓝净额必须恰为申报调整额，否则拒绝。
        """
        on_date = as_of or date.today()
        if not reason:
            raise ValidationError("更正单必须填写原因")
        with self.store.lock:
            period = self.store.get_period(str(year))
            if period is None or not period["closed"]:
                raise PreconditionFailed(
                    f"{year} 年度尚未封账；未封账调整请直接冲回或补记")
            original = None
            if entry_id is not None:
                referenced = self.store.get_entry(entry_id)
                if referenced is None:
                    raise NotFound(f"原分录不存在：{entry_id}")
                if referenced.volunteer_id != volunteer_id:
                    raise ValidationError("原分录与志愿者不匹配")
                if new_amount is None or new_amount < 0:
                    raise ValidationError("更正原分录必须提供非负 new_amount")
                original = self._resolve_carrier_entry(referenced)
            elif additive_amount is None or additive_amount <= 0:
                raise ValidationError(
                    "必须提供 entry_id+new_amount，或正整数 additive_amount")
            corr = self._create_correction_locked(
                volunteer_id=volunteer_id,
                original_entry=original,
                red_amount=self._entry_live_remainder(original) if original else None,
                blue_amount=new_amount if original is not None else additive_amount,
                reason=reason,
                approver=approver,
                as_of=on_date,
                original_year=year,
            )
            return self._correction_dict(corr)

    def _resolve_carrier_entry(self, referenced: Entry) -> Entry:
        """把用户引用的分录解析为"当前承载在账积分"的分录。

        重复报送组核对后，原分录可能已被挂起红冲，积分由"待确认结算补记"
        分录承载。若引用的分录已无在账余额，且同 (志愿者, 场次) 存在唯一
        在账承载分录，则自动定位到它；有多个候选时拒绝歧义，要求显式指定。
        """
        if self._entry_live_remainder(referenced) > 0:
            return referenced
        if referenced.session_id is None:
            raise ValidationError(
                f"分录 {referenced.entry_id} 已无在账余额，且无法定位到场次承载分录")
        carriers = [
            e for e in self.store.iter_entries()
            if e.volunteer_id == referenced.volunteer_id
            and e.session_id == referenced.session_id
            and e.kind in (SourceKind.SERVICE, SourceKind.LATE)
            and self._entry_live_remainder(e) > 0
        ]
        if len(carriers) == 1:
            return carriers[0]
        if not carriers:
            raise ValidationError(
                f"分录 {referenced.entry_id} 已无在账余额，同场次也无在账分录可更正")
        raise ValidationError(
            f"分录 {referenced.entry_id} 已无在账余额，同场次存在多笔在账分录："
            + "、".join(e.entry_id for e in carriers) + "；请显式指定承载分录")

    def _create_correction_locked(
        self,
        *,
        volunteer_id: str,
        original_entry: Optional[Entry],
        reason: str,
        approver: str,
        as_of: date,
        red_amount: Optional[int] = None,
        blue_amount: Optional[int] = None,
        original_year: Optional[int] = None,
        origin: str = "manual",
        session_id: Optional[str] = None,
    ) -> Correction:
        """构造并落账一张更正单。调用方须持锁。

        显式金额口径（避免在已部分撤销后超额红冲）：

        - ``original_entry`` + ``red_amount``：红冲额，1..当前在账剩余额；
        - ``blue_amount``：蓝字重记额（>=0，0 表示纯红字冲销）；
        - 无 ``original_entry`` 时为纯蓝字补记，``blue_amount`` 必须为正；
        - 守恒自检恒成立：净影响 = 蓝字 - 红冲。

        更正分录 year = 开放的入账年度（``as_of.year``）；原年度快照不动。
        """
        if original_entry is not None:
            original_year = original_year or original_entry.year
            remainder = self._entry_live_remainder(original_entry)
            if red_amount is None:
                red_amount = remainder
            if not 1 <= red_amount <= remainder:
                raise ValidationError(
                    f"红冲额必须在 1..{remainder}（原分录当前在账剩余额）之间")
            blue_amount = blue_amount or 0
            if blue_amount < 0:
                raise ValidationError("蓝字重记金额不能为负")
            expected_delta = blue_amount - red_amount
            sid = original_entry.session_id
        else:
            if not blue_amount or blue_amount <= 0:
                raise ValidationError("纯蓝字补记金额必须为正整数")
            red_amount = 0
            expected_delta = blue_amount
            sid = session_id

        booking_year = as_of.year
        target_period = self.store.get_period(str(booking_year))
        if target_period is not None and target_period["closed"]:
            raise PeriodClosed(
                f"更正入账年度 {booking_year} 已封账，请在开放期办理更正")
        if original_year is not None and booking_year == original_year:
            raise PeriodClosed("更正必须记入封账年度之后的开放期")
        if target_period is None:
            self.store.create_period(
                str(booking_year), booking_year, f"{booking_year}年度结算期")

        red_id: Optional[str] = None
        blue_id: Optional[str] = None
        delta = 0

        if original_entry is not None:
            red = engine.build_reversal_entry(
                _new_id("E"),
                Entry(
                    original_entry.entry_id, original_entry.volunteer_id,
                    original_entry.year, red_amount, original_entry.kind,
                    original_entry.session_id, original_entry.basis,
                    original_entry.evidence, original_entry.parent_entry_id,
                    original_entry.pending_id, original_entry.created_date,
                    original_entry.period_id,
                ),
                reason=f"封账后更正红字（原归属 {original_year} 年度）：{reason}",
                on_date=as_of,
            )
            red = Entry(
                red.entry_id, red.volunteer_id, booking_year, red.amount,
                SourceKind.CORRECTION, red.session_id, red.basis,
                (("原归属年度", str(original_year)),
                 ("原分录", original_entry.entry_id),
                 ("原在账额", str(remainder)),
                 ("红冲额", str(red_amount)), *red.evidence),
                original_entry.entry_id, None, as_of, str(booking_year),
            )
            self.store.add_entry(red)
            red_id = red.entry_id
            delta += red.amount

            if blue_amount > 0:
                blue = Entry(
                    entry_id=_new_id("E"),
                    volunteer_id=volunteer_id,
                    year=booking_year,
                    amount=blue_amount,
                    kind=SourceKind.CORRECTION,
                    session_id=original_entry.session_id,
                    basis=f"封账后蓝字重记（原 {original_year} 年度，"
                          f"原分录 {original_entry.entry_id}）：{reason}",
                    evidence=(
                        ("原归属年度", str(original_year)),
                        ("原分录", original_entry.entry_id),
                        ("原金额", str(original_entry.amount)),
                        ("红冲额", str(red_amount)),
                        ("重记金额", str(blue_amount)),
                        ("原因", reason),
                        ("审批人", approver),
                    ),
                    parent_entry_id=original_entry.entry_id,
                    created_date=as_of,
                    period_id=str(booking_year),
                )
                self.store.add_entry(blue)
                blue_id = blue.entry_id
                delta += blue.amount
        else:
            blue = Entry(
                entry_id=_new_id("E"),
                volunteer_id=volunteer_id,
                year=booking_year,
                amount=blue_amount,
                kind=SourceKind.CORRECTION,
                session_id=sid,
                basis=f"封账后蓝字补记（原归属 {original_year or booking_year} 年度）：{reason}",
                evidence=(
                    ("原归属年度", str(original_year or booking_year)),
                    ("补记金额", str(blue_amount)),
                    ("原因", reason),
                    ("审批人", approver),
                ),
                created_date=as_of,
                period_id=str(booking_year),
            )
            self.store.add_entry(blue)
            blue_id = blue.entry_id
            delta += blue.amount

        # 守恒自检：蓝字 - 红冲必须恰为净影响
        if delta != expected_delta:  # pragma: no cover - 构造逻辑保证，防回归
            raise RuntimeError(
                f"更正单净额 {delta} 与申报调整额 {expected_delta} 不一致")

        corr = Correction(
            correction_id=_new_id("C"),
            year=original_year or booking_year,
            volunteer_id=volunteer_id,
            red_entry_id=red_id,
            blue_entry_id=blue_id,
            reason=reason,
            approver=approver,
            created_date=as_of,
            period_id=str(booking_year),
            origin=origin,
        )
        self.store.add_correction(corr)
        self.store.add_review(Review(
            review_id=_new_id("R"), ref_kind="correction", ref_id=corr.correction_id,
            decision=ReviewDecision.CONFIRM, reviewer=approver,
            reason=f"封账后更正，净额 {delta:+d}：{reason}",
            created_at=as_of.isoformat(),
        ))
        return corr

    # ================================================================ 查询：余额 / 逐笔解释 / 排名

    def balance(self, volunteer_id: str, year: Optional[int] = None) -> dict:
        """余额查询。封存年度额外返回封账快照与封账后调整额。"""
        with self.store.lock:
            entries = [
                e for e in self.store.iter_entries()
                if e.volunteer_id == volunteer_id and (year is None or e.year == year)
            ]
            result = {
                "volunteer_id": volunteer_id,
                "year": year,
                "balance": sum(e.amount for e in entries),
                "entry_count": len(entries),
            }
            if year is not None:
                period = self.store.get_period(str(year))
                if period is not None and period["closed"]:
                    result["settled_snapshot"] = period["snapshot"].get(volunteer_id, 0)
                    post_close = 0
                    for c in self.store.list_corrections(year):
                        if c.volunteer_id != volunteer_id:
                            continue
                        if c.red_entry_id:
                            post_close += self.store.get_entry(c.red_entry_id).amount
                        if c.blue_entry_id:
                            post_close += self.store.get_entry(c.blue_entry_id).amount
                    result["post_close_adjustments"] = post_close
                    result["effective_balance"] = (
                        result["settled_snapshot"] + post_close)
            return result

    def ledger(
        self,
        volunteer_id: Optional[str] = None,
        year: Optional[int] = None,
    ) -> list[dict]:
        """逐笔账本：每笔都带证据链，用于逐笔解释积分来源。"""
        with self.store.lock:
            rows = []
            for e in sorted(self.store.iter_entries(),
                            key=lambda x: (x.created_date or date.min, x.entry_id)):
                if volunteer_id and e.volunteer_id != volunteer_id:
                    continue
                if year is not None and e.year != year:
                    continue
                rows.append(self._entry_to_dict(e))
            return rows

    def explain_entry(self, entry_id: str) -> dict:
        """单笔分录的完整来源解释。"""
        with self.store.lock:
            entry = self.store.get_entry(entry_id)
            if entry is None:
                raise NotFound(f"分录不存在：{entry_id}")
            payload = self._entry_to_dict(entry)
            if entry.parent_entry_id:
                parent = self.store.get_entry(entry.parent_entry_id)
                payload["parent"] = self._entry_to_dict(parent) if parent else None
            return payload

    def rankings(self, year: int) -> dict:
        """年度排名与兑换额度依据。

        - 未封账：按活账求和（待确认挂起积分天然为 0，不计入）；
        - 已封账：以不可变快照为准；封账后更正单体现在更正发生的开放期。
        """
        with self.store.lock:
            period = self.store.get_period(str(year))
            if period is not None and period["closed"]:
                totals = dict(period["snapshot"])
                basis = "settled_snapshot"
            else:
                totals = self._snapshot_year(year)
                basis = "live"
            ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
            return {
                "year": year,
                "basis": basis,
                "rankings": [
                    {"rank": i + 1, "volunteer_id": v, "points": p}
                    for i, (v, p) in enumerate(ranked) if p > 0
                ],
                "note": "待确认/挂起积分不计入；封账后以快照为准，"
                        "更正单在其入账开放期体现。",
            }

    # ================================================================ 审计复算

    def recompute_period(self, year: int) -> dict:
        """从原始事实独立复算某结算期，并与账本、封账快照双向核对。

        审计恒等式（任一志愿者与年度汇总均成立）::

            facts(Y) = 由签到/场次状态/角色/审核结论独立重算出的归属积分
            ledger(Y) = Σ 归属年度为 Y 的已入账分录

            开放年度：facts(Y) = ledger(Y) − 入在 Y、但归属于其他封存年度的更正净额
            封存年度：facts(Y) = snapshot(Y) + 归属于 Y 的更正单净影响
                       （且 ledger(Y) 必须恒等于不可变快照 snapshot(Y)）
        """
        with self.store.lock:
            sessions = {s.session_id: s for s in self.store.list_sessions()}

            # ---- 事实侧：按 (志愿者, 场次) 求期望积分 ----
            groups: dict[tuple[str, str], list[Checkin]] = {}
            for c in self.store.list_checkins():
                groups.setdefault((c.volunteer_id, c.session_id), []).append(c)

            facts: dict[str, int] = {}
            unresolved_groups: list[str] = []
            detail: dict[str, list[dict]] = {}
            for (vid, sid), items in sorted(groups.items()):
                session = sessions.get(sid)
                if session is None or session.accounting_year != year:
                    continue
                unique = {c.checkin_id: c for c in items}
                is_dup = len(unique) > 1
                effective: list[Checkin] = []
                resolution_tag: Optional[str] = None

                if session.status is SessionStatus.CANCELLED:
                    # 取消场次事实积分为 0：开放期由红冲体现，
                    # 封账后取消由"归属本年、入在次年"的更正单体现
                    resolution_tag = "session_cancelled"
                elif session.status is not SessionStatus.HELD:
                    # 计划中而未举办：不计提
                    resolution_tag = "not_held"
                elif is_dup:
                    effective_ids = self._final_effective_checkins(vid, sid)
                    if effective_ids is None:
                        unresolved_groups.append(f"{vid}@{sid}")
                        effective_ids = set()  # 未结案 → 挂起，净额 0
                        resolution_tag = "duplicate_pending"
                    effective = [unique[i] for i in effective_ids if i in unique]
                else:
                    effective = list(unique.values())

                net = sum(
                    engine.compute_points(c.role, session.hours) for c in effective)
                if net:
                    facts[vid] = facts.get(vid, 0) + net
                detail.setdefault(vid, []).append({
                    "session_id": sid,
                    "activity_id": session.activity_id,
                    "status": session.status.value,
                    "duplicate": is_dup,
                    "resolution": resolution_tag or ("unique" if not is_dup else "resolved"),
                    "effective_checkins": [c.checkin_id for c in effective],
                    "points": net,
                })

            # ---- 裁决事实侧 1：申诉成立补记（审核结论）----
            appeals_facts: dict[str, int] = {}
            for appeal in self._appeals.values():
                if appeal["status"] != "approved" or appeal["year"] != year:
                    continue
                amount = appeal["amount"]
                appeals_facts[appeal["volunteer_id"]] = (
                    appeals_facts.get(appeal["volunteer_id"], 0) + amount)

            # ---- 裁决事实侧 2：人工部分/全额撤销（红冲为负）----
            # 只取归属本年度原分录的撤销；封存后撤销不存在（必须走更正单）。
            # 例外：若原分录所在场次后来被取消、或其组后来成为重复报送组，
            # 该组最终净额已由"场次状态 / 待确认结论"在分组事实 G 中完整重算，
            # 历史部分撤销必须剔除，否则会重复扣减。
            cancelled_sids = {
                s.session_id for s in sessions.values()
                if s.status is SessionStatus.CANCELLED}
            dup_groups_set = {
                (vid, sid)
                for (vid, sid), its in groups.items()
                if len({c.checkin_id for c in its}) > 1
            }
            partial_facts: dict[str, int] = {}
            for rv in self.store.list_reversals():
                if rv.origin != "partial":
                    continue
                original = self.store.get_entry(rv.original_entry_id)
                if original is None or original.year != year:
                    continue
                if original.session_id in cancelled_sids:
                    continue
                if (original.volunteer_id, original.session_id) in dup_groups_set:
                    continue
                rev_entry = self.store.get_entry(rv.reversal_entry_id)
                partial_facts[rv.volunteer_id] = (
                    partial_facts.get(rv.volunteer_id, 0) + rev_entry.amount)
            partial_facts = {k: v for k, v in sorted(partial_facts.items()) if v != 0}

            facts = {k: v for k, v in sorted(facts.items()) if v != 0}
            ledger = self._snapshot_year(year)
            period = self.store.get_period(str(year))
            closed = bool(period and period["closed"])
            snapshot = dict(period["snapshot"]) if closed else None

            # ---- 更正单：归属本年 / 入在本年但归属他年，两个方向分别汇总 ----
            attr_rows = self._review_corrections(year)
            attr_delta: dict[str, int] = {}
            manual_delta: dict[str, int] = {}
            for row in attr_rows:
                attr_delta[row["volunteer_id"]] = (
                    attr_delta.get(row["volunteer_id"], 0) + row["net_delta"])
                if row["origin"] == "manual":
                    manual_delta[row["volunteer_id"]] = (
                        manual_delta.get(row["volunteer_id"], 0) + row["net_delta"])
            attr_delta = {k: v for k, v in sorted(attr_delta.items()) if v != 0}
            manual_delta = {k: v for k, v in sorted(manual_delta.items()) if v != 0}

            booked_delta: dict[str, int] = {}
            for c in self.store.list_corrections():
                if c.period_id != str(year) or c.year == year:
                    continue
                for eid in (c.red_entry_id, c.blue_entry_id):
                    if eid:
                        e = self.store.get_entry(eid)
                        booked_delta[e.volunteer_id] = (
                            booked_delta.get(e.volunteer_id, 0) + e.amount)
            booked_delta = {k: v for k, v in sorted(booked_delta.items()) if v != 0}

            # ---- 权威积分 = 分组事实 G + 申诉 A + 人工撤销 P + 手工更正 M ----
            # 申诉与手工更正是"裁决事实"，无论记在当年还是以后续更正单入账，
            # 都完整计入权威侧；账侧再用"外来更正/归属更正"两个方向找平。
            def _merge(*maps: dict[str, int]) -> dict[str, int]:
                out: dict[str, int] = {}
                for m in maps:
                    for k, v in m.items():
                        out[k] = out.get(k, 0) + v
                return {k: v for k, v in sorted(out.items()) if v != 0}

            authority = _merge(facts, appeals_facts, partial_facts, manual_delta)

            # 账侧恒等式（开放/封存通用；封存时 ledger 恒等不可变快照）：
            #   权威(Y) = 年度分录合计 − 入在本年但归属他年的外来更正
            #                    + 归属本年、以后续更正单入账的净影响
            checks: dict[str, dict[str, int]] = {}
            if closed:
                checks["ledger_vs_settled_snapshot"] = self._diff_totals(
                    ledger, snapshot or {})
            rhs = _merge(ledger,
                         {k: -v for k, v in booked_delta.items()},
                         attr_delta)
            checks["authoritative_vs_ledger_rebased"] = self._diff_totals(
                authority, rhs)

            # ---- 申诉凭证完整性：成立的申诉必须有分录或更正单 ----
            appeal_vouchers = []
            for appeal in self._appeals.values():
                if appeal["status"] != "approved" or appeal["year"] != year:
                    continue
                ok = bool(appeal["entry_id"] or appeal["correction_id"])
                appeal_vouchers.append({
                    "appeal_id": appeal["appeal_id"],
                    "volunteer_id": appeal["volunteer_id"],
                    "amount": appeal["amount"],
                    "entry_id": appeal["entry_id"],
                    "correction_id": appeal["correction_id"],
                    "voucher_ok": ok,
                })

            conservation = self.verify_conservation()
            voucher_ok = all(v["voucher_ok"] for v in appeal_vouchers)
            return {
                "year": year,
                "closed": closed,
                "authoritative_totals": authority,
                "fact_totals": facts,
                "fact_group_detail": detail,
                "appeal_facts": appeals_facts,
                "manual_partial_reversal_facts": partial_facts,
                "manual_correction_facts": manual_delta,
                "ledger_totals": ledger,
                "snapshot_totals": snapshot,
                "corrections_attributable_to_period": {
                    "rows": attr_rows,
                    "net_delta_by_volunteer": attr_delta,
                },
                "foreign_corrections_booked_in_period": {
                    "net_delta_by_volunteer": booked_delta,
                },
                "unresolved_pending_groups": unresolved_groups,
                "appeal_vouchers": appeal_vouchers,
                "checks": checks,
                "matches": (all(not d for d in checks.values())
                            and voucher_ok and conservation["ok"]),
                "conservation": conservation,
            }

    def verify_conservation(self) -> dict:
        """余额守恒自检；任何违例都意味着落账路径存在缺陷。"""
        problems: list[str] = []
        entries = list(self.store.iter_entries())

        # 1) 单条原分录的累计对冲不得超过其金额（按对冲目标分录分别校验）
        by_parent: dict[str, int] = {}
        for e in entries:
            if e.parent_entry_id and e.amount < 0:
                by_parent[e.parent_entry_id] = by_parent.get(e.parent_entry_id, 0) - e.amount
        for parent_id, reversed_amt in by_parent.items():
            parent = self.store.get_entry(parent_id)
            if parent is None:
                problems.append(f"对冲分录引用了不存在的原分录 {parent_id}")
            elif reversed_amt > parent.amount:
                problems.append(
                    f"分录 {parent_id} 累计冲回 {reversed_amt} 超过原额 {parent.amount}")

        # 2) 封存快照必须恒等于该年度全部归属分录之和
        for p in self.store.list_periods():
            if p["closed"]:
                live = self._snapshot_year(p["year"])
                if live != p["snapshot"]:
                    problems.append(
                        f"{p['year']} 年度快照与分录求和不一致："
                        f"差额 {self._diff_totals(live, p['snapshot'])}")

        # 3) 更正单红/蓝分录必须真实存在，且至少有一条
        for c in self.store.list_corrections():
            if not c.red_entry_id and not c.blue_entry_id:
                problems.append(f"更正单 {c.correction_id} 红蓝皆空")
            if c.red_entry_id and self.store.get_entry(c.red_entry_id) is None:
                problems.append(f"更正单 {c.correction_id} 红字分录缺失")
            if c.blue_entry_id and self.store.get_entry(c.blue_entry_id) is None:
                problems.append(f"更正单 {c.correction_id} 蓝字分录缺失")

        grand_total = sum(e.amount for e in entries)
        per_vol: dict[str, int] = {}
        for e in entries:
            per_vol[e.volunteer_id] = per_vol.get(e.volunteer_id, 0) + e.amount
        return {
            "ok": not problems,
            "problems": problems,
            "grand_total": grand_total,
            "volunteer_count": len(per_vol),
        }

    # ================================================================ 内部记账原语

    def _build_service_entry(
        self,
        entry_id: str,
        checkin: Checkin,
        session: Session,
        on_date: date,
        *,
        pending_id: Optional[str] = None,
    ) -> Entry:
        is_late = session.plan_year is not None and session.plan_year < on_date.year
        if is_late:
            return engine.build_late_entries(
                [entry_id], [checkin], {session.session_id: session}, on_date,
            )[0] if pending_id is None else self._tag_pending(
                engine.build_late_entries(
                    [entry_id], [checkin], {session.session_id: session}, on_date)[0],
                pending_id)
        return engine.build_service_entry(
            entry_id, checkin, session, pending_id=pending_id, on_date=on_date)

    @staticmethod
    def _tag_pending(entry: Entry, pending_id: str) -> Entry:
        return Entry(
            entry.entry_id, entry.volunteer_id, entry.year, entry.amount,
            entry.kind, entry.session_id, entry.basis,
            (("待确认单", pending_id), *entry.evidence),
            entry.parent_entry_id, pending_id, entry.created_date, entry.period_id,
        )

    def _year_closed(self, year: int) -> bool:
        period = self.store.get_period(str(year))
        return bool(period and period["closed"])

    def _stamp_and_post(self, entry: Entry) -> Entry:
        """落账前强制校验归属期开放并打上结算期标记。"""
        period = self.store.get_period(str(entry.year))
        if period is not None and period["closed"]:
            raise PeriodClosed(
                f"{entry.year} 年度已封账，普通分录不可入账，请使用更正单")
        if period is None:
            self.store.create_period(
                str(entry.year), entry.year, f"{entry.year}年度结算期")
        if entry.period_id is None:
            entry = Entry(
                entry.entry_id, entry.volunteer_id, entry.year, entry.amount,
                entry.kind, entry.session_id, entry.basis, entry.evidence,
                entry.parent_entry_id, entry.pending_id, entry.created_date,
                str(entry.year),
            )
        self.store.add_entry(entry)
        return entry

    def _group_entries(
        self,
        volunteer_id: Optional[str],
        session_id: str,
        *,
        year: Optional[int] = None,
    ) -> dict[Entry, int]:
        """返回组内正向分录 -> 当前未冲余额。"""
        result: dict[Entry, int] = {}
        for e in self.store.iter_entries():
            if e.session_id != session_id:
                continue
            if volunteer_id and e.volunteer_id != volunteer_id:
                continue
            if year is not None and e.year != year:
                continue
            if e.kind in (SourceKind.SERVICE, SourceKind.LATE):
                remainder = self._entry_live_remainder(e)
                if remainder > 0:
                    result[e] = remainder
        return result

    def _entry_live_remainder(self, entry: Entry) -> int:
        """一笔正向分录扣除全部对冲（红冲/更正红字）后的剩余额。"""
        if entry.kind not in (SourceKind.SERVICE, SourceKind.LATE, SourceKind.APPEAL):
            return 0
        offset = 0
        for e in self.store.iter_entries():
            if e.parent_entry_id == entry.entry_id and e.amount < 0:
                offset += -e.amount
        return max(0, entry.amount - offset)

    def _group_net(self, volunteer_id: str, session_id: str, year: int) -> int:
        """组内净额：含归属年度内的服务/补录/红冲，以及归属于该组、
        因封账而记在后续开放期的更正单（更正分录也带 session_id）。"""
        return sum(
            e.amount for e in self.store.iter_entries()
            if e.volunteer_id == volunteer_id
            and e.session_id == session_id
            and (
                (e.year == year and e.kind in (
                    SourceKind.SERVICE, SourceKind.LATE, SourceKind.REVERSAL))
                or e.kind == SourceKind.CORRECTION
            )
        )

    def _suspend_group_locked(
        self,
        session: Session,
        sources: list[Checkin],
        on_date: date,
    ) -> list[str]:
        """把一组当前全部积分挂起（净额归 0）。

        归属年度开放：直接红字冲回；归属年度已封账：逐笔生成更正单
        （纯红字）记到当前开放期。返回红冲/更正分录 id。
        """
        vid = sources[0].volunteer_id
        year = session.accounting_year
        period = self.store.get_period(str(year))
        closed = bool(period and period["closed"])
        live = list(self._group_entries(vid, session.session_id).items())
        if closed:
            ids = []
            for entry, remainder in live:
                corr = self._create_correction_locked(
                    volunteer_id=vid, original_entry=entry,
                    red_amount=remainder, blue_amount=0,
                    reason="疑似重复报送，封存年度发现，积分按更正单挂起待核对",
                    approver="system-duplicate", as_of=on_date,
                    origin="duplicate")
                ids.append(corr.red_entry_id)
            return [i for i in ids if i]
        records = self._reverse_entries_locked(
            live,
            reason="疑似重复报送，积分临时挂起待核对",
            on_date=on_date, approver="system-duplicate",
            origin="duplicate",
        )
        return [r.reversal_entry_id for r in records]

    def _settle_group_locked(
        self,
        session: Session,
        volunteer_id: str,
        target: int,
        on_date: date,
        *,
        pending_id: str,
        effective_ids: list[str],
    ) -> dict:
        """把组内净额结算到 target：差额补记或红冲；封存年度自动走更正单。"""
        current = sum(
            self._group_net(volunteer_id, session.session_id, y)
            for y in {session.accounting_year}
        )
        diff = target - current
        if diff == 0:
            return {"posted": [], "reversals": [], "corrections": [],
                    "from": current, "to": target}

        year = session.accounting_year
        period = self.store.get_period(str(year))
        closed = bool(period and period["closed"])

        if diff > 0:
            if closed:
                corr = self._create_correction_locked(
                    volunteer_id=volunteer_id, original_entry=None,
                    blue_amount=diff,
                    reason=f"待确认 {pending_id} 核对结论成立，"
                           f"原归属 {year} 年度已封账，按更正单补记",
                    approver="pending-resolution", as_of=on_date,
                    original_year=year, origin="duplicate",
                    session_id=session.session_id)
                return {"posted": [], "reversals": [],
                        "corrections": [corr.correction_id],
                        "from": current, "to": target}
            # 开放年度：直接按差额构造一笔结算分录（依据写清来自核对结论）
            kind = SourceKind.LATE if session.plan_year is not None and \
                session.plan_year < on_date.year else SourceKind.SERVICE
            entry = Entry(
                entry_id=_new_id("E"),
                volunteer_id=volunteer_id,
                year=year,
                amount=diff,
                kind=kind,
                session_id=session.session_id,
                basis=f"待确认 {pending_id} 核对结论结算补记 {diff} 分",
                evidence=(
                    ("待确认单", pending_id),
                    ("有效签到", "、".join(effective_ids) or "(无)"),
                    ("目标净额", str(target)),
                    ("结算前净额", str(current)),
                ),
                pending_id=pending_id,
                created_date=on_date,
            )
            self._stamp_and_post(entry)
            return {"posted": [entry.entry_id], "reversals": [], "corrections": [],
                    "from": current, "to": target}

        # diff < 0：把 -diff 按各正向分录剩余额分摊红冲
        need = -diff
        plan: list[tuple[Entry, int]] = []
        for entry, remainder in self._group_entries(
                volunteer_id, session.session_id).items():
            if entry.year != year:
                continue
            take = min(remainder, need)
            if take > 0:
                plan.append((entry, take))
                need -= take
            if need == 0:
                break
        if need != 0:
            raise RuntimeError(
                f"待确认 {pending_id} 结算红冲时可冲额不足，还差 {need}")  # pragma: no cover
        if closed:
            corr_ids = []
            for entry, take in plan:
                corr = self._create_correction_locked(
                    volunteer_id=volunteer_id, original_entry=entry,
                    red_amount=take, blue_amount=0,
                    reason=f"待确认 {pending_id} 核对驳回，封存年度更正冲减 {take} 分",
                    approver="pending-resolution", as_of=on_date,
                    origin="duplicate")
                corr_ids.append(corr.correction_id)
            return {"posted": [], "reversals": [], "corrections": corr_ids,
                    "from": current, "to": target}
        records = self._reverse_entries_locked(
            plan,
            reason=f"待确认 {pending_id} 核对结论结算红冲",
            on_date=on_date, approver="pending-resolution",
            origin="duplicate",
        )
        return {"posted": [],
                "reversals": [r.reversal_entry_id for r in records],
                "corrections": [], "from": current, "to": target}

    def _reverse_entries_locked(
        self,
        plan: list[tuple[Entry, int]],
        *,
        reason: str,
        on_date: date,
        approver: str,
        origin: str = "partial",
    ) -> list[ReversalRecord]:
        """按 (原分录, 冲减额) 清单落红字分录；冲减额 <= 原分录剩余额。"""
        records: list[ReversalRecord] = []
        for original, amount in plan:
            if amount <= 0 or amount > self._entry_live_remainder(original) + (
                    # 同一批次内的连续冲减：允许等于当前剩余
                    0):
                raise ValidationError(
                    f"红冲额 {amount} 超过分录 {original.entry_id} 可冲余额")
            partial = original if amount == original.amount else Entry(
                original.entry_id, original.volunteer_id, original.year,
                amount, original.kind, original.session_id, original.basis,
                original.evidence, original.parent_entry_id, original.pending_id,
                original.created_date, original.period_id,
            )
            rev = engine.build_reversal_entry(
                _new_id("E"), partial, reason, on_date=on_date)
            rev = self._stamp_and_post(rev)
            rec = ReversalRecord(
                reversal_id=_new_id("RV"),
                volunteer_id=original.volunteer_id,
                original_entry_id=original.entry_id,
                reversal_entry_id=rev.entry_id,
                reason=reason,
                created_date=on_date,
                period_id=rev.period_id,
                origin=origin,
            )
            self.store.add_reversal(rec)
            records.append(rec)
        review_decision = {
            "cancel": ReviewDecision.CANCEL_SESSION,
            "duplicate": ReviewDecision.REJECT,
        }.get(origin, ReviewDecision.REVERSAL)
        self.store.add_review(Review(
            review_id=_new_id("R"),
            ref_kind="entry",
            ref_id=",".join(p.entry_id for p, _ in plan),
            decision=review_decision,
            reviewer=approver,
            reason=f"红冲 {sum(a for _, a in plan)} 分：{reason}",
            created_at=on_date.isoformat(),
        ))
        return records

    def _final_effective_checkins(
        self, volunteer_id: str, session_id: str,
    ) -> Optional[set[str]]:
        """根据审核记录链求一个重复组最终有效的签到集合。

        返回 None 表示仍有未结案待确认（应挂起，净额 0）；
        返回集合（可能为空集）表示已有最终结论。
        """
        pendings = [
            p for p in self.store.list_pending(True)
            if p.volunteer_id == volunteer_id and p.session_id == session_id
        ]
        effective: set[str] = set()
        saw_resolved = False
        for p in sorted(pendings, key=lambda x: x.created_date):
            done = self.store.pending_resolution(p.pending_id)
            if done is None:
                return None
            data = json.loads(done[1])
            if data["decision"] == "cancelled":
                return set()
            effective = set(data.get("effective", []))
            saw_resolved = True
        return effective if saw_resolved else set()

    def _review_corrections(self, attributable_year: int) -> list[dict]:
        rows = []
        for c in self.store.list_corrections(attributable_year):
            red = self.store.get_entry(c.red_entry_id) if c.red_entry_id else None
            blue = self.store.get_entry(c.blue_entry_id) if c.blue_entry_id else None
            red_amt = red.amount if red else 0
            blue_amt = blue.amount if blue else 0
            referenced_ok = True
            if red is not None:
                referenced_ok = (
                    red.parent_entry_id is not None
                    and self.store.get_entry(red.parent_entry_id) is not None)
            rows.append({
                "correction_id": c.correction_id,
                "volunteer_id": c.volunteer_id,
                "red": red_amt,
                "blue": blue_amt,
                "net_delta": red_amt + blue_amt,
                "booked_period": c.period_id,
                "reference_intact": referenced_ok,
                "origin": c.origin,
                "approver": c.approver,
                "reason": c.reason,
            })
        return rows

    def _snapshot_year(self, year: int) -> dict[str, int]:
        totals: dict[str, int] = {}
        for e in self.store.iter_entries():
            if e.year == year:
                totals[e.volunteer_id] = totals.get(e.volunteer_id, 0) + e.amount
        return {k: totals[k] for k in sorted(totals) if totals[k] != 0}

    @staticmethod
    def _diff_totals(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
        keys = set(a) | set(b)
        return {
            k: a.get(k, 0) - b.get(k, 0)
            for k in sorted(keys)
            if a.get(k, 0) != b.get(k, 0)
        }

    def _find_open_pending(
        self, key: tuple[str, str]) -> Optional[PendingItem]:
        for item in self.store.list_pending():
            if (item.volunteer_id, item.session_id) == key:
                return item
        return None

    def _require_session(self, session_id: str) -> Session:
        session = self.store.get_session(session_id)
        if session is None:
            raise NotFound(f"场次不存在：{session_id}")
        return session

    # ================================================================ 序列化

    def _entry_to_dict(self, e: Entry) -> dict:
        # 派生分录状态：正向分录按在账剩余额判定；红冲/更正分录本身恒为已入账
        if e.amount > 0 and e.kind in (
                SourceKind.SERVICE, SourceKind.LATE, SourceKind.APPEAL):
            remainder = self._entry_live_remainder(e)
            if remainder == 0:
                status = EntryStatus.REVERSED.value
            elif remainder < e.amount:
                status = EntryStatus.PARTIALLY_REVERSED.value
            else:
                status = EntryStatus.POSTED.value
        else:
            status = EntryStatus.POSTED.value
        return {
            "entry_id": e.entry_id,
            "volunteer_id": e.volunteer_id,
            "year": e.year,
            "period_id": e.period_id,
            "amount": e.amount,
            "kind": e.kind.value,
            "status": status,
            "session_id": e.session_id,
            "parent_entry_id": e.parent_entry_id,
            "pending_id": e.pending_id,
            "basis": e.basis,
            "created_date": e.created_date.isoformat() if e.created_date else None,
            "evidence": [{"key": k, "value": v} for k, v in e.evidence],
        }

    def _pending_to_dict(
        self,
        item: PendingItem,
        done: Optional[tuple[tuple[str, ...], str]],
    ) -> dict:
        resolution = None
        effective: list[str] = []
        if done:
            resolution = json.loads(done[1]).get("decision")
            effective = json.loads(done[1]).get("effective", [])
        return {
            "pending_id": item.pending_id,
            "volunteer_id": item.volunteer_id,
            "session_id": item.session_id,
            "checkin_ids": list(item.checkin_ids),
            "reason": item.reason,
            "created_date": item.created_date.isoformat(),
            "proposed": {
                "entry_id": item.proposed_entry.entry_id,
                "amount": item.proposed_entry.amount,
                "year": item.proposed_entry.year,
                "basis": item.proposed_entry.basis,
            },
            "resolved": done is not None,
            "resolution": resolution,
            "effective_checkin_ids": effective,
            "posted_entry_ids": list(done[0]) if done else [],
        }

    def _correction_dict(self, c: Correction) -> dict:
        return {
            "correction_id": c.correction_id,
            "attributable_year": c.year,
            "volunteer_id": c.volunteer_id,
            "red_entry_id": c.red_entry_id,
            "blue_entry_id": c.blue_entry_id,
            "reason": c.reason,
            "approver": c.approver,
            "created_date": c.created_date.isoformat(),
            "booked_period": c.period_id,
            "origin": c.origin,
        }
