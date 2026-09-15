"""统一日志对象：业务代码只描述「发生了什么」，级别/域/堆栈由这里定。

用法（模块顶部一行）::

    from backend.logger import AppLog
    log = AppLog(__name__)

    log.system_error("LLM 流式调用失败: %s", e)        # 系统级 → ERROR + 点红灯
    log.user_error("入库失败: %s（文档超阈值）", doc)   # 用户级 → ERROR，不点灯
    log.skipped("知识图谱抽取超时，跳过: chunk#%d", i)  # 跳过/回退 → WARNING
    log.info("入库完成: %s chunks=%d", doc, n)

底层还是标准库 logging（logger 名 = 模块名，日志里的来源不变），这里只是把
「该用什么级别、要不要点红灯、要不要记堆栈」收口到一处，调用方不用再手写
``extra={"fault": True}``，也不用纠结该 warning 还是 error。

想改级别就传枚举（**不收裸字符串**，避免拼错）::

    log.system_error("...", e, level=LogLevel.WARNING)

但**级别不影响点灯**——灯只看是不是 ``system_error``。
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any


class LogLevel(str, Enum):
    """日志级别枚举：覆盖默认级别时传它，别手写字符串

    继承 str，所以可以直接当级别名交给标准库 logging。
    """

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    @property
    def levelno(self) -> int:
        """标准库用的数值级别（"ERROR" → 40）"""
        return logging.getLevelName(self.value)  # type: ignore[return-value]


class AppLog:
    """统一日志对象：每个模块建一个 ``log = AppLog(__name__)``

    语义方法定「域」，域决定要不要点红灯；通用方法（info/warning/debug）只透传。
    """

    __slots__ = ("_logger",)

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    # ==================== 语义方法（域 + 默认级别在这里定） ====================

    def system_error(self, msg: str, *args: Any, level: LogLevel = LogLevel.ERROR,
                     exc_info: Any = None) -> None:
        """**系统级故障**：依赖服务不可用、未捕获异常等，影响全体 → 点亮红灯

        「Embedding 服务不可用」「LLM 崩溃」这类走这里；用户自己传错文档不算。
        默认 ERROR 级（可用枚举覆盖，但不影响点灯）；要堆栈传 exc_info=True。
        """
        self._log(level, msg, args, exc_info=exc_info, fault=True)

    def user_error(self, msg: str, *args: Any, level: LogLevel = LogLevel.ERROR,
                   exc_info: Any = None) -> None:
        """**用户级错误**：用户自己造成的失败（文档超阈值、格式不支持）→ 不点灯

        级别该是 ERROR 还是 ERROR——翻日志得知道这次操作失败了，只是不该让全站亮红灯。
        """
        self._log(level, msg, args, exc_info=exc_info)

    def skipped(self, msg: str, *args: Any, level: LogLevel = LogLevel.WARNING,
                exc_info: Any = None) -> None:
        """**跳过/回退**：超时跳过、单块失败跳过、回退切块——还能用 → 不点灯

        介于"正常"和"故障"之间：确实出了问题，但主流程兜住了。
        """
        self._log(level, msg, args, exc_info=exc_info)

    # ==================== 通用方法（方法名即级别，不覆盖） ====================

    def error(self, msg: str, *args: Any, exc_info: Any = None) -> None:
        """ERROR（无域语义，不点灯）：内部问题但不属于系统故障/用户操作"""
        self._logger.error(msg, *args, exc_info=exc_info)

    def warning(self, msg: str, *args: Any) -> None:
        self._logger.warning(msg, *args)

    def info(self, msg: str, *args: Any) -> None:
        self._logger.info(msg, *args)

    def debug(self, msg: str, *args: Any) -> None:
        self._logger.debug(msg, *args)

    # ==================== 内部 ====================

    def _log(self, level: LogLevel, msg: str, args: tuple, *,
             exc_info: Any = None, fault: bool = False) -> None:
        """统一出口：fault=True 时注入 extra 标记，由过滤器加 system. 前缀"""
        self._logger.log(
            level.levelno, msg, *args,
            exc_info=exc_info,
            extra={"fault": True} if fault else None,
            stacklevel=3,      # 指向业务调用方（_log ← system_error ← 调用处）
        )
