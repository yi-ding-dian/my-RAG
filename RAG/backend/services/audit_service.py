"""审计操作日志服务

企业合规：关键操作（登录/用户/部门/知识库/文档/会话/配置）落库 audit_logs，
仅超级管理员可查询。

设计要点：
- **审计绝不能影响主流程**：record_action 用独立 session 自建事务提交，
  任何异常（连接失败/表缺失/序列化失败）仅 warning，绝不抛出；
- **埋点位置**：路由层业务成功后（或失败分支记录失败原因）调用，
  不改任何业务逻辑；
- **detail 为 JSON 字符串**（请求体关键字段摘要，如文档大小/解析方式），
  禁止记录密码等敏感信息；超过 1000 字符截断防表膨胀；
- **IP 获取**：X-Forwarded-For 优先（代理部署），其次 request.client.host。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from backend.db import get_session
from backend.models.user_models import AuditLogORM, gen_id, now_str

logger = logging.getLogger(__name__)

# 操作类型 → 中文名（前端筛选下拉 / 展示映射；与 /api/audit/actions 同源）
AUDIT_ACTION_LABELS: dict[str, str] = {
    "auth.login": "登录",
    "auth.change-password": "修改密码",
    "user.create": "创建用户",
    "user.update": "更新用户",
    "user.delete": "删除用户",
    "dept.create": "创建部门",
    "dept.update": "更新部门",
    "dept.delete": "删除部门",
    "kb.create": "创建知识库",
    "kb.update": "更新知识库",
    "kb.delete": "删除知识库",
    "kb.tags-update": "更新知识库标签",
    "kb.rebuild-vectors": "重建向量",
    "doc.upload": "上传文档",
    "doc.rename": "重命名文档",
    "doc.from-url": "网页导入",
    "doc.ingest": "解析文档",
    "doc.delete": "删除文档",
    "doc.purge": "彻底删除文档",
    "doc.restore": "恢复文档",
    "doc.trash-empty": "清空回收站",
    "chat.delete": "删除会话",
    "chat.export": "导出会话",
    "settings.create": "创建配置档案",
    "settings.update": "修改配置",
    "settings.delete": "删除配置档案",
    "settings.activate": "激活配置档案",
    "settings.test-connections": "测试连接",
    "settings.chat-update": "更新聊天设置",
    "ext.create": "创建外部查询",
    "ext.update": "编辑外部查询",
    "ext.delete": "删除外部查询",
    "ext.reset-token": "重置外部查询 Token",
    "ext.toggle": "切换外部查询状态",
}

_DETAIL_MAX_LEN = 1000


def get_client_ip(request) -> Optional[str]:
    """取客户端 IP：X-Forwarded-For 首段优先（代理部署），否则直连地址"""
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd and fwd.strip():
        return fwd.split(",")[0].strip()
    if getattr(request, "client", None):
        return request.client.host
    return None


def _dump_detail(detail: Any) -> Optional[str]:
    """detail 序列化为 JSON 字符串（超长截断）；非 dict 原样转 str"""
    if detail is None:
        return None
    if not isinstance(detail, str):
        try:
            detail = json.dumps(detail, ensure_ascii=False)
        except (TypeError, ValueError):
            detail = str(detail)
    if len(detail) > _DETAIL_MAX_LEN:
        detail = detail[:_DETAIL_MAX_LEN]
    return detail


def log_audit(db_session, user, action: str,
              target_type: Optional[str] = None,
              target_id: Optional[str] = None,
              target_name: Optional[str] = None,
              detail: Any = None,
              status: str = "success",
              request=None) -> None:
    """将审计记录加入给定 session（与业务事务同库提交/回滚）

    - user: UserPublic；登录失败场景可传 None（此时 username 留空，
      操作对象记在 target_name/detail 里）；
    - 构造失败仅 warning，不抛出（审计绝不能影响主流程）。
    """
    user_id = username = role = ""
    if user is not None:
        user_id = user.id
        username = getattr(user, "username", "") or ""
        role = getattr(user, "role", "") or ""
    try:
        db_session.add(AuditLogORM(
            id=gen_id(),
            user_id=user_id,
            username=username,
            role=role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name,
            detail=_dump_detail(detail),
            ip=get_client_ip(request),
            status=status,
            created_at=now_str(),
        ))
    except Exception as e:  # noqa: BLE001
        logger.warning("审计记录构造失败（不阻塞业务）: %s", str(e)[:150])


async def record_action(user, action: str,
                        target_type: Optional[str] = None,
                        target_id: Optional[str] = None,
                        target_name: Optional[str] = None,
                        detail: Any = None,
                        status: str = "success",
                        request=None) -> None:
    """独立 session 同步落库（与业务事务分离）

    路由埋点统一调用入口；内部任何异常仅 warning——审计失败绝不
    阻塞或改变主流程的返回结果。
    """
    try:
        async with get_session() as session:
            log_audit(session, user, action, target_type, target_id,
                      target_name, detail, status, request)
            await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("审计日志写入失败（不阻塞业务）: %s", str(e)[:150])
