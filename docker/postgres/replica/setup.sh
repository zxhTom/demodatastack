#!/bin/bash
# =============================================================================
# PostgreSQL Replica 初始化脚本
# 使用 pg_basebackup 从 Primary 同步数据，配置流复制
# =============================================================================

set -e

PRIMARY_HOST="${PRIMARY_HOST:-postgres-primary}"
PRIMARY_PORT="${PRIMARY_PORT:-5432}"
REPLICATION_USER="${REPLICATION_USER:-replicator}"
REPLICATION_PASSWORD="${REPLICATION_PASSWORD:-replicator123}"
PGDATA="${PGDATA:-/var/lib/postgresql/data}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-postgres123}"
MAX_WAIT=120

echo "=============================================="
echo "  PostgreSQL Replica 初始化"
echo "  Primary: ${PRIMARY_HOST}:${PRIMARY_PORT}"
echo "  Data dir: ${PGDATA}"
echo "=============================================="

# 等待 Primary 就绪
echo "[1/5] 等待 Primary 数据库就绪..."
for i in $(seq 1 $MAX_WAIT); do
    if PGPASSWORD="${POSTGRES_PASSWORD}" pg_isready \
        -h "${PRIMARY_HOST}" \
        -p "${PRIMARY_PORT}" \
        -U "${POSTGRES_USER}" > /dev/null 2>&1; then
        echo "  Primary 已就绪 (等待了 ${i} 次)"
        break
    fi
    if [ $i -eq $MAX_WAIT ]; then
        echo "  错误: 超时等待 Primary，退出"
        exit 1
    fi
    echo "  等待中... (${i}/${MAX_WAIT})"
    sleep 2
done

# 检查数据目录
echo "[2/5] 检查数据目录..."
if [ -d "${PGDATA}/global" ]; then
    echo "  数据目录已存在，跳过 pg_basebackup"
else
    echo "  数据目录为空，开始 pg_basebackup..."

    # 清理目录（确保干净）
    rm -rf "${PGDATA}"/*

    # 执行基础备份（流式复制）
    export PGPASSWORD="${REPLICATION_PASSWORD}"
    pg_basebackup \
        -h "${PRIMARY_HOST}" \
        -p "${PRIMARY_PORT}" \
        -U "${REPLICATION_USER}" \
        -D "${PGDATA}" \
        -Fp \
        -Xs \
        -P \
        -R \
        --checkpoint=fast \
        --label="replica_basebackup_$(date +%Y%m%d_%H%M%S)"

    echo "  pg_basebackup 完成"
fi

# 配置流复制连接信息
echo "[3/5] 配置流复制..."

# PostgreSQL 12+ 使用 postgresql.auto.conf + standby.signal
# pg_basebackup -R 会自动生成 standby.signal 和写入 primary_conninfo
# 确认 standby.signal 存在
if [ ! -f "${PGDATA}/standby.signal" ]; then
    touch "${PGDATA}/standby.signal"
    echo "  创建 standby.signal"
else
    echo "  standby.signal 已存在"
fi

# 写入/覆盖复制连接配置
cat > "${PGDATA}/postgresql.auto.conf" << EOF
# Replica 流复制配置（由 setup.sh 自动生成）
primary_conninfo = 'host=${PRIMARY_HOST} port=${PRIMARY_PORT} user=${REPLICATION_USER} password=${REPLICATION_PASSWORD} application_name=replica1 sslmode=prefer'
primary_slot_name = ''
recovery_target_timeline = 'latest'
hot_standby = on
hot_standby_feedback = on
max_standby_streaming_delay = 30s
wal_receiver_status_interval = 10s
EOF

echo "  流复制配置写入完成"

# 修复权限
echo "[4/5] 修复数据目录权限..."
chown -R postgres:postgres "${PGDATA}"
chmod 700 "${PGDATA}"

# 启动 PostgreSQL
echo "[5/5] 启动 PostgreSQL Replica..."
exec gosu postgres postgres \
    -c "hot_standby=on" \
    -c "wal_level=replica" \
    -c "max_wal_senders=0" \
    -c "log_connections=on" \
    -c "log_replication_commands=on" \
    -c "shared_preload_libraries=timescaledb" \
    -c "timescaledb.telemetry_level=off"
