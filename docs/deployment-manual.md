# 部署手册（Redis Stream CDC 版）

> 适用于 2026-07 整改后的架构（无 Kafka/Zookeeper/Debezium）。
> 架构说明见 [architecture.md](architecture.md)，原理学习见 [learning-guide.md](learning-guide.md)。

## 1. 环境要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Linux (Ubuntu 20.04+/CentOS 7+) 或 macOS（Docker Desktop） |
| Docker | ≥ 20.10（推荐 24+，自带 `docker compose`） |
| 内存 | ≥ 2 GB（旧 Kafka 版需要 4 GB+） |
| 磁盘 | ≥ 10 GB |
| 端口 | 3000 / 5432 / 5433 / 6380 / 8000 空闲 |

## 2. 一键部署

```bash
cd demodatastack
bash deploy.sh
```

脚本流程：检查 Docker → 检查端口 → 启动 PostgreSQL + Redis → 初始化数据库
（Schema/数据/超表/CDC 发布）→ 构建并启动 backend + cdc-collector → 启动 frontend
→ 10 项自动验证 → 输出访问地址。

常用参数：

```bash
bash deploy.sh --skip-build   # 已有镜像，跳过构建
bash deploy.sh --reset        # 清除所有数据重新部署（危险）
```

部署完成后：

| 入口 | 地址 |
|------|------|
| 前端应用 | http://localhost:3000 |
| 后端 API | http://localhost:8000 |
| API 文档 | http://localhost:8000/docs |
| 管理员账号 | admin / admin123 |

## 3. 手动部署（理解每一步）

```bash
# 1. 启动数据层
docker compose up -d postgres-primary redis
# 等待健康
docker compose ps postgres-primary   # 出现 (healthy)

# 2.（可选）只读副本
docker compose up -d postgres-replica

# 3. 构建并启动后端 + CDC 采集器（共用一个镜像）
docker compose build backend
docker compose up -d backend cdc-collector

# 4. 前端
docker compose build frontend
docker compose up -d frontend
```

> 首次启动时 PG 会自动执行 `docker/postgres/init/*.sql`：
> `01_extensions`(TimescaleDB) → `02_schema` → `03_data` → `04_timeseries`(超表)
> → `05_publication`(CDC 发布 cdc_pub + REPLICA IDENTITY FULL)。
> 对已存在的数据卷，deploy.sh 会幂等补跑 01/04/05。

## 4. 部署验证

```bash
# 后端健康
curl http://localhost:8000/health
# → {"status":"ok","service":"edumanage-api"}

# CDC 采集器在线（复制槽 active 且组件日志正常）
docker logs cdc-collector --tail 5
# → "CDC 监听已启动: slot=redis_cdc_slot publication=cdc_pub → stream=cdc:events"

docker exec postgres-primary psql -U postgres -d edumanage -c \
  "SELECT slot_name, active FROM pg_replication_slots;"
# → redis_cdc_slot | t

# 端到端链路测试：写一条数据，看事件流到 kpi_events
docker exec postgres-primary psql -U postgres -d edumanage -c \
  "UPDATE students SET class_name='部署验证' WHERE id=1;"
sleep 3
docker exec redis redis-cli XLEN cdc:events          # ≥ 1
docker exec postgres-primary psql -U postgres -d edumanage -c \
  "SELECT metric_name, dimension->>'op' AS op FROM kpi_events
   WHERE tags->>'source'='redis_cdc' ORDER BY event_time DESC LIMIT 1;"
# → student_count | u
```

## 5. 日常运维

### 5.1 服务管理

```bash
docker compose ps                          # 状态
docker compose logs -f backend             # API 日志
docker compose logs -f cdc-collector       # CDC 采集日志
docker compose restart cdc-collector       # 重启采集器（复制槽保证不丢事件）
docker compose stop                        # 停止全部
docker compose down -v                     # 删除全部（含数据，危险）
```

### 5.2 CDC 链路监控

```bash
# 复制槽消费延迟（持续增长 = 采集器停了 → WAL 会堆积！）
docker exec postgres-primary psql -U postgres -d edumanage -c \
  "SELECT slot_name, active,
          pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)) AS wal_lag
   FROM pg_replication_slots;"

# Stream 深度与消费组 lag
docker exec redis redis-cli XLEN cdc:events
docker exec redis redis-cli XINFO GROUPS cdc:events   # 看 lag / pending

# 最近的 CDC 事件
docker exec redis redis-cli XREVRANGE cdc:events + - COUNT 5
```

建议告警项：

| 指标 | 阈值建议 | 含义 |
|------|---------|------|
| `pg_replication_slots.active = f` | 立即 | 采集器掉线 |
| `wal_lag` | > 500MB | WAL 堆积，磁盘风险 |
| `XINFO GROUPS` 的 `lag` | > 5000 | 消费跟不上，接近 maxlen 裁剪线 |
| `pending` | 长期 > 0 | 有消息未 ACK（消费者可能挂在半路） |

### 5.3 备份

```bash
# 逻辑备份
docker exec postgres-primary pg_dump -U postgres edumanage > backup_$(date +%F).sql
# Redis（Stream 数据可丢弃重建，一般无需备份）
docker exec redis redis-cli BGSAVE
```

## 6. 故障排查

| 现象 | 排查 | 处理 |
|------|------|------|
| cdc-collector 反复重启 | `docker logs cdc-collector` | 常见：PG 未就绪（等 healthy）、`publication "cdc_pub" does not exist`（补跑 05_publication.sql） |
| Stream 一直是 0 | 复制槽是否 active；发布表清单 `SELECT * FROM pg_publication_tables;` | 变更的表不在 cdc_pub 里则不会有事件 |
| kpi_events 无新数据但 Stream 有 | `docker logs backend | grep -i error` | 消费者写库失败会打 ERROR 日志 |
| `replication slot ... is active for PID` | 有旧 collector 进程占着槽 | `docker compose restart cdc-collector` |
| WAL 磁盘暴涨 | §5.2 的 wal_lag | 恢复采集器；确认不再用时删槽：`SELECT pg_drop_replication_slot('redis_cdc_slot');` |
| changed_fields 为空 | `SELECT relreplident FROM pg_class WHERE relname='students';` 应为 `f` | 补 `ALTER TABLE ... REPLICA IDENTITY FULL;` |

## 7. 从旧 Kafka 版本升级

在已部署过 Kafka 版的机器上：

```bash
git pull

# 1. 停掉并移除旧的三件套（数据卷无关，业务数据不受影响）
docker compose down --remove-orphans      # 新 compose 里已无 kafka/zk/connect，会作为 orphan 清掉
docker rm -f kafka kafka-connect zookeeper 2>/dev/null || true

# 2. 清理旧 Debezium 复制槽与发布（新 init 脚本也会自动 DROP dbz_publication）
docker exec postgres-primary psql -U postgres -d edumanage -c \
  "SELECT pg_drop_replication_slot('debezium_slot');" 2>/dev/null || true

# 3. 补跑新的发布脚本并重建后端
PGPASSWORD=postgres123 psql -h localhost -U postgres -d edumanage \
  -f docker/postgres/init/05_publication.sql
docker compose build backend
docker compose up -d backend cdc-collector
```

回滚：`git checkout <旧版本>` 后按旧版 operations.md 部署；新方案的复制槽记得删除。

## 8. 卸载

```bash
docker compose down -v          # 容器 + 数据卷
docker rmi demodatastack-backend demodatastack-frontend 2>/dev/null || true
```
