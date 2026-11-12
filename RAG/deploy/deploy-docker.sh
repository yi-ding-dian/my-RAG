#!/bin/bash
# ============================================================
# deploy/deploy-docker.sh — my-RAG 一键 Docker 部署（全新部署）
#
# 用法:
#   ./deploy/deploy-docker.sh           # 交互模式：先展示部署信息，回车后开始
#   ./deploy/deploy-docker.sh --yes     # 跳过确认，直接开始（CI/批量场景）
#   ./deploy/deploy-docker.sh --dry-run # 只展示部署计划，不执行任何操作
#   ./deploy/deploy-docker.sh --help    # 查看帮助
#
# 说明:
#   - 只做**全新部署**；检测到已有部署（.env.docker 或容器存在）会提示并退出
#   - 每步打印开始/结束时间、单步耗时、累计耗时
#   - 完整日志落盘到 deploy/logs/deploy-YYYYmmdd-HHMMSS.log
#   - 失败即停，不自动清理容器/数据，便于排查
#   - 需要自定义数据库名/端口/存储后端等，改下方「可自定义配置」区即可
# ============================================================

set -euo pipefail

# ==================== 可自定义配置（不改则用默认值）====================
# 两种修改方式（二选一）：
#   ① 直接改下面的默认值
#   ② 运行时用环境变量覆盖，如：
#        STORAGE_BACKEND=minio VECTOR_BACKEND=milvus ./deploy/deploy-docker.sh --dry-run


# ---- 数据库 ----
MYSQL_DATABASE="${MYSQL_DATABASE:-my_rag}"          # 业务库名
MYSQL_USER="${MYSQL_USER:-app_rag}"             # 业务账号
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-}"           # root 口令；留空 = 自动生成随机强口令
MYSQL_PASSWORD="${MYSQL_PASSWORD:-}"                # 业务口令；留空 = 自动生成随机强口令
INSTALL_MYSQL="${INSTALL_MYSQL:-yes}"              # yes = 用项目自带 infra 起 MySQL；no = 使用已有 MySQL

# ---- 认证 ----
JWT_SECRET="${JWT_SECRET:-}"                    # 留空 = 自动生成（64 位十六进制）

# ---- 端口（宿主机端口；容器内固定为 8091 / 80 / 3306）----
HTTP_PORT="${HTTP_PORT:-8081}"                 # 前端访问端口
BACKEND_PORT="${BACKEND_PORT:-8091}"              # 后端 API 端口
MYSQL_PORT="${MYSQL_PORT:-5455}"                # MySQL 映射端口

# ---- 对象存储（文件/解析图片）----
#   local : 存本地 data/storage，不依赖外部服务（默认，最省事）
#   minio : 使用 MinIO，本脚本可按需自动启动（见 INSTALL_MINIO）
STORAGE_BACKEND="${STORAGE_BACKEND:-minio}"
INSTALL_MINIO="${INSTALL_MINIO:-auto}"             # auto = STORAGE_BACKEND=minio 时自动起；yes / no
MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-}"              # 留空 = 自动生成
MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-}"              # 留空 = 自动生成
MINIO_BUCKET="${MINIO_BUCKET:-my-rag}"
MINIO_PORT="${MINIO_PORT:-9000}"                 # MinIO 宿主机 API 端口
MINIO_CONSOLE_PORT="${MINIO_CONSOLE_PORT:-9001}" # MinIO 宿主机控制台端口
EXTERNAL_MINIO_ENDPOINT="${EXTERNAL_MINIO_ENDPOINT:-}"  # INSTALL_MINIO=no 且用 minio 时填（如 10.0.0.8:9000）

# ---- 向量存储 ----
#   chroma : 嵌入式文件库，随 data/ 持久化（默认，零外部依赖）
#   milvus : 服务化向量库，本脚本可按需自动启动 etcd+minio+milvus
VECTOR_BACKEND="${VECTOR_BACKEND:-milvus}"
INSTALL_MILVUS="${INSTALL_MILVUS:-auto}"            # auto = VECTOR_BACKEND=milvus 时自动起；yes / no
MILVUS_PORT="${MILVUS_PORT:-19530}"              # Milvus 宿主机 gRPC 端口
MILVUS_METRICS_PORT="${MILVUS_METRICS_PORT:-9091}"  # Milvus 健康检查/指标端口
MILVUS_URI="${MILVUS_URI:-http://host.docker.internal:${MILVUS_PORT}}"

# ---- 文档解析 ----
#   MinerU 是外部解析服务（镜像 mineru:latest 需预构建、约 12GB，且需要 GPU）。
#   默认不启动；已有该镜像和 GPU 的机器可设 INSTALL_MINERU=yes。
INSTALL_MINERU="${INSTALL_MINERU:-no}"
MINERU_PORT="${MINERU_PORT:-8001}"
MINERU_PROFILE="${MINERU_PROFILE:-api}"          # compose profile：api（8001）/ openai-server（30000）

# ---- 网络加速（构建时使用；可改为官方源）----
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"
DOCKER_MIRROR="${DOCKER_MIRROR:-docker.m.daocloud.io}"   # Docker Hub 镜像代理

# ===================================================================

# ---------- 路径与全局 ----------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"      # RAG 项目根
COMPOSE_FILE="$PROJECT_DIR/docker/docker-compose.yml"
INFRA_SRC="$PROJECT_DIR/docker/docker-compose.infra.yml"   # 仓库原文件（不改动）
INFRA_RUN="$PROJECT_DIR/docker/.deploy-infra.yml"          # 部署用副本（含真实口令，已 gitignore）
MILVUS_FILE="$PROJECT_DIR/docker/Milvus/docker-compose.yml"
MILVUS_RUN="$PROJECT_DIR/docker/.deploy-milvus.yml"        # 部署用副本（端口改写，已 gitignore）
MINERU_FILE="$PROJECT_DIR/docker/MinerU/compose.yaml"
MINERU_RUN="$PROJECT_DIR/docker/.deploy-mineru.yaml"       # 部署用副本（容器名改写，已 gitignore）
ENV_EXAMPLE="$PROJECT_DIR/docker/.env.docker.example"
ENV_FILE="$PROJECT_DIR/docker/.env.docker"
DATA_DIR="$PROJECT_DIR/data"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/deploy-$(date +%Y%m%d-%H%M%S).log"

