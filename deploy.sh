#!/bin/bash
# =============================================================================
# 教务管理系统 一键部署脚本
# 支持：Ubuntu 20.04/22.04、Debian 11/12、CentOS 7/8、Rocky Linux 8/9
# 用法：bash deploy.sh [--port-prefix N] [--skip-build] [--reset]
#   --port-prefix N  端口前缀偏移（默认0，即使用标准端口）
#   --skip-build     跳过镜像构建（已有镜像时使用）
#   --reset          清除所有数据重新部署
# =============================================================================
set -euo pipefail

# ── 颜色输出 ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "\n${BOLD}${BLUE}══ $* ══${NC}"; }

# ── 参数解析 ──────────────────────────────────────────────────────────────────
SKIP_BUILD=false
RESET=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for arg in "$@"; do
  case $arg in
    --skip-build) SKIP_BUILD=true ;;
    --reset)      RESET=true ;;
  esac
done

# ── 端口配置（如有冲突可在此修改） ───────────────────────────────────────────
PORT_POSTGRES_PRIMARY=5432
PORT_POSTGRES_REPLICA=5433
PORT_REDIS=6380           # 避开系统默认 6379
PORT_BACKEND=8000
PORT_FRONTEND=3000

COMPOSE_CMD=""
DOCKER_API_VERSION="1.44"

# =============================================================================
# 1. 检查并安装 Docker
# =============================================================================
log_step "检查 Docker 环境"

install_docker() {
    log_info "安装 Docker..."
    if command -v apt-get &>/dev/null; then
        apt-get update -qq
        apt-get install -y -qq ca-certificates curl gnupg lsb-release
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
            https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
            > /etc/apt/sources.list.d/docker.list
        apt-get update -qq
        apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    elif command -v yum &>/dev/null; then
        yum install -y -q yum-utils
        yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
        yum install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
        systemctl enable --now docker
    else
        log_error "不支持的操作系统，请手动安装 Docker"
        exit 1
    fi
}

if ! command -v docker &>/dev/null; then
    if [[ $EUID -ne 0 ]]; then
        log_error "需要 root 权限安装 Docker，请用 sudo bash deploy.sh"
        exit 1
    fi
    install_docker
fi

docker info &>/dev/null || { log_error "Docker 守护进程未运行，执行: systemctl start docker"; exit 1; }
log_info "Docker: $(docker --version)"

# ── 确定 docker-compose 命令 ──────────────────────────────────────────────────
if docker compose version &>/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose &>/dev/null; then
    # v1/v2 的旧二进制，需要设置 API 版本
    export DOCKER_API_VERSION="$DOCKER_API_VERSION"
    COMPOSE_CMD="docker-compose"
else
    log_warn "未找到 docker-compose，尝试安装..."
    if [[ $EUID -eq 0 ]]; then
        curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" \
            -o /usr/local/bin/docker-compose
        chmod +x /usr/local/bin/docker-compose
        COMPOSE_CMD="docker-compose"
        export DOCKER_API_VERSION="$DOCKER_API_VERSION"
    else
        log_error "请手动安装 docker-compose 或使用 Docker 23+ (内置 docker compose)"
        exit 1
    fi
fi
log_info "Compose 命令: $COMPOSE_CMD"

# ── 确保当前用户在 docker 组 ───────────────────────────────────────────────────
if ! groups | grep -q docker; then
    log_warn "当前用户不在 docker 组，尝试以 sudo 运行 docker 命令"
    DOCKER_SUDO="sudo"
else
    DOCKER_SUDO=""
fi

DC="$DOCKER_SUDO $COMPOSE_CMD"

# =============================================================================
# 2. 检查必要端口
# =============================================================================
log_step "检查端口占用"

check_port() {
    local port=$1 name=$2
    if ss -tlnp 2>/dev/null | grep -q ":${port} " || \
       netstat -tlnp 2>/dev/null | grep -q ":${port} "; then
        # 检查是否是已有的 docker 容器占用的同名端口
        if docker ps --format "{{.Ports}}" 2>/dev/null | grep -q ":${port}->"; then
            log_warn "端口 ${port} (${name}) 已被 Docker 容器占用，将尝试复用"
        else
            log_warn "端口 ${port} (${name}) 已被占用，部署可能失败。可修改脚本顶部端口配置"
        fi
    else
        log_info "端口 ${port} (${name}) 可用"
    fi
}

check_port $PORT_POSTGRES_PRIMARY "PostgreSQL Primary"
check_port $PORT_REDIS            "Redis"
check_port $PORT_BACKEND          "Backend API"
check_port $PORT_FRONTEND         "Frontend"

