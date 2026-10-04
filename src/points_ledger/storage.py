"""存储抽象与线程安全的内存实现。

存储层只负责数据与锁，不含业务规则；业务规则全部在 ``service`` 中。
内存实现用一把可重入锁保护所有可变结构，另用一个 ``Condition``
让并发封账排队（先到者封账，后来者直接得到已封账结果）。
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from datetime import date
from typing import Iterable, Optional

from .models import Correction, Entry, PendingItem, ReversalRecord, Review, Session, Checkin


class Store(ABC):
    """可替换为 SQLite/Postgres 的最小存储接口。"""

    # ---- 基础数据 ----
    @abstractmethod
    def upsert_session(self, session: Session) -> None: ...

    @abstractmethod
    def get_session(self, session_id: str) -> Optional[Session]: ...

    @abstractmethod
    def list_sessions(self) -> list[Session]: ...

    @abstractmethod
    def add_checkin(self, checkin: Checkin) -> None: ...

    @abstractmethod
    def get_checkin(self, checkin_id: str) -> Optional[Checkin]: ...

    @abstractmethod
    def list_checkins(self, volunteer_id: Optional[str] = None) -> list[Checkin]: ...

    @abstractmethod
    def add_review(self, review: Review) -> None: ...

    @abstractmethod
    def list_reviews(self) -> list[Review]: ...

    # ---- 账本 ----
    @abstractmethod
    def add_entry(self, entry: Entry) -> None: ...

    @abstractmethod
    def get_entry(self, entry_id: str) -> Optional[Entry]: ...

    @abstractmethod
    def iter_entries(self) -> Iterable[Entry]: ...

    @abstractmethod
    def add_pending(self, item: PendingItem) -> None: ...

    @abstractmethod
    def get_pending(self, pending_id: str) -> Optional[PendingItem]: ...

    @abstractmethod
    def list_pending(self, include_resolved: bool = False) -> list[PendingItem]: ...

    @abstractmethod
    def resolve_pending(self, pending_id: str, entry_ids: tuple[str, ...], resolution: str) -> None: ...

    @abstractmethod
    def pending_resolution(self, pending_id: str) -> Optional[tuple[tuple[str, ...], str]]: ...

    @abstractmethod
    def add_correction(self, correction: Correction) -> None: ...

    @abstractmethod
    def list_corrections(self, year: Optional[int] = None) -> list[Correction]: ...

    @abstractmethod
    def add_reversal(self, record: ReversalRecord) -> None: ...

    @abstractmethod
    def list_reversals(self, volunteer_id: Optional[str] = None) -> list[ReversalRecord]: ...

    # ---- 结算期 ----
    @abstractmethod
    def get_period(self, period_id: str) -> Optional[dict]: ...

    @abstractmethod
    def list_periods(self) -> list[dict]: ...

    @abstractmethod
    def create_period(self, period_id: str, year: int, label: str, closed: bool = False) -> dict: ...

    @abstractmethod
    def close_period(self, period_id: str, closed_date: date, snapshot: dict[str, int]) -> dict: ...


class InMemoryStore(Store):
    """线程安全的内存存储；测试与单进程部署可直接使用。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._close_gate = threading.Condition(self.lock)
        self._sessions: dict[str, Session] = {}
        self._checkins: dict[str, Checkin] = {}
        self._reviews: list[Review] = []
        self._entries: dict[str, Entry] = {}
        self._pendings: dict[str, PendingItem] = {}
        self._pending_done: dict[str, tuple[tuple[str, ...], str]] = {}
        self._corrections: list[Correction] = []
        self._reversals: list[ReversalRecord] = []
        self._periods: dict[str, dict] = {}

    # ---- sessions ----
    def upsert_session(self, session: Session) -> None:
        with self.lock:
            self._sessions[session.session_id] = session

    def get_session(self, session_id: str) -> Optional[Session]:
        with self.lock:
            return self._sessions.get(session_id)

    def list_sessions(self) -> list[Session]:
        with self.lock:
            return sorted(self._sessions.values(), key=lambda s: (s.service_date, s.session_id))

    # ---- checkins ----
    def add_checkin(self, checkin: Checkin) -> None:
        with self.lock:
            self._checkins[checkin.checkin_id] = checkin

    def get_checkin(self, checkin_id: str) -> Optional[Checkin]:
        with self.lock:
            return self._checkins.get(checkin_id)

    def list_checkins(self, volunteer_id: Optional[str] = None) -> list[Checkin]:
        with self.lock:
            values = list(self._checkins.values())
        if volunteer_id:
            values = [c for c in values if c.volunteer_id == volunteer_id]
        return sorted(values, key=lambda c: c.checkin_id)

    # ---- reviews ----
    def add_review(self, review: Review) -> None:
        with self.lock:
            self._reviews.append(review)

    def list_reviews(self) -> list[Review]:
        with self.lock:
            return list(self._reviews)

    # ---- entries ----
    def add_entry(self, entry: Entry) -> None:
        with self.lock:
            if entry.entry_id in self._entries:
                raise ValueError(f"分录编号冲突：{entry.entry_id}")
            self._entries[entry.entry_id] = entry

    def get_entry(self, entry_id: str) -> Optional[Entry]:
        with self.lock:
            return self._entries.get(entry_id)

    def iter_entries(self) -> Iterable[Entry]:
        with self.lock:
            return list(self._entries.values())

    # ---- pending ----
    def add_pending(self, item: PendingItem) -> None:
        with self.lock:
            self._pendings[item.pending_id] = item

    def get_pending(self, pending_id: str) -> Optional[PendingItem]:
        with self.lock:
            return self._pendings.get(pending_id)

    def list_pending(self, include_resolved: bool = False) -> list[PendingItem]:
        with self.lock:
            items = list(self._pendings.values())
            if not include_resolved:
                items = [i for i in items if i.pending_id not in self._pending_done]
            return sorted(items, key=lambda i: (i.created_date, i.pending_id))

    def resolve_pending(self, pending_id: str, entry_ids: tuple[str, ...], resolution: str) -> None:
        with self.lock:
            self._pending_done[pending_id] = (tuple(entry_ids), resolution)

    def pending_resolution(self, pending_id: str) -> Optional[tuple[tuple[str, ...], str]]:
        with self.lock:
            return self._pending_done.get(pending_id)

    # ---- corrections / reversals ----
    def add_correction(self, correction: Correction) -> None:
        with self.lock:
            self._corrections.append(correction)

    def list_corrections(self, year: Optional[int] = None) -> list[Correction]:
        with self.lock:
            values = list(self._corrections)
        if year is not None:
            values = [c for c in values if c.year == year]
        return sorted(values, key=lambda c: (c.created_date, c.correction_id))

    def add_reversal(self, record: ReversalRecord) -> None:
        with self.lock:
            self._reversals.append(record)

    def list_reversals(self, volunteer_id: Optional[str] = None) -> list[ReversalRecord]:
        with self.lock:
            values = list(self._reversals)
        if volunteer_id:
            values = [r for r in values if r.volunteer_id == volunteer_id]
        return sorted(values, key=lambda r: r.reversal_id)

    # ---- periods ----
    def get_period(self, period_id: str) -> Optional[dict]:
        with self.lock:
            p = self._periods.get(period_id)
            return dict(p) if p else None

    def list_periods(self) -> list[dict]:
        with self.lock:
            return [dict(p) for p in self._periods.values()]

    def create_period(self, period_id: str, year: int, label: str, closed: bool = False) -> dict:
        with self._close_gate:
            if period_id in self._periods:
                raise ValueError(f"结算期已存在：{period_id}")
            period = {
                "period_id": period_id,
                "year": year,
                "label": label,
                "closed": closed,
                "closed_date": None,
                "snapshot": None,
            }
            self._periods[period_id] = period
            return dict(period)

    def close_period(self, period_id: str, closed_date: date, snapshot: dict[str, int]) -> dict:
        """封账在条件变量内完成；并发调用串行化，只有第一个真正执行。

        快照只在首次封账时落定，之后的并发调用拿到同一张不可变快照。
        """
        with self._close_gate:
            period = self._periods.get(period_id)
            if period is None:
                raise KeyError(period_id)
            if not period["closed"]:
                period["closed"] = True
                period["closed_date"] = closed_date
                period["snapshot"] = dict(snapshot)
            self._close_gate.notify_all()
            return dict(period)