# Compose 项目名：三个编排文件都在 docker/ 下、默认项目名同为 "docker"，
# 而 infra 与 Milvus 都定义了名为 minio 的服务 —— 不隔离的话启动 Milvus
# 会把主 MinIO 容器当成同一服务重建掉。约定：
#   my-rag-infra : MySQL + MinIO
#   my-rag-milvus: Milvus 全家桶（etcd + 内部 minio + standalone）
#   my-rag-app   : 应用（后端 + 前端）
PROJECT_INFRA="my-rag-infra"
PROJECT_MILVUS="my-rag-milvus"
PROJECT_APP="my-rag-app"

TOTAL_STEPS=13
START_TIME=$SECONDS
STEP_START=$SECONDS
STEP_NO=0
STEP_NAME=""

ASSUME_YES=0
DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes) ASSUME_YES=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --dry-run=*) DRY_RUN=1 ;;
        -h|--help)
            sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "未知参数: $arg（用 --help 查看用法）" >&2; exit 2 ;;
    esac
done

mkdir -p "$LOG_DIR"
: > "$LOG_FILE"
chmod 600 "$LOG_FILE"      # 日志可能含生成的口令，收紧权限

# ---------- 输出与计时 ----------
if [ -t 1 ]; then
    C_BOLD=$'\033[1m'; C_BLUE=$'\033[36m'; C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_RESET=$'\033[0m'
else
    C_BOLD=""; C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_RESET=""
fi

# 屏幕保留颜色；日志文件写入去掉 ANSI 转义后的纯文本（便于 grep/排查）
log() {
    printf '%s\n' "$*"
    printf '%s\n' "$*" | sed 's/\x1b\[[0-9;]*m//g' >> "$LOG_FILE"
}
log_raw(){ echo "$*" >> "$LOG_FILE"; }          # 仅写文件
info()   { log "  $*"; }
warn()   { log "  ${C_YELLOW}⚠ $*${C_RESET}"; }
fail()   { log "  ${C_RED}✗ $*${C_RESET}"; }

fmt_duration() {
    local s=${1:-0}
    if   [ "$s" -ge 3600 ]; then printf "%dh%02dm%02ds" $((s/3600)) $(((s%3600)/60)) $((s%60))
    elif [ "$s" -ge 60 ];   then printf "%dm%02ds"     $((s/60))   $((s%60))
    else                         printf "%ds" "$s"
    fi
}

step_start() {
    STEP_NO=$((STEP_NO + 1))
    STEP_NAME="$1"
    STEP_START=$SECONDS
    log ""
    log "${C_BLUE}[$(date +%H:%M:%S)] ▶ 步骤 ${STEP_NO}/${TOTAL_STEPS}  ${STEP_NAME}${C_RESET}"
}

step_done() {
    local cost=$((SECONDS - STEP_START))
    log "${C_GREEN}[$(date +%H:%M:%S)] ✓ 步骤 ${STEP_NO} 完成  （本步 $(fmt_duration "$cost")｜累计 $(fmt_duration $((SECONDS - START_TIME)))）${C_RESET}"
}

die() {
    fail "$1"
    log "  ${C_RED}部署中断于：步骤 ${STEP_NO}「${STEP_NAME}」${C_RESET}"
    log "  完整日志：$LOG_FILE"
    exit 1
}

trap 'die "脚本异常退出（行 $LINENO）"' ERR

# ---------- 工具函数 ----------
gen_secret() { openssl rand -hex "${1:-16}"; }

# sed 替换安全转义（口令可能含 | & \ 等字符）
sed_escape() { printf '%s' "$1" | sed 's/[|&\\]/\\&/g'; }

port_free() {
    ! ss -ltn 2>/dev/null | grep -qE "[:.]${1}[[:space:]]"
}

host_ip() {
    if hostname -I 2>/dev/null | awk '{print $1}' | grep -q .; then
        hostname -I | awk '{print $1}'
    else
        echo "localhost"
    fi
}

# ---------- 按需开关解析 ----------
use_minio=0
[ "$STORAGE_BACKEND" = "minio" ] && use_minio=1 || true
[ "$INSTALL_MINIO" = "yes" ] && use_minio=1 || true
[ "$INSTALL_MINIO" = "no" ] && use_minio=0 || true

use_milvus=0
[ "$VECTOR_BACKEND" = "milvus" ] && use_milvus=1 || true
[ "$INSTALL_MILVUS" = "yes" ] && use_milvus=1 || true
[ "$INSTALL_MILVUS" = "no" ] && use_milvus=0 || true

use_mineru=0
[ "$INSTALL_MINERU" = "yes" ] && use_mineru=1 || true

# ============================================================
# 部署信息确认
# ============================================================
IP=$(host_ip)

