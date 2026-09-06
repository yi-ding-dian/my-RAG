#!/usr/bin/env bash
# ============================================================================
# my-RAG 知识库系统 · 安全基线自检脚本（部署/发布前跑一遍，开箱即过等保预检）
#
# 检查项:
#   1. JWT_SECRET 是否配置且强度足够（≥16 位随机，默认空/弱值拒绝启动）
#   2. data/settings.json 权限是否 600（含全部 api_key，同机不可读）
#   3. 其他敏感文件权限（.env* 同样 600 提醒）
#   4. CORS 出厂是否通配（提示按需收紧）
#   5. HTTPS 启用状态提示（nginx 默认 HTTP，现场合规需按 docker/nginx.conf 注释开启）
# 用法: bash scripts/security_check.sh
# ============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

PASS=0
WARN=0
FAIL=0

check_ok()   { PASS=$((PASS+1)); echo "[OK]   $1"; }
check_warn() { WARN=$((WARN+1)); echo "[WARN] $1"; }
check_fail() { FAIL=$((FAIL+1)); echo "[FAIL] $1"; }

echo "=============================================="
echo "  my-RAG 安全基线自检"
echo "=============================================="

# 1) JWT_SECRET 强度
JWT_SECRET=""
if [ -f .env ]; then
    JWT_SECRET=$(grep -E '^JWT_SECRET=' .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
fi
if [ -z "$JWT_SECRET" ]; then
    check_fail "JWT_SECRET 未配置（后端会拒绝启动；请设置 ≥16 位随机值）"
elif [ ${#JWT_SECRET} -lt 16 ]; then
    check_fail "JWT_SECRET 长度 ${#JWT_SECRET} < 16（建议高强度随机值）"
elif [ "$JWT_SECRET" = "change-me" ] || [ "$JWT_SECRET" = "secret" ]; then
    check_warn "JWT_SECRET 疑似默认弱值，请更换"
else
    check_ok "JWT_SECRET 已配置（长度 ${#JWT_SECRET}）"
fi

# 2) settings.json 权限
SETTINGS="data/settings.json"
if [ -f "$SETTINGS" ]; then
    PERM=$(stat -c %a "$SETTINGS" 2>/dev/null || stat -f %Lp "$SETTINGS")
    if [ "$PERM" = "600" ]; then
        check_ok "settings.json 权限 600"
    else
        check_fail "settings.json 权限为 $PERM（应为 600，含全部 api_key）"
    fi
else
    check_warn "settings.json 不存在（未保存过配置档案，首次保存时自动收紧 600）"
fi

# 3) .env 权限提醒
if [ -f .env ]; then
    PERM=$(stat -c %a .env 2>/dev/null || stat -f %Lp .env)
    if [ "$PERM" = "600" ]; then
        check_ok ".env 权限 600"
    else
        check_warn ".env 权限为 $PERM（含数据库/对象存储口令，建议 chmod 600）"
    fi
fi

# 4) CORS 检查（.env 或 docker-compose 若有显式配置）
CORS_SET=$(grep -E '(CORS_ORIGINS|CORS_ALLOW)' .env docker/docker-compose*.yml 2>/dev/null | grep -v '^#' | head -2)
if [ -z "$CORS_SET" ]; then
    check_warn "未显式配置 CORS（出厂默认放开，公网/多租户环境请按来源收紧）"
else
    check_ok "CORS 已显式配置"
fi

# 5) HTTPS 提示
NGINX="docker/nginx.conf"
if grep -q "listen 443 ssl" "$NGINX" 2>/dev/null; then
    check_warn "nginx 已启用 443 ssl 示例段（确认证书路径已填并启用，未启用则默认 HTTP）"
else
    check_warn "nginx 默认 HTTP（客户现场合规要求下，按 docker/nginx.conf 注释开启 HTTPS）"
fi

echo "----------------------------------------------"
echo "  结果: $PASS OK / $WARN 提醒 / $FAIL 失败"
if [ "$FAIL" -gt 0 ]; then
    echo "  -> 存在失败项，请修复后再对外发布"
    exit 1
fi
echo "  -> 基线通过（提醒项按场景处理）"
exit 0
