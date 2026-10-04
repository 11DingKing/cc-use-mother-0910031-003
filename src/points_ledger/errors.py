"""异常体系。"""
from __future__ import annotations


class LedgerError(Exception):
    """所有业务异常的基类，HTTP 层统一映射为 4xx。"""


class NotFound(LedgerError):
    code = "not_found"


class Conflict(LedgerError):
    code = "conflict"


class PeriodClosed(Conflict):
    """结算期已封账，普通业务写入被拒绝。"""

    code = "period_closed"


class DuplicateReport(Conflict):
    """同一志愿服务被多个渠道重复报送——业务上进入待确认而非报错给调用方。"""

    code = "duplicate_report"


class ValidationError(LedgerError):
    code = "validation_error"


class PreconditionFailed(LedgerError):
    code = "precondition_failed"