show_plan() {
    local minio_line minio_account_line milvus_line milvus_note mysql_line
    if [ "$INSTALL_MYSQL" = "yes" ]; then
        mysql_line="宿主机端口 ${MYSQL_PORT}（容器 my-rag-mysql）"
    elif [ -n "$EXTERNAL_MYSQL_HOST" ]; then
        mysql_line="使用已有 MySQL：${EXTERNAL_MYSQL_HOST}:${EXTERNAL_MYSQL_PORT}"
    else
        mysql_line="${C_RED}未配置！INSTALL_MYSQL=no 时必须设置 EXTERNAL_MYSQL_HOST/PORT${C_RESET}"
    fi

    if [ "$use_minio" -eq 1 ]; then
        minio_line="（自动启动容器 my-rag-minio，端口 ${MINIO_PORT}/${MINIO_CONSOLE_PORT}）"
        minio_account_line="
                账号 ${MINIO_ACCESS_KEY:-（自动生成）} / 桶 ${MINIO_BUCKET}"
    else
        minio_line="（本地目录 data/storage，无需外部服务）"
        minio_account_line=""
    fi
    if [ "$use_milvus" -eq 1 ]; then
        milvus_line="（自动启动 etcd + minio + milvus，端口 ${MILVUS_PORT}/${MILVUS_METRICS_PORT}）"
        milvus_note="${C_YELLOW}提示：Milvus 镜像来自 quay.io / Docker Hub，国内网络可能需要预拉取或代理${C_RESET}"
    else
        milvus_line="（嵌入式 Chroma，随 data/ 持久化）"
        milvus_note=""
    fi

    cat <<EOF

${C_BOLD}══════════════════════════════════════════════════════════${C_RESET}
${C_BOLD}  my-RAG 一键部署（Docker）${C_RESET}
${C_BOLD}══════════════════════════════════════════════════════════${C_RESET}

${C_BOLD}【部署内容】${C_RESET}
  项目目录      $PROJECT_DIR
  前端界面      http://${IP}:${HTTP_PORT}
  后端 API      http://${IP}:${BACKEND_PORT}/api/health

  ${C_BOLD}MySQL${C_RESET}
                ${mysql_line}
                库 ${MYSQL_DATABASE} / 用户 ${MYSQL_USER}
                业务口令 ${MYSQL_PASSWORD:-（自动生成）} / root ${MYSQL_ROOT_PASSWORD:-（自动生成）}

  ${C_BOLD}对象存储${C_RESET}      ${STORAGE_BACKEND} ${minio_line}${minio_account_line}

  ${C_BOLD}向量存储${C_RESET}      ${VECTOR_BACKEND} ${milvus_line}

  JWT_SECRET    ${JWT_SECRET:-（自动生成）}
  数据目录      $DATA_DIR

${C_BOLD}【将执行 ${TOTAL_STEPS} 个步骤】${C_RESET}
   1. 环境检查（Docker 版本 / 端口 / 磁盘 / 目录结构 / 已部署检测）
   2. 生成部署配置（docker/.env.docker + 基础服务部署副本）
   3. 拉取基础镜像（python/node/nginx/mysql/minio，直连失败自动切代理）
   4. $([ "$INSTALL_MYSQL" = "yes" ] && echo "启动 MySQL 容器（${MYSQL_PORT}）" || echo "MySQL：跳过（使用已有 ${EXTERNAL_MYSQL_HOST}:${EXTERNAL_MYSQL_PORT}）")
   5. 校验数据库与账号连通性
   6. 对象存储：$([ "$use_minio" -eq 1 ] && echo "启动 MinIO 容器（${MINIO_PORT}/${MINIO_CONSOLE_PORT}）" || echo "跳过（使用 local）")
   7. 向量存储：$([ "$use_milvus" -eq 1 ] && echo "启动 Milvus（etcd + minio + standalone，${MILVUS_PORT}）" || echo "跳过（使用 chroma）")
   8. 文档解析：$([ "$use_mineru" -eq 1 ] && echo "启动 MinerU（${MINERU_PORT}）" || echo "跳过（INSTALL_MINERU=no，需 GPU + 预构建镜像）")
   9. 准备数据目录（属主 1000:1000）
  10. 构建应用镜像（预计 10-25 分钟，最耗时的一步）
  11. 启动应用容器（后端 healthy 后起前端）
  12. 健康验证（应用 / 存储 / 向量库 / 解析服务）
  13. 输出部署报告（含访问地址与全部口令）

${C_BOLD}【预计耗时】${C_RESET}首次约 20-40 分钟（含 MinIO/Milvus），主要花在步骤 10 构建
${C_BOLD}【日志文件】${C_RESET}$LOG_FILE

${C_YELLOW}注意：脚本会生成 docker/.deploy-infra.yml 作为基础服务部署副本（含真实口令，已 gitignore），
      仓库内的模板文件不会被改动${C_RESET}
${milvus_note}

EOF
}

show_plan | sed 's/\x1b\[[0-9;]*m//g' >> "$LOG_FILE"
show_plan

if [ "$DRY_RUN" -eq 1 ]; then
    log ""
    log "${C_YELLOW}（--dry-run：仅展示以上计划，未执行任何操作）${C_RESET}"
    exit 0
fi

if [ "$ASSUME_YES" -eq 0 ]; then
    read -r -p "${C_BOLD}按回车开始部署（Ctrl+C 取消）▸ ${C_RESET}" _
    log ""
    log "${C_GREEN}已确认，开始执行...${C_RESET}"
else
    log "${C_YELLOW}已指定 --yes，跳过确认，开始执行...${C_RESET}"
fi

log_raw "===== 部署开始 $(date '+%F %T') ====="

# ============================================================
# 步骤 1：环境检查
# ============================================================
step_start "环境检查"

[ -f "$COMPOSE_FILE" ] || die "找不到 $COMPOSE_FILE（请在完整项目目录中运行）"
[ -f "$INFRA_SRC" ]    || die "找不到 $INFRA_SRC"
[ -f "$ENV_EXAMPLE" ]  || die "找不到 $ENV_EXAMPLE"
if [ "$use_milvus" -eq 1 ]; then
    [ -f "$MILVUS_FILE" ] || die "找不到 Milvus 编排: $MILVUS_FILE"
fi
if [ "$use_mineru" -eq 1 ]; then
    [ -f "$MINERU_FILE" ] || die "找不到 MinerU 编排: $MINERU_FILE"
