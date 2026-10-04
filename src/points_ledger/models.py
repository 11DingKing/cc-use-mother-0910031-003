"""领域模型：角色、场次状态、分录来源与审核决定。

所有值对象均为不可变 ``dataclass``，业务时间统一使用 ``date``，
不依赖具体数据库，便于复算与审计。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# ---------------------------------------------------------------- 枚举

class Role(str, enum.Enum):
    """志愿者在一场活动中的服务角色，决定积分系数。"""

    LEADER = "领队"
    DOCENT = "讲解员"
    ASSISTANT = "协助员"

    @property
    def factor(self) -> str:
        """以字符串返回系数，避免浮点误差，由引擎用 Decimal 解释。"""
        return {
            Role.LEADER: "1.5",
            Role.DOCENT: "1.2",
            Role.ASSISTANT: "1.0",
        }[self]


class SessionStatus(str, enum.Enum):
    """场次（活动排班）状态。

    只有 ``HELD`` 场次的签到可以产生积分；``CANCELLED`` 场次此前
    误发的积分必须生成红字（负向）分录冲回。
    """

    PLANNED = "计划中"
    HELD = "正常举办"
    CANCELLED = "取消"


class SourceKind(str, enum.Enum):
    """积分分录的业务来源。"""

    SERVICE = "service"          # 正常服务
    REVERSAL = "reversal"        # 场次取消 / 撤销冲回
    APPEAL = "appeal"            # 申诉成立补记
    LATE = "late"                # 跨年度补录
    CORRECTION = "correction"    # 封账后更正单


class EntryStatus(str, enum.Enum):
    PENDING = "pending"            # 待核验（属于待确认提议，尚未真正落账）
    POSTED = "posted"              # 已入账，全额计入余额
    PARTIALLY_REVERSED = "partially_reversed"  # 部分撤销，仍有在账余额
    REVERSED = "reversed"          # 已被全额冲回/更正，在账净值为 0
    REJECTED = "rejected"          # 核对/审核不成立，永不计入


class ReviewDecision(str, enum.Enum):
    PENDING = "pending"
    CONFIRM = "confirm"
    REJECT = "reject"
    KEEP_BOTH = "keep_both"      # 两笔来源其实不同（如同日不同场次），各自成立
    CANCEL_SESSION = "cancel_session"
    REVERSAL = "reversal"        # 人工部分/全额撤销
    APPEAL = "appeal"            # 申诉裁决


# ---------------------------------------------------------------- 输入实体

@dataclass(frozen=True, slots=True)
class Session:
    """一场活动排班。同一活动可以有多场（日期/时段不同）。"""

    session_id: str
    activity_id: str
    service_date: date
    hours: int
    status: SessionStatus = SessionStatus.PLANNED
    # 原始计划归属年度，用于跨年度补录：补录分录记到计划年度而非入账年度
    plan_year: Optional[int] = None

    @property
    def accounting_year(self) -> int:
        """积分归属年度：跨年度补录以 plan_year 为准。"""
        return self.plan_year if self.plan_year is not None else self.service_date.year


@dataclass(frozen=True, slots=True)
class Checkin:
    """志愿者签到记录。

    ``source`` 标识报送渠道（如 ``"school"`` / ``"venue"``），
    同一 (志愿者, 场次) 出现多个渠道即为重复报送。
    """

    checkin_id: str
    volunteer_id: str
    session_id: str
    role: Role
    source: str
    checkin_time: Optional[str] = None


@dataclass(frozen=True, slots=True)
class Review:
    """审核记录：对某笔签到/待确认项的人工决定。"""

    review_id: str
    ref_kind: str           # "checkin" | "pending" | "appeal"
    ref_id: str
    decision: ReviewDecision
    reviewer: str
    reason: str = ""
    created_at: Optional[str] = None


# ---------------------------------------------------------------- 分录

@dataclass(frozen=True, slots=True)
class Entry:
    """积分分录——账本中的最小不可变单位。

    分录一经生成即不可修改；更正与冲回永远通过新增对冲分录实现，
    从而保证任意时点的余额都可以逐笔重放复算。
    """

    entry_id: str
    volunteer_id: str
    year: int
    amount: int             # 整数积分，红冲为负；小数由规则取整后落地
    kind: SourceKind
    session_id: Optional[str]
    basis: str              # 人类可读的计提依据
    evidence: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    parent_entry_id: Optional[str] = None     # 冲回/更正指向的原分录
    pending_id: Optional[str] = None          # 由哪个待确认项核准而来
    created_date: Optional[date] = None       # 业务日期（补录时可能晚于归属年度）
    period_id: Optional[str] = None           # 落账结算期


@dataclass(frozen=True, slots=True)
class PendingItem:
    """待确认队列项：重复来源或暂不能自动成立的积分。"""

    pending_id: str
    volunteer_id: str
    session_id: str
    checkin_ids: tuple[str, ...]
    proposed_entry: Entry
    reason: str
    created_date: date


@dataclass(frozen=True, slots=True)
class Correction:
    """封账后的更正单：红字 + 蓝字成对，当期净额必为 0 或为申报调整额。

    ``origin`` 标明更正来源，供审计复算区分"可从原始事实推出的更正"
    （取消/重复核对/申诉/补录）与"人工裁决型手工更正"：
    ``manual / cancel / duplicate / appeal / late``。
    """

    correction_id: str
    year: int
    volunteer_id: str
    red_entry_id: Optional[str]
    blue_entry_id: Optional[str]
    reason: str
    approver: str
    created_date: date
    period_id: str
    origin: str = "manual"


@dataclass(frozen=True, slots=True)
class ReversalRecord:
    """部分撤销/取消冲回记录，便于审计追踪对冲链。

    ``origin`` 区分冲回来源：``partial``（人工部分撤销，属裁决事实）、
    ``cancel``（场次取消，可由场次状态推导）、``duplicate``
    （重复报送挂起，可由待确认结论推导）。
    """

    reversal_id: str
    volunteer_id: str
    original_entry_id: str
    reversal_entry_id: str
    reason: str
    created_date: date
    period_id: Optional[str]
    origin: str = "partial"
