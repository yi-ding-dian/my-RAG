"""日志统一管理。

业务代码只需要::

    from backend.logger import AppLog
    log = AppLog(__name__)

    log.system_error("LLM 流式调用失败: %s", e)   # 系统级 → 点红灯
    log.user_error("入库失败: %s", doc_id)        # 用户级 → 不点灯
    log.skipped("超时跳过: chunk#%d", i)          # 跳过/回退 → 不点灯

目录里其余文件是基础设施：`filters`（系统级标记）、`handlers`（按天落盘）、
`alert`（红灯上报口子），业务代码一般不用直接碰。
"""
from backend.logger.app_log import AppLog, LogLevel
from backend.logger.filters import SYSTEM_PREFIX, SystemFaultFilter

__all__ = ["AppLog", "LogLevel", "SYSTEM_PREFIX", "SystemFaultFilter"]