fi

command -v docker  >/dev/null 2>&1 || die "未安装 docker，请先安装（curl -fsSL https://get.docker.com | sudo sh）"
command -v openssl >/dev/null 2>&1 || die "缺少 openssl（用于生成口令）：sudo apt install -y openssl"
docker compose version >/dev/null 2>&1 || die "缺少 docker compose v2（命令是 'docker compose'，不是 'docker-compose'）"
docker info >/dev/null 2>&1 || die "无法访问 Docker 守护进程（当前用户可能不在 docker 组：sudo usermod -aG docker \$USER 后重登）"
info "Docker: $(docker --version)"
info "Compose: $(docker compose version --short 2>/dev/null || echo unknown)"

# 已部署检测
if [ -f "$ENV_FILE" ]; then
    warn "检测到已有部署配置：$ENV_FILE"
    die "看起来已部署过。本脚本只做全新部署；如需重装请先停止并清理（参考 deploy/README）"
fi
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "rag-backend"; then
    die "检测到已有容器 rag-backend，本脚本只做全新部署"
fi

check_ports() {
    local p
    for p in "$@"; do
        port_free "$p" || die "端口 $p 已被占用（改脚本顶部对应端口后重试）"
    done
}
check_ports "$HTTP_PORT" "$BACKEND_PORT"
[ "$INSTALL_MYSQL" = "yes" ] && check_ports "$MYSQL_PORT" || true
[ "$use_minio" -eq 1 ] && check_ports "$MINIO_PORT" "$MINIO_CONSOLE_PORT" || true
[ "$use_milvus" -eq 1 ] && check_ports "$MILVUS_PORT" "$MILVUS_METRICS_PORT" || true
[ "$INSTALL_MINERU" = "yes" ] && check_ports "$MINERU_PORT" || true
info "端口检查通过"

AVAIL_KB=$(df -Pk "$PROJECT_DIR" | awk 'NR==2{print $4}')
if [ "${AVAIL_KB:-0}" -lt 10485760 ]; then
    die "磁盘可用空间不足 10GB（当前 $((AVAIL_KB/1024))MB）"
fi
info "磁盘可用: $((AVAIL_KB/1024/1024))GB"

step_done

# ============================================================
# 步骤 2：生成部署配置
# ============================================================
step_start "生成部署配置"

[ -n "$MYSQL_ROOT_PASSWORD" ] || MYSQL_ROOT_PASSWORD="$(gen_secret 16)"
[ -n "$MYSQL_PASSWORD" ]      || MYSQL_PASSWORD="$(gen_secret 16)"
[ -n "$JWT_SECRET" ]          || JWT_SECRET="$(gen_secret 32)"
if [ "$use_minio" -eq 1 ]; then
    [ -n "$MINIO_ACCESS_KEY" ] || MINIO_ACCESS_KEY="myrag$(gen_secret 4)"
    [ -n "$MINIO_SECRET_KEY" ] || MINIO_SECRET_KEY="$(gen_secret 16)"
fi
info "口令已就绪（生成的随机值会在最终报告中展示）"

# 2.1 生成基础服务部署副本并写入真实口令与库名
#     为什么要副本：真实口令不能写进仓库跟踪的文件，否则误提交即泄露；
#     .deploy-infra.yml 已加入 .gitignore，仅用于本机部署。
cp -f "$INFRA_SRC" "$INFRA_RUN"
R_PWD=$(sed_escape "$MYSQL_ROOT_PASSWORD")
U_PWD=$(sed_escape "$MYSQL_PASSWORD")
sed -i "s|MYSQL_ROOT_PASSWORD: <your-password>|MYSQL_ROOT_PASSWORD: ${R_PWD}|" "$INFRA_RUN"
sed -i "s|MYSQL_PASSWORD: <your-password>|MYSQL_PASSWORD: ${U_PWD}|" "$INFRA_RUN"
sed -i "s|-p<your-password>|-p${R_PWD}|" "$INFRA_RUN"
sed -i "s|MYSQL_DATABASE: my_rag|MYSQL_DATABASE: $(sed_escape "$MYSQL_DATABASE")|" "$INFRA_RUN"
sed -i "s|MYSQL_USER: app_rag|MYSQL_USER: $(sed_escape "$MYSQL_USER")|" "$INFRA_RUN"
sed -i "s|\"5455:3306\"|\"${MYSQL_PORT}:3306\"|" "$INFRA_RUN"
if [ "$use_minio" -eq 1 ]; then
    sed -i "s|MINIO_ROOT_USER: <your-access-key>|MINIO_ROOT_USER: $(sed_escape "$MINIO_ACCESS_KEY")|" "$INFRA_RUN"
    sed -i "s|MINIO_ROOT_PASSWORD: <your-secret-key>|MINIO_ROOT_PASSWORD: $(sed_escape "$MINIO_SECRET_KEY")|" "$INFRA_RUN"
    sed -i "s|\"9000:9000\"|\"${MINIO_PORT}:9000\"|" "$INFRA_RUN"
    sed -i "s|\"9001:9001\"|\"${MINIO_CONSOLE_PORT}:9001\"|" "$INFRA_RUN"
fi

# 2.1c MinerU 部署副本（容器名加 my-rag- 前缀，避免与机器上其他 MinerU 冲突）
if [ "$use_mineru" -eq 1 ]; then
    cp -f "$MINERU_FILE" "$MINERU_RUN"
    sed -i "s|container_name: mineru-|container_name: my-rag-mineru-|g" "$MINERU_RUN"
    info "已生成 MinerU 部署副本 $MINERU_RUN"
fi

