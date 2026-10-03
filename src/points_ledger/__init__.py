"""志愿服务积分结算服务端。

关键模块：

- ``store``：SQLite 事件库与投影表，写事务串行化。
- ``projection``：纯函数重放，从不可变事件重建积分分录，供在线入账与审计复算共用。
- ``service``：领域服务，负责命令校验、守恒约束与查询（逐笔来源解释、排名、复算）。
- ``server``：基于标准库 ``http.server`` 的 JSON HTTP 服务。
"""
from .service import LedgerService, ServiceError
from .store import Store
from . import projection

__all__ = ["LedgerService", "ServiceError", "Store", "projection"]