# =============================================================================
# 3. 进入项目目录
# =============================================================================
log_step "准备项目目录"

cd "$SCRIPT_DIR"
log_info "工作目录: $SCRIPT_DIR"

# 可选：重置数据
if [[ "$RESET" == "true" ]]; then
    log_warn "--reset 模式：清除所有容器和数据卷..."
    $DC down -v 2>/dev/null || true
    log_info "数据清除完成"
fi

# =============================================================================
# 4. 生成 .env（如不存在）
# =============================================================================
if [[ ! -f .env ]]; then
    log_info "生成 .env 配置文件..."
    cat > .env << EOF
# 自动生成，可按需修改
POSTGRES_PASSWORD=postgres123
REDIS_URL=redis://redis:6379/0
SECRET_KEY=$(openssl rand -hex 32 2>/dev/null || echo "edu-manage-secret-$(date +%s)")
ACCESS_TOKEN_EXPIRE_MINUTES=1440
EOF
fi

# =============================================================================
# 5. 启动基础服务
# =============================================================================
log_step "启动基础服务 (PostgreSQL + Redis)"

$DC up -d postgres-primary redis

log_info "等待 PostgreSQL 健康..."
TIMEOUT=120
ELAPSED=0
until $DC ps postgres-primary 2>/dev/null | grep -q "healthy"; do
    if [[ $ELAPSED -ge $TIMEOUT ]]; then
        log_error "PostgreSQL 启动超时，请检查日志: $DC logs postgres-primary"
        exit 1
    fi
    printf "."
    sleep 3
    ELAPSED=$((ELAPSED + 3))
done
echo ""
log_info "PostgreSQL 已就绪"

# =============================================================================
# 6. 应用数据库初始化脚本（幂等）
# =============================================================================
log_step "初始化数据库 Schema 和数据"

run_sql() {
    local file=$1
    if [[ -f "$file" ]]; then
        log_info "执行: $(basename $file)"
        PGPASSWORD=postgres123 psql -h localhost -p $PORT_POSTGRES_PRIMARY \
            -U postgres -d edumanage -f "$file" \
            --on-error-stop \
            -v ON_ERROR_STOP=0 \
            2>&1 | grep -v "^$" | grep -E "ERROR|NOTICE|WARNING|FATAL" || true
    else
        log_warn "跳过不存在的文件: $file"
    fi
}