# 2.1b Milvus 部署副本（端口可配置；不改仓库原文件）
if [ "$use_milvus" -eq 1 ]; then
    cp -f "$MILVUS_FILE" "$MILVUS_RUN"
    sed -i "s|\"19530:19530\"|\"${MILVUS_PORT}:19530\"|" "$MILVUS_RUN"
    sed -i "s|\"9091:9091\"|\"${MILVUS_METRICS_PORT}:9091\"|" "$MILVUS_RUN"
    # 容器名/网络名加 my-rag- 前缀：宿主机上可能已有其他项目的 Milvus
    # （同名会报 "container name already in use"），隔离后可并存。
    sed -i "s|container_name: milvus-etcd|container_name: my-rag-milvus-etcd|"     "$MILVUS_RUN"
    sed -i "s|container_name: milvus-minio|container_name: my-rag-milvus-minio|"   "$MILVUS_RUN"
    sed -i "s|container_name: milvus-standalone|container_name: my-rag-milvus-standalone|" "$MILVUS_RUN"
    sed -i "s|^    name: milvus$|    name: my-rag-milvus|" "$MILVUS_RUN"
    info "已生成 Milvus 部署副本 $MILVUS_RUN"
fi
chmod 600 "$INFRA_RUN"
info "已生成部署副本 $INFRA_RUN（权限 600，含真实口令）"

# 2.2 生成 .env.docker
if [ "$INSTALL_MYSQL" = "yes" ]; then
    MYSQL_HOST_VAL="host.docker.internal"   # 容器内访问宿主机上的 MySQL 容器
    MYSQL_PORT_VAL="$MYSQL_PORT"
else
    [ -n "$EXTERNAL_MYSQL_HOST" ] || die "INSTALL_MYSQL=no 时必须设置 EXTERNAL_MYSQL_HOST（已有 MySQL 地址）"
    [ -n "$EXTERNAL_MYSQL_PORT" ] || die "INSTALL_MYSQL=no 时必须设置 EXTERNAL_MYSQL_PORT（已有 MySQL 端口）"
    MYSQL_HOST_VAL="$EXTERNAL_MYSQL_HOST"
    MYSQL_PORT_VAL="$EXTERNAL_MYSQL_PORT"
fi
cp -f "$ENV_EXAMPLE" "$ENV_FILE"
sed -i "s|^MYSQL_HOST=.*|MYSQL_HOST=${MYSQL_HOST_VAL}|"          "$ENV_FILE"
sed -i "s|^MYSQL_PORT=.*|MYSQL_PORT=${MYSQL_PORT_VAL}|"          "$ENV_FILE"
sed -i "s|^MYSQL_USER=.*|MYSQL_USER=$(sed_escape "$MYSQL_USER")|" "$ENV_FILE"
sed -i "s|^MYSQL_PASSWORD=.*|MYSQL_PASSWORD=$(sed_escape "$MYSQL_PASSWORD")|" "$ENV_FILE"
sed -i "s|^MYSQL_DATABASE=.*|MYSQL_DATABASE=$(sed_escape "$MYSQL_DATABASE")|" "$ENV_FILE"
sed -i "s|^JWT_SECRET=.*|JWT_SECRET=${JWT_SECRET}|"              "$ENV_FILE"
sed -i "s|^STORAGE_BACKEND=.*|STORAGE_BACKEND=${STORAGE_BACKEND}|" "$ENV_FILE"
sed -i "s|^VECTOR_BACKEND=.*|VECTOR_BACKEND=${VECTOR_BACKEND}|"  "$ENV_FILE"
sed -i "s|^MILVUS_URI=.*|MILVUS_URI=${MILVUS_URI}|"              "$ENV_FILE"
sed -i "s|^MY_RAG_HTTP_PORT=.*|MY_RAG_HTTP_PORT=${HTTP_PORT}|"   "$ENV_FILE"
sed -i "s|^MY_RAG_BACKEND_PORT=.*|MY_RAG_BACKEND_PORT=${BACKEND_PORT}|" "$ENV_FILE"
if [ "$use_minio" -eq 1 ]; then
    MINIO_ENDPOINT_VAL="${EXTERNAL_MINIO_ENDPOINT:-host.docker.internal:${MINIO_PORT}}"
    sed -i "s|^MINIO_ENDPOINT=.*|MINIO_ENDPOINT=${MINIO_ENDPOINT_VAL}|" "$ENV_FILE"
    sed -i "s|^MINIO_ACCESS_KEY=.*|MINIO_ACCESS_KEY=$(sed_escape "$MINIO_ACCESS_KEY")|" "$ENV_FILE"
    sed -i "s|^MINIO_SECRET_KEY=.*|MINIO_SECRET_KEY=$(sed_escape "$MINIO_SECRET_KEY")|" "$ENV_FILE"
    sed -i "s|^MINIO_BUCKET=.*|MINIO_BUCKET=$(sed_escape "$MINIO_BUCKET")|" "$ENV_FILE"
fi
if [ "$use_mineru" -eq 1 ]; then
    sed -i "s|^MINERU_API_URL=.*|MINERU_API_URL=http://host.docker.internal:${MINERU_PORT}|" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
info "已生成 $ENV_FILE（权限 600）"

step_done

# ============================================================
# 步骤 3：拉取基础镜像
# ============================================================
step_start "拉取基础镜像"

