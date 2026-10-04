"""积分引擎：纯函数式的计提规则与分录构造。

规则与 I/O 无关，审计复算时可直接对原始签到/场次重放本模块，
因此所有函数都不读存储、不产生随机 ID（ID 由调用方传入）。

积分规则（可在此集中调整，复算时对历史期使用同一规则即可）：

    每场积分 = 服务小时 × 每小时积分(BASE_POINTS_PER_HOUR) × 角色系数
    结果按四舍五入(ROUND_HALF_UP)取整，账本内只存整数积分。
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from .models import (
    Checkin,
    Entry,
    Role,
    Session,
    SessionStatus,
    SourceKind,
)

BASE_POINTS_PER_HOUR = 10


def compute_points(role: Role, hours: int) -> int:
    """按角色系数计算一场服务的积分。"""
    if hours <= 0:
        raise ValueError("服务小时数必须为正整数")
    amount = Decimal(hours) * Decimal(BASE_POINTS_PER_HOUR) * Decimal(role.factor)
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def explain_points(role: Role, hours: int) -> tuple[int, tuple[tuple[str, str], ...]]:
    """返回 (积分, 计算过程证据)，证据逐笔挂在分录上供查询解释。"""
    amount = compute_points(role, hours)
    evidence = (
        ("规则", f"{hours} 小时 × {BASE_POINTS_PER_HOUR} 分/小时 × 角色系数 {role.factor}（{role.value}）"),
        ("取整", "ROUND_HALF_UP"),
        ("结果", f"{amount} 分"),
    )
    return amount, evidence


def detect_duplicates(checkins: list[Checkin]) -> dict[tuple[str, str], list[Checkin]]:
    """按 (志愿者, 场次) 分组，返回疑似重复报送的分组。

    一名志愿者在同一场次至多有一次有效服务，因此同一组出现两个以上
    不同 checkin_id 即为疑点——无论来自不同渠道（学校/场馆双报）
    还是同一渠道重复提交（重发/双击）。同一 checkin_id 重复导入幂等，
    不算疑点。
    """
    groups: dict[tuple[str, str], list[Checkin]] = {}
    for c in checkins:
        groups.setdefault((c.volunteer_id, c.session_id), []).append(c)
    dup: dict[tuple[str, str], list[Checkin]] = {}
    for key, items in groups.items():
        unique = {c.checkin_id for c in items}
        if len(unique) > 1:
            dup[key] = sorted(items, key=lambda c: c.checkin_id)
    return dup


def build_service_entry(
    entry_id: str,
    checkin: Checkin,
    session: Session,
    *,
    pending_id: Optional[str] = None,
    on_date: Optional[date] = None,
) -> Entry:
    """由签到 + 场次构造正常服务分录。

    只有 ``HELD`` 场次可计提；归属年度取场次的会计年度
    （跨年度补录的场次设置 ``plan_year``）。
    """
    if session.status is not SessionStatus.HELD:
        raise ValueError(f"场次 {session.session_id} 状态为 {session.status.value}，不可计提服务积分")
    amount, calc = explain_points(checkin.role, session.hours)
    basis = f"{session.activity_id} 场次 {session.session_id} {checkin.role.value} 服务 {session.hours} 小时"
    evidence = (
        ("签到", checkin.checkin_id),
        ("场次", session.session_id),
        ("活动", session.activity_id),
        ("报送渠道", checkin.source),
        ("服务日期", session.service_date.isoformat()),
        *calc,
    )
    if pending_id:
        evidence = (("待确认单", pending_id), *evidence)
    return Entry(
        entry_id=entry_id,
        volunteer_id=checkin.volunteer_id,
        year=session.accounting_year,
        amount=amount,
        kind=SourceKind.SERVICE,
        session_id=session.session_id,
        basis=basis,
        evidence=evidence,
        pending_id=pending_id,
        created_date=on_date or session.service_date,
    )


def build_reversal_entry(
    entry_id: str,
    original: Entry,
    reason: str,
    *,
    kind: SourceKind = SourceKind.REVERSAL,
    on_date: Optional[date] = None,
) -> Entry:
    """构造红字冲回分录，金额与原分录相反，其余维度保持一致。"""
    return Entry(
        entry_id=entry_id,
        volunteer_id=original.volunteer_id,
        year=original.year,
        amount=-original.amount,
        kind=kind,
        session_id=original.session_id,
        basis=f"冲回分录 {original.entry_id}：{reason}",
        evidence=(
            ("原分录", original.entry_id),
            ("原金额", f"{original.amount}"),
            ("冲回金额", f"{-original.amount}"),
            ("原因", reason),
        ),
        parent_entry_id=original.entry_id,
        created_date=on_date or date.today(),
    )


def build_appeal_entry(
    entry_id: str,
    volunteer_id: str,
    year: int,
    amount: int,
    reason: str,
    appeal_id: str,
    *,
    session_id: Optional[str] = None,
    on_date: Optional[date] = None,
) -> Entry:
    """申诉成立的补记分录取，挂申诉单编号作为证据。"""
    if amount <= 0:
        raise ValueError("申诉补记金额必须为正；如需扣减请走撤销/更正流程")
    return Entry(
        entry_id=entry_id,
        volunteer_id=volunteer_id,
        year=year,
        amount=amount,
        kind=SourceKind.APPEAL,
        session_id=session_id,
        basis=f"申诉 {appeal_id} 成立补记：{reason}",
        evidence=(
            ("申诉单", appeal_id),
            ("补记金额", f"{amount}"),
            ("原因", reason),
        ),
        created_date=on_date or date.today(),
    )


def build_late_entries(
    ids: list[str],
    checkins: list[Checkin],
    sessions_by_id: dict[str, Session],
    recorded_on: date,
) -> list[Entry]:
    """跨年度补录：分录归属 ``plan_year``（场次的计划年度），
    ``created_date`` 记实际补录日期，查询时可区分"归属年度"与"入账日期"。

    若该计划年度已封账，调用方（service）会拒绝或转更正单；
    本函数只负责构造分录。``ids`` 与 ``checkins`` 等长一一对应。
    """
    if len(ids) != len(checkins):
        raise ValueError("分录 ID 数量必须与签到一致")
    entries: list[Entry] = []
    for entry_id, checkin in zip(ids, checkins):
        session = sessions_by_id[checkin.session_id]
        if session.plan_year is None:
            raise ValueError(f"跨年度补录场次 {session.session_id} 必须显式设置 plan_year")
        if session.plan_year >= recorded_on.year:
            raise ValueError("仅当年发现的以前年度遗漏才适用跨年度补录")
        entry = build_service_entry(entry_id, checkin, session, on_date=recorded_on)
        # build_service_entry 已按 accounting_year(=plan_year) 归属
        entry = Entry(
            entry_id=entry.entry_id,
            volunteer_id=entry.volunteer_id,
            year=entry.year,
            amount=entry.amount,
            kind=SourceKind.LATE,
            session_id=entry.session_id,
            basis="跨年度补录：" + entry.basis,
            evidence=(("补录日期", recorded_on.isoformat()), *entry.evidence),
            pending_id=entry.pending_id,
            created_date=recorded_on,
        )
        entries.append(entry)
    return entries
