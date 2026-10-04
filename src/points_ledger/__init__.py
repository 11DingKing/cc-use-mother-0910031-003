"""志愿服务积分结算服务端。

模块划分：

- ``models``        领域值对象（角色、场次状态、分录来源、审核决定）
- ``storage``       可插拔存储接口与线程安全的内存实现
- ``engine``        积分引擎：从签到/场次/角色/审核生成积分分录
- ``service``       应用服务：待确认队列、申诉、撤销、跨年度补录、封账、更正单、复算
- ``api``           基于 ``http.server`` 的 HTTP/JSON 接口
"""

__all__ = ["models", "storage", "engine", "service", "api"]