# 先探测 Docker Hub 直连能力（200/401 都算可达）
code=$(timeout 8 curl -s -o /dev/null -w '%{http_code}' https://registry-1.docker.io/v2/ 2>/dev/null || true)
if [ "$code" = "200" ] || [ "$code" = "401" ]; then
    REGISTRY_DIRECT=1
    info "Docker Hub 直连可用"
else
    REGISTRY_DIRECT=0
    warn "Docker Hub 直连不可用，使用镜像代理: $DOCKER_MIRROR"
fi

pull_image() {
    local img="$1"
    if docker image inspect "$img" >/dev/null 2>&1; then
        info "已存在: $img"
        return 0
    fi
    if [ "$REGISTRY_DIRECT" -eq 1 ]; then
        info "拉取: $img"
        if docker pull "$img" >>"$LOG_FILE" 2>&1; then
            info "  完成: $img"
            return 0
        fi
        warn "直连拉取失败，改用代理: $img"
    fi
    # 官方镜像在 proxy 下需要 library/ 前缀；带斜杠的（如 minio/minio）直接用原名
    local repo="library/${img}"
    case "$img" in */*) repo="$img" ;; esac
    info "经代理拉取: ${DOCKER_MIRROR}/${repo}"
    docker pull "${DOCKER_MIRROR}/${repo}" >>"$LOG_FILE" 2>&1 \
        || die "镜像拉取失败: $img（换一个代理后重试）"
    docker tag "${DOCKER_MIRROR}/${repo}" "$img"
    info "  完成并重命名: $img"
}

# 某些代理会对特定仓库/tag 返回 403（如 minio/minio:latest）；
# 若本地已有同仓库的任一 tag，直接复用它并打上默认 tag，避免无谓失败。
reuse_local_image() {
    local name="$1"
    if docker image inspect "$name" >/dev/null 2>&1; then
        info "已存在: $name"
        return 0
    fi
    local existing
    existing=$(docker images --format '{{.Repository}}:{{.Tag}}' \
        | grep -E "^${name}:" | grep -v "<none>" | head -1 || true)
    if [ -n "$existing" ]; then
        info "复用本地镜像: $existing → ${name}"
        docker tag "$existing" "$name"
        return 0
    fi
    return 1
}

pull_image python:3.12-slim
pull_image node:22-alpine
pull_image nginx:alpine
# 基础服务镜像：必须在这里一并准备好，否则后面 compose up 会因直接访问
# Docker Hub 拉取而长时间卡住（本步骤走代理逻辑）
[ "$INSTALL_MYSQL" = "yes" ] && pull_image mysql:8 || true
if [ "$use_minio" -eq 1 ]; then
    if ! reuse_local_image minio/minio; then
        # 注意：部分代理对 minio/minio:latest 返回 403；拉不到时在步骤 6 启动处会明确报错
        if ! docker pull minio/minio >>"$LOG_FILE" 2>&1; then
            warn "minio/minio 拉取失败（代理可能禁止该镜像），将在启动 MinIO 时再次尝试"
        fi
    fi
fi

step_done

# ============================================================
# 步骤 4：启动 MySQL
# ============================================================
step_start "启动 MySQL 容器"

if [ "$INSTALL_MYSQL" = "yes" ]; then
    docker compose -p "$PROJECT_INFRA" -f "$INFRA_RUN" up -d mysql >>"$LOG_FILE" 2>&1 \
        || die "MySQL 容器启动失败（日志：$LOG_FILE）"
    info "MySQL 容器已启动，等待健康检查..."

    MYSQL_HEALTHY=0
    for _ in $(seq 1 90); do
        status=$(docker inspect --format '{{.State.Health.Status}}' my-rag-mysql 2>/dev/null || echo unknown)
        if [ "$status" = "healthy" ]; then MYSQL_HEALTHY=1; break; fi
        sleep 2
    done
    [ "$MYSQL_HEALTHY" -eq 1 ] || die "MySQL 90 秒内未就绪（docker logs my-rag-mysql 查看原因）"
    info "MySQL 健康检查通过"
else
    info "INSTALL_MYSQL=no，跳过安装，使用已有 MySQL：${EXTERNAL_MYSQL_HOST}:${EXTERNAL_MYSQL_PORT}"
fi

step_done

# ============================================================
# 步骤 5：校验数据库与账号
# ============================================================
step_start "校验数据库与账号"

if [ "$INSTALL_MYSQL" = "yes" ]; then
    docker exec -e MYSQL_PWD="$MYSQL_PASSWORD" my-rag-mysql \
        mysql -u"$MYSQL_USER" -e "SELECT 1" "$MYSQL_DATABASE" >/dev/null 2>&1 \
        || die "无法用 ${MYSQL_USER} 连接库 ${MYSQL_DATABASE}（检查 infra 口令是否与 .env.docker 一致）"
    info "账号 ${MYSQL_USER} → 库 ${MYSQL_DATABASE} 连通正常"
else
    if command -v mysql >/dev/null 2>&1; then
        MYSQL_PWD="$MYSQL_PASSWORD" mysql -h "$EXTERNAL_MYSQL_HOST" -P "$EXTERNAL_MYSQL_PORT" \
            -u"$MYSQL_USER" -e "SELECT 1" "$MYSQL_DATABASE" >/dev/null 2>&1 \
            || warn "外部 MySQL（${EXTERNAL_MYSQL_HOST}:${EXTERNAL_MYSQL_PORT}）连通性检查失败（后端启动时会重试）"
    else
        warn "未安装 mysql 客户端，跳过外部数据库连通性检查"
    fi
fi

step_done

# ============================================================
# 步骤 6：启动 MinIO（按需）
# ============================================================
step_start "启动 MinIO 对象存储"

if [ "$use_minio" -eq 1 ]; then
    docker image inspect minio/minio >/dev/null 2>&1 \
        || die "缺少 minio/minio 镜像（代理可能禁止该镜像）。请在有网机器拉取后导入，或改用 STORAGE_BACKEND=local"
    docker compose -p "$PROJECT_INFRA" -f "$INFRA_RUN" up -d minio >>"$LOG_FILE" 2>&1 \
        || die "MinIO 容器启动失败（日志：$LOG_FILE）"
    info "MinIO 容器已启动，等待健康检查..."

    MINIO_OK=0
    for _ in $(seq 1 60); do
        if curl -sf --max-time 3 "http://127.0.0.1:${MINIO_PORT}/minio/health/live" >/dev/null 2>&1; then
            MINIO_OK=1; break
        fi
        sleep 2
    done
    [ "$MINIO_OK" -eq 1 ] || die "MinIO 120 秒内未就绪（docker logs my-rag-minio 查看）"
    info "MinIO 健康检查通过（API ${MINIO_PORT} / 控制台 ${MINIO_CONSOLE_PORT}）"
else
    info "STORAGE_BACKEND=${STORAGE_BACKEND}，无需 MinIO，跳过"
fi

step_done

# ============================================================
# 步骤 7：启动 Milvus（按需）
# ============================================================
step_start "启动 Milvus 向量库"

if [ "$use_milvus" -eq 1 ]; then
    info "启动 etcd + minio + milvus（首次需拉取镜像，可能较慢）..."
    docker compose -p "$PROJECT_MILVUS" -f "$MILVUS_RUN" up -d >>"$LOG_FILE" 2>&1 \
        || die "Milvus 编排启动失败（常见原因：镜像拉取失败，日志：$LOG_FILE）"

    MILVUS_OK=0
    for _ in $(seq 1 120); do
        status=$(docker inspect --format '{{.State.Health.Status}}' my-rag-milvus-standalone 2>/dev/null || echo unknown)
        if [ "$status" = "healthy" ]; then MILVUS_OK=1; break; fi
        sleep 3
    done
    [ "$MILVUS_OK" -eq 1 ] || die "Milvus 360 秒内未就绪（docker logs my-rag-milvus-standalone 查看）"
    info "Milvus 健康检查通过（端口 19530）"
else
    info "VECTOR_BACKEND=${VECTOR_BACKEND}，无需 Milvus，跳过"
fi

step_done

# ============================================================
# 步骤 8：启动 MinerU（按需）
# ============================================================
step_start "启动 MinerU 文档解析"

if [ "$use_mineru" -eq 1 ]; then
    docker image inspect mineru:latest >/dev/null 2>&1 \
        || die "缺少 mineru:latest 镜像（约 12GB，需预构建，见 docker/MinerU/README_zh-CN.md）"
    info "启动 MinerU（profile: ${MINERU_PROFILE}，首次加载模型较慢）..."
    docker compose -p my-rag-mineru -f "$MINERU_RUN" --profile "$MINERU_PROFILE" up -d >>"$LOG_FILE" 2>&1 \
        || die "MinerU 启动失败（日志：$LOG_FILE）"

    MINERU_OK=0
    for _ in $(seq 1 100); do
        if curl -sf --max-time 3 "http://127.0.0.1:${MINERU_PORT}/health" >/dev/null 2>&1; then
            MINERU_OK=1; break
        fi
        sleep 3
    done
    [ "$MINERU_OK" -eq 1 ] || die "MinerU 300 秒内未就绪（docker logs my-rag-mineru-${MINERU_PROFILE} 查看）"
    info "MinerU 健康检查通过（端口 ${MINERU_PORT}）"
else
    info "INSTALL_MINERU=no，跳过（文档解析需另行部署 MinerU，并在系统配置里填地址）"
fi

step_done

# ============================================================
# 步骤 9：准备数据目录
# ============================================================
step_start "准备数据目录"

mkdir -p "$DATA_DIR"
if [ "$(id -u)" -eq 1000 ]; then
    info "当前用户 uid=1000，与容器内 rag 用户一致，无需 chown"
else
    info "将 data 目录属主改为 1000:1000（需要 sudo）"
    sudo chown -R 1000:1000 "$DATA_DIR" || die "chown 失败，请手动执行: sudo chown -R 1000:1000 $DATA_DIR"
fi
info "数据目录: $DATA_DIR"

step_done

# ============================================================
# 步骤 10：构建镜像
# ============================================================
step_start "构建镜像（耗时最长，请耐心等待）"

BUILD_LOG="$LOG_DIR/build-$(date +%Y%m%d-%H%M%S).log"
info "构建日志: $BUILD_LOG"

docker compose -p "$PROJECT_APP" -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build \
    --build-arg PIP_INDEX_URL="$PIP_INDEX_URL" \
    --build-arg NPM_REGISTRY="$NPM_REGISTRY" >"$BUILD_LOG" 2>&1 &
BUILD_PID=$!

while kill -0 "$BUILD_PID" 2>/dev/null; do
    sleep 30
    if kill -0 "$BUILD_PID" 2>/dev/null; then
        info "...仍在构建，已耗时 $(fmt_duration $((SECONDS - STEP_START)))"
    fi
done
if ! wait "$BUILD_PID"; then
    fail "镜像构建失败，最近日志："
    tail -40 "$BUILD_LOG" | tee -a "$LOG_FILE"
    die "构建失败（完整日志：$BUILD_LOG）"
fi
info "镜像构建完成"

step_done

# ============================================================
# 步骤 11：启动容器
# ============================================================
step_start "启动容器"

docker compose -p "$PROJECT_APP" -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d >>"$LOG_FILE" 2>&1 \
    || die "容器启动失败（docker compose ps 查看）"
info "容器已创建，等待后端 healthy..."

BACKEND_OK=0
for _ in $(seq 1 60); do
    status=$(docker inspect --format '{{.State.Health.Status}}' rag-backend 2>/dev/null || echo unknown)
    if [ "$status" = "healthy" ]; then BACKEND_OK=1; break; fi
    sleep 2
done
[ "$BACKEND_OK" -eq 1 ] || die "后端 120 秒内未就绪（docker logs rag-backend 查看）"
info "后端健康检查通过"

step_done

# ============================================================
# 步骤 12：健康验证
# ============================================================
step_start "健康验证"

check_url() {
    local url="$1" expect="$2" name="$3"
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url" || true)
    if [ "$code" = "$expect" ]; then
        info "✓ $name -> HTTP $code"
    else
        die "$name 校验失败（期望 $expect，实际 ${code:-无响应}）: $url"
    fi
}

check_url "http://127.0.0.1:${BACKEND_PORT}/api/health" "200" "后端 /api/health"
check_url "http://127.0.0.1:${HTTP_PORT}/" "200" "前端页面"
check_url "http://127.0.0.1:${HTTP_PORT}/api/health" "200" "前端反代 /api"

if [ "$use_minio" -eq 1 ]; then
    if curl -sf --max-time 5 "http://127.0.0.1:${MINIO_PORT}/minio/health/live" >/dev/null; then
        info "✓ MinIO 健康检查"
    else
        die "MinIO 健康检查失败"
    fi
fi
if [ "$use_milvus" -eq 1 ]; then
    status=$(docker inspect --format '{{.State.Health.Status}}' my-rag-milvus-standalone 2>/dev/null || echo unknown)
    if [ "$status" = "healthy" ]; then
        info "✓ Milvus 健康检查（healthy）"
    else
        die "Milvus 状态异常: $status"
    fi
fi
if [ "$use_mineru" -eq 1 ]; then
    if curl -sf --max-time 10 "http://127.0.0.1:${MINERU_PORT}/health" >/dev/null; then
        info "✓ MinerU 健康检查（${MINERU_PORT}）"
    else
        die "MinerU 健康检查失败"
    fi
fi

step_done

# ============================================================
# 步骤 13：部署报告
# ============================================================
step_start "输出部署报告"

TOTAL_COST=$((SECONDS - START_TIME))

log ""
log "${C_BOLD}══════════════════════════════════════════════════════════${C_RESET}"
log "${C_GREEN}${C_BOLD}  ✓ 部署完成（总耗时 $(fmt_duration "$TOTAL_COST")）${C_RESET}"
log "${C_BOLD}══════════════════════════════════════════════════════════${C_RESET}"
log ""
log "${C_BOLD}【访问地址】${C_RESET}"
log "  前端界面   http://${IP}:${HTTP_PORT}"
log "  后端 API   http://${IP}:${BACKEND_PORT}/api/health"
log ""
log "${C_BOLD}【登录账号】${C_RESET}"
log "  用户名     admin"
log "  密  码     admin123"
log "  ${C_RED}${C_BOLD}⚠ 请登录后立即修改密码${C_RESET}"
log ""
log "${C_BOLD}【MySQL】${C_RESET}"
if [ "$INSTALL_MYSQL" = "yes" ]; then
    log "  地址       ${IP}:${MYSQL_PORT}（宿主机端口 → 容器 my-rag-mysql:3306）"
else
    log "  地址       ${EXTERNAL_MYSQL_HOST}:${EXTERNAL_MYSQL_PORT}（已有实例）"
fi
log "  库名       ${MYSQL_DATABASE}"
log "  账号       ${MYSQL_USER}"
log "  口令       ${MYSQL_PASSWORD}"
log "  root 口令  ${MYSQL_ROOT_PASSWORD}"
log ""
if [ "$use_minio" -eq 1 ]; then
    log "${C_BOLD}【MinIO】${C_RESET}"
    log "  API        http://${IP}:${MINIO_PORT}"
    log "  控制台     http://${IP}:${MINIO_CONSOLE_PORT}"
    log "  账号       ${MINIO_ACCESS_KEY}"
    log "  密码       ${MINIO_SECRET_KEY}"
    log "  桶         ${MINIO_BUCKET}（不存在时系统自动创建）"
    log ""
fi
log "${C_BOLD}【向量存储】${C_RESET}"
if [ "$use_milvus" -eq 1 ]; then
    log "  Milvus     http://${IP}:19530（容器 my-rag-milvus-standalone）"
    log "  管理端口   9091"
else
    log "  Chroma     嵌入式，数据在 $DATA_DIR/chroma"
fi
log ""
if [ "$use_mineru" -eq 1 ]; then
    log "${C_BOLD}【MinerU】${C_RESET}"
    log "  地址       http://${IP}:${MINERU_PORT}（容器 my-rag-mineru-${MINERU_PROFILE}）"
    log ""
fi
log "${C_BOLD}【数据与日志】${C_RESET}"
log "  数据目录   $DATA_DIR"
log "  部署日志   $LOG_FILE"
log "  配置文件   $ENV_FILE（权限 600）"
log ""
log "${C_BOLD}【下一步】${C_RESET}"
log "  1. 浏览器打开 http://${IP}:${HTTP_PORT}，用 admin/admin123 登录并改密码"
log "  2. 进入「系统配置」填写 LLM / Embedding / MinerU / Rerank 地址"
log "     （容器内访问宿主机服务用 host.docker.internal 或局域网 IP，不能用 127.0.0.1）"
log "  3. 建知识库 → 上传文档 → 解析 → 提问，验证完整链路"
log ""
log "${C_BOLD}【常用命令】${C_RESET}"
log "  cd $PROJECT_DIR"
log "  docker compose -p my-rag-app -f docker/docker-compose.yml ps"
log "  docker compose -p my-rag-app -f docker/docker-compose.yml logs -f rag-backend"
log "  docker compose -p my-rag-app -f docker/docker-compose.yml down   # 停止应用（数据保留）"
log "  docker compose -p my-rag-infra  -f docker/.deploy-infra.yml down # 停止 MySQL/MinIO"
if [ "$use_milvus" -eq 1 ]; then
    log "  docker compose -p my-rag-milvus -f docker/.deploy-milvus.yml down # 停止向量库"
fi
log ""
log "${C_YELLOW}提示：基础服务由 docker/.deploy-infra.yml 管理（含真实口令，已 gitignore）；仓库模板未改动${C_RESET}"

log_raw "===== 部署成功 $(date '+%F %T')，总耗时 $(fmt_duration "$TOTAL_COST") ====="

step_done

exit 0