# 检查数据库是否已初始化
TABLE_COUNT=$(PGPASSWORD=postgres123 psql -h localhost -p $PORT_POSTGRES_PRIMARY \
    -U postgres -d edumanage -tAc \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';" \
    2>/dev/null || echo "0")

if [[ "$TABLE_COUNT" -lt 10 ]]; then
    log_info "数据库为空，执行完整初始化..."
    run_sql "docker/postgres/init/01_extensions.sql"
    run_sql "docker/postgres/init/02_schema.sql"
    run_sql "docker/postgres/init/03_data.sql"
    run_sql "docker/postgres/init/04_timeseries.sql"
    run_sql "docker/postgres/init/05_publication.sql"
else
    log_info "数据库已有 ${TABLE_COUNT} 张表，执行补充初始化（幂等）..."
    run_sql "docker/postgres/init/01_extensions.sql"
    run_sql "docker/postgres/init/04_timeseries.sql"
    run_sql "docker/postgres/init/05_publication.sql"
fi

log_info "数据库初始化完成"

# =============================================================================
# 7. 构建并启动后端 + CDC 采集器
# =============================================================================
log_step "构建并启动后端 (FastAPI) 与 CDC 采集器 (PG逻辑复制 → Redis Stream)"

if [[ "$SKIP_BUILD" == "false" ]]; then
    log_info "构建后端镜像（backend 与 cdc-collector 共用）..."
    $DC build backend
fi

$DC up -d backend cdc-collector

log_info "等待后端 API 就绪..."
TIMEOUT=120; ELAPSED=0
until curl -sf http://localhost:$PORT_BACKEND/health &>/dev/null; do
    if [[ $ELAPSED -ge $TIMEOUT ]]; then
        log_error "后端启动超时，查看日志: $DC logs backend"
        exit 1
    fi
    printf "."
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done
echo ""
log_info "后端 API 已就绪"

# =============================================================================
# 8. 构建并启动前端
# =============================================================================
log_step "构建并启动前端 (React + Nginx)"

if [[ "$SKIP_BUILD" == "false" ]]; then
    log_info "构建前端镜像（需要 2-5 分钟）..."
    $DC build frontend
fi

$DC up -d frontend

log_info "等待前端就绪..."
TIMEOUT=60; ELAPSED=0
until curl -sf http://localhost:$PORT_FRONTEND &>/dev/null; do
    if [[ $ELAPSED -ge $TIMEOUT ]]; then
        log_warn "前端未响应，请手动检查: $DC logs frontend"
        break
    fi
    printf "."
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done
echo ""

# =============================================================================
# 9. 部署验证
# =============================================================================
log_step "部署验证"

PASS=0; FAIL=0

check() {
    local name=$1 cmd=$2
    if eval "$cmd" &>/dev/null; then
        log_info "✅ $name"
        PASS=$((PASS+1))
    else
        log_warn "❌ $name"
        FAIL=$((FAIL+1))
    fi
}

check "PostgreSQL 连接"     "PGPASSWORD=postgres123 psql -h localhost -p $PORT_POSTGRES_PRIMARY -U postgres -d edumanage -c 'SELECT 1' -q"
check "Redis 连接"          "redis-cli -p $PORT_REDIS ping 2>/dev/null | grep -q PONG || docker exec redis redis-cli ping 2>/dev/null | grep -q PONG"
check "CDC 采集器运行"      "$DC ps cdc-collector | grep -qiE 'up|running'"
check "CDC 复制槽存在"      "PGPASSWORD=postgres123 psql -h localhost -p $PORT_POSTGRES_PRIMARY -U postgres -d edumanage -tAc \"SELECT COUNT(*) FROM pg_replication_slots WHERE slot_name='redis_cdc_slot'\" | grep -q '^1$'"
check "CDC Publication"     "PGPASSWORD=postgres123 psql -h localhost -p $PORT_POSTGRES_PRIMARY -U postgres -d edumanage -tAc \"SELECT COUNT(*) FROM pg_publication WHERE pubname='cdc_pub'\" | grep -q '^1$'"
check "后端 /health"        "curl -sf http://localhost:$PORT_BACKEND/health"
check "后端登录 API"        "curl -sf -X POST http://localhost:$PORT_BACKEND/api/auth/login -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"admin123\"}' | grep -q access_token"
check "前端首页"            "curl -sf http://localhost:$PORT_FRONTEND | grep -q 'html'"
check "数据库学生表"         "PGPASSWORD=postgres123 psql -h localhost -p $PORT_POSTGRES_PRIMARY -U postgres -d edumanage -tAc 'SELECT COUNT(*) FROM students' | grep -qE '^[0-9]+'"
check "TimescaleDB hypertable" "PGPASSWORD=postgres123 psql -h localhost -p $PORT_POSTGRES_PRIMARY -U postgres -d edumanage -tAc 'SELECT COUNT(*) FROM timescaledb_information.hypertables' | grep -qE '^[2-9]'"

# =============================================================================
# 10. 汇总输出
# =============================================================================
echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║              教务管理系统  部署完成                      ║${NC}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${GREEN}通过: $PASS 项${NC}  ${RED}失败: $FAIL 项${NC}"
echo ""
echo -e "  ${BOLD}访问地址：${NC}"
HOST=$(hostname -I | awk '{print $1}')
echo -e "  🌐 前端应用    http://${HOST}:${PORT_FRONTEND}"
echo -e "  🔌 后端 API    http://${HOST}:${PORT_BACKEND}"
echo -e "  📖 API 文档    http://${HOST}:${PORT_BACKEND}/docs"
echo -e "  📡 CDC 事件流  docker exec redis redis-cli XLEN cdc:events"
echo ""
echo -e "  ${BOLD}登录账号：${NC}"
echo -e "  👤 管理员      admin     / admin123"
echo -e "  👨‍🏫 教师        teacher01 / teacher123"
echo -e "  👨‍🎓 学生        student01 / student123"
echo ""
echo -e "  ${BOLD}常用命令：${NC}"
echo -e "  查看状态   $COMPOSE_CMD ps"
echo -e "  查看日志   $COMPOSE_CMD logs -f backend"
echo -e "  停止服务   $COMPOSE_CMD stop"
echo -e "  完全清除   $COMPOSE_CMD down -v   ${RED}(删除所有数据)${NC}"
echo ""
if [[ $FAIL -gt 0 ]]; then
    echo -e "  ${YELLOW}⚠️  有 $FAIL 项检查未通过，排查建议：${NC}"
    echo -e "  $COMPOSE_CMD logs --tail=50 [服务名]"
fi
