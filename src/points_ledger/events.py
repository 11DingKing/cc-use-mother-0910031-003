"""不可变事件定义与序列化。

积分系统的全部事实（签到、场次状态、审核、重复报送裁决、申诉、部分撤销、
更正单、期间开/封账）都只以"追加事件"表达，不做就地修改。投影层
(:mod:`points_ledger.projection`) 负责把事件流重放成积分分录。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

# --- 事件类型 ---------------------------------------------------------------

VOLUNTEER_REGISTERED = "volunteer_registered"
PERIOD_OPENED = "period_opened"
PERIOD_CLOSED = "period_closed"
SESSION_STATUS_CHANGED = "session_status_changed"
REPORT_RECEIVED = "report_received"
CHECKIN_RECORDED = "checkin_recorded"
REVIEW_RECORDED = "review_recorded"
DUPLICATE_RESOLVED = "duplicate_resolved"
APPEAL_SUBMITTED = "appeal_submitted"
APPEAL_DECIDED = "appeal_decided"
SERVICE_REVOKED = "service_revoked"
CORRECTION_VOUCHERED = "correction_vouchered"

ALL_TYPES = frozenset(
    {
        VOLUNTEER_REGISTERED,
        PERIOD_OPENED,
        PERIOD_CLOSED,
        SESSION_STATUS_CHANGED,
        REPORT_RECEIVED,
        CHECKIN_RECORDED,
        REVIEW_RECORDED,
        DUPLICATE_RESOLVED,
        APPEAL_SUBMITTED,
        APPEAL_DECIDED,
        SERVICE_REVOKED,
        CORRECTION_VOUCHERED,
    }
)

# 场次状态机：报备 -> 完成（可入账）/ 取消（必须冲回）-> 申诉成立后可恢复完成。
SESSION_SCHEDULED = "scheduled"
SESSION_COMPLETED = "completed"
SESSION_CANCELLED = "cancelled"
SESSION_STATUSES = frozenset({SESSION_SCHEDULED, SESSION_COMPLETED, SESSION_CANCELLED})

# 报送来源：学校与场馆两条线，正是重复报送的根源。
SOURCE_SCHOOL = "school"
SOURCE_VENUE = "venue"
SOURCES = frozenset({SOURCE_SCHOOL, SOURCE_VENUE})


@dataclass(frozen=True, slots=True)
class Event:
    """一条不可变领域事件。"""

    seq: int  # 全局单调序号，由存储层分配，也是重放顺序的唯一权威
    type: str
    payload: dict
    created_at: str  # UTC ISO-8601，事件进入账本的时间（记账时间）

    def to_row(self) -> tuple[int, str, str, str]:
        return self.seq, self.type, json.dumps(self.payload, ensure_ascii=False, sort_keys=True), self.created_at

    @staticmethod
    def from_row(row: tuple[int, str, str, str]) -> "Event":
        return Event(seq=row[0], type=row[1], payload=json.loads(row[2]), created_at=row[3])


def now_iso() -> str:
    """事件时间戳：统一 UTC，单调序号负责裁决同一时刻的先后。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def year_of(date_text: str) -> str:
    """从 ISO 日期/时间字符串中取出年度（服务发生年度）。"""
    if not isinstance(date_text, str) or len(date_text) < 4 or not date_text[:4].isdigit():
        raise ValueError(f"无法解析日期：{date_text!r}")
    return date_text[:4]


def dedup_key(session_id: str, volunteer_id: str) -> str:
    """同一志愿者在同一场次的多条报送互为重复嫌疑。"""
    return f"{session_id}@{volunteer_id}"


def group_id_for(session_id: str, volunteer_id: str) -> str:
    return f"grp:{dedup_key(session_id, volunteer_id)}"
