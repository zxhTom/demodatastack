# 教务管理系统 运维手册

## 1. 快速启动

### 环境要求
- Docker >= 24.x
- docker-compose >= 2.x
- 磁盘空间 >= 20 GB（测试数据）
- 内存 >= 4 GB

### 一键启动

```bash
cd /home/zxhtom/temp/github/demodatastack

# 1. 启动基础服务（PostgreSQL、Zookeeper、Redis）
DOCKER_API_VERSION=1.44 docker-compose up -d postgres-primary zookeeper redis

# 2. 等待 PostgreSQL 健康（约30秒）
until DOCKER_API_VERSION=1.44 docker-compose ps postgres-primary | grep "(healthy)"; do sleep 3; done

# 3. 启动 Kafka
DOCKER_API_VERSION=1.44 docker-compose up -d kafka

# 4. 启动 Kafka Connect
DOCKER_API_VERSION=1.44 docker-compose up -d kafka-connect

# 5. 启动应用
DOCKER_API_VERSION=1.44 docker-compose up -d backend frontend

# 6. 注册 Debezium Connector
KAFKA_CONNECT_HOST=localhost bash docker/debezium/register-connector.sh
```

### 验证服务

```bash
# 检查所有容器状态
DOCKER_API_VERSION=1.44 docker-compose ps

# 验证后端
no_proxy=localhost curl http://localhost:8000/health

# 验证前端
no_proxy=localhost curl -I http://localhost:3000

# 验证 Kafka Connect
no_proxy=localhost curl http://localhost:8083/connectors

# 验证数据库
PGPASSWORD=postgres123 psql -h localhost -U postgres -d edumanage -c "SELECT COUNT(*) FROM students;"
```

---

## 2. 常用操作命令

### 服务管理

```bash
# 查看所有服务状态
DOCKER_API_VERSION=1.44 docker-compose ps

# 查看日志
DOCKER_API_VERSION=1.44 docker-compose logs -f backend
DOCKER_API_VERSION=1.44 docker-compose logs -f kafka-connect
DOCKER_API_VERSION=1.44 docker-compose logs -f postgres-primary

# 重启单个服务
DOCKER_API_VERSION=1.44 docker-compose restart backend

# 停止所有服务（保留数据）
DOCKER_API_VERSION=1.44 docker-compose stop

# 停止并删除容器（保留 volume）
DOCKER_API_VERSION=1.44 docker-compose down

# 清除所有数据（⚠️ 危险）
DOCKER_API_VERSION=1.44 docker-compose down -v
```

### 数据库操作

```bash
# 连接主库
PGPASSWORD=postgres123 psql -h localhost -U postgres -d edumanage

# 连接只读副本
PGPASSWORD=postgres123 psql -h localhost -p 5433 -U postgres -d edumanage

# 查看复制状态
PGPASSWORD=postgres123 psql -h localhost -U postgres -c "SELECT * FROM pg_stat_replication;"

# 查看 Replication Slots
PGPASSWORD=postgres123 psql -h localhost -U postgres -c "SELECT slot_name, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS lag FROM pg_replication_slots;"

# 查看 TimescaleDB chunks
PGPASSWORD=postgres123 psql -h localhost -U postgres -d edumanage -c "SELECT * FROM timescaledb_information.chunks WHERE hypertable_name = 'kpi_events';"
```

### Kafka 操作

```bash
# 查看所有 Topics
DOCKER_API_VERSION=1.44 docker-compose exec kafka kafka-topics --bootstrap-server kafka:9092 --list

# 消费 CDC 消息（实时查看）
DOCKER_API_VERSION=1.44 docker-compose exec kafka kafka-console-consumer \
  --bootstrap-server kafka:9092 \
  --topic edumanage.public.students \
  --from-beginning

# 查看 Consumer Group lag
DOCKER_API_VERSION=1.44 docker-compose exec kafka kafka-consumer-groups \
  --bootstrap-server kafka:9092 \
  --describe --group kpi-consumer-group

# 查看 Connector 状态
no_proxy=localhost curl http://localhost:8083/connectors/edumanage-postgres-connector/status
```

---

## 3. Debezium Connector 管理

```bash
# 查看 Connector 状态
no_proxy=localhost curl http://localhost:8083/connectors/edumanage-postgres-connector/status | python3 -m json.tool

# 重启 Connector
no_proxy=localhost curl -X POST http://localhost:8083/connectors/edumanage-postgres-connector/restart

# 暂停 Connector
no_proxy=localhost curl -X PUT http://localhost:8083/connectors/edumanage-postgres-connector/pause

# 恢复 Connector
no_proxy=localhost curl -X PUT http://localhost:8083/connectors/edumanage-postgres-connector/resume

# 删除并重新注册（重置 offset 和 slot）
no_proxy=localhost curl -X DELETE http://localhost:8083/connectors/edumanage-postgres-connector
PGPASSWORD=postgres123 psql -h localhost -U postgres -c "SELECT pg_drop_replication_slot('debezium_slot');"
KAFKA_CONNECT_HOST=localhost bash docker/debezium/register-connector.sh
```

---

## 4. 性能测试

```bash
# 100万条数据基准测试
python3 db/benchmarks/run_benchmark.py --rows 1000000

# 1000万条数据基准测试
python3 db/benchmarks/run_benchmark.py --rows 10000000

# 安装依赖
pip3 install psycopg2-binary
```

---

## 5. 故障排查

### 后端无法启动

```bash
# 查看错误日志
DOCKER_API_VERSION=1.44 docker-compose logs backend | tail -50

# 检查数据库连接
DOCKER_API_VERSION=1.44 docker-compose exec backend python3 -c "
import asyncio, asyncpg
async def test():
    conn = await asyncpg.connect('postgresql://postgres:postgres123@postgres-primary:5432/edumanage')
    print(await conn.fetchval('SELECT COUNT(*) FROM students'))
    await conn.close()
asyncio.run(test())
"
```

### Debezium Connector 失败

```bash
# 查看 Connector 错误信息
no_proxy=localhost curl http://localhost:8083/connectors/edumanage-postgres-connector/status

# 常见原因：
# 1. Replication Slot 已存在 → 删除 slot 后重新注册
# 2. Publication 不存在 → 执行 05_publication.sql
# 3. WAL Level 不是 logical → 检查 postgresql.conf

PGPASSWORD=postgres123 psql -h localhost -U postgres -d edumanage -c "
SELECT * FROM pg_replication_slots;
SELECT * FROM pg_publication;
SHOW wal_level;
"
```

### Kafka Consumer Lag 过高

```bash
DOCKER_API_VERSION=1.44 docker-compose exec kafka kafka-consumer-groups \
  --bootstrap-server kafka:9092 \
  --describe --group kpi-consumer-group

# 如果 lag 过高，考虑重启 backend 服务
DOCKER_API_VERSION=1.44 docker-compose restart backend
```

---

## 6. 账号信息

| 服务 | 用户名 | 密码 |
|------|--------|------|
| PostgreSQL | postgres | postgres123 |
| PostgreSQL Replication | replicator | replicator123 |
| 系统管理员 | admin | admin123 |
| 教师账号 | teacher01 | teacher123 |
| 学生账号 | student01 | student123 |

---

## 7. 访问地址

| 服务 | URL |
|------|-----|
| 前端应用 | http://localhost:3000 |
| 后端 API | http://localhost:8000 |
| API 文档（Swagger） | http://localhost:8000/docs |
| Kafka Connect REST | http://localhost:8083 |
