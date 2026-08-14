"""认证服务：bcrypt 密码哈希 + JWT 签发/解码 + 登录校验

- 密码：bcrypt 直接调用（不用 passlib），单次哈希成本约 0.2~0.3s；
- JWT：PyJWT HS256，24h 过期，sub=user_id（字符串），密钥取
  config.settings.JWT_SECRET（仅 .env 注入，生产必须改为强随机值）；
- 登录：查用户名 + verify_password + status==active 三重校验。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt

from backend.config import settings as config_settings
from backend.models.user_models import STATUS_ACTIVE, UserORM

logger = logging.getLogger(__name__)

# token 有效期
TOKEN_EXPIRE_HOURS = 24
# bcrypt 算法天然限制 72 字节，超长密码截断避免异常
_BCRYPT_MAX_BYTES = 72


def _pw_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """bcrypt 哈希（返回 str，入库）"""
    return bcrypt.hashpw(_pw_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """校验明文密码与哈希是否匹配"""
    try:
        return bcrypt.checkpw(_pw_bytes(password), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: str) -> str:
    """签发 JWT（HS256，24h 过期，sub=user_id）"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(hours=TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, config_settings.JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> Optional[str]:
    """解码 JWT，成功返回 user_id，失败/过期返回 None"""
    try:
        payload = jwt.decode(token, config_settings.JWT_SECRET,
                             algorithms=["HS256"])
        sub = payload.get("sub")
        return str(sub) if sub else None
    except jwt.PyJWTError:
        return None


async def login(db, username: str, password: str) -> Optional[UserORM]:
    """用户名+密码登录：成功返回 UserORM（status 必须 active），否则 None"""
    from sqlalchemy import select
    result = await db.execute(
        select(UserORM).where(UserORM.username == username))
    user = result.scalar_one_or_none()
    if user is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    if user.status != STATUS_ACTIVE:
        logger.info("登录被拒（账号已禁用）: %s", username)
        return None
    return user
