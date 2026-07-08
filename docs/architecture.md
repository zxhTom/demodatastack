# 教务管理系统 - 架构解说（Redis Stream CDC 版）

> 2026-07 架构整改：移除 Kafka + Zookeeper + Kafka Connect(Debezium) 三个组件，
> 改用「PG 逻辑复制 + 自研采集器 + Redis Stream」实现同样的表变动监听能力。
> 整改思路参考 [tenant-dashboard](https://github.com/zxhTom/tenant-dashboard) 项目的
> PG CDC + Redis Stream 方案。

## 1. 系统概述

本系统是一套基于微服务 + 事件驱动架构的教务管理平台，集成实时数据管道，支持：

- 教务数据管理（学生/教师/课程/成绩）
- 数据库变更实时捕获（CDC，秒级延迟）
- 时序数据存储与分析（TimescaleDB）
- 实时 KPI 监控仪表盘（WebSocket）

---

## 2. 架构图

```
┌────────────────────────────────────────────────────────────────────┐
│                       Docker Network (app-network)                  │
│                                                                     │
│  ┌──────────────┐         ┌───────────────────────────────────┐    │
│  │   Frontend   │  HTTP/  │       Backend API (FastAPI)       │    │
│  │  React+AntD  │◄───────►│  /api/auth /students /teachers …  │    │
│  │  Port: 3000  │   WS    │  /api/kpi (WebSocket)             │    │
│  └──────────────┘         │  内置 Redis Stream 消费者          │    │
│                           └────────┬────────────────┬─────────┘    │
│                                    │ 读写业务表      │ XREADGROUP   │
│                                    ▼                ▼              │
│  ┌─────────────────────────┐   ┌───────────────────────┐           │
│  │  PostgreSQL Primary     │   │        Redis          │           │
│  │  + TimescaleDB (pg16)   │   │  Port: 6380(宿主机)    │           │
│  │  wal_level = logical    │   │  Stream: cdc:events   │           │
│  │  Port: 5432             │   │  + 缓存(JWT黑名单等)   │           │
│  └───┬──────────────┬──────┘   └───────────▲───────────┘           │
│      │ 流复制        │ 逻辑复制(WAL)         │ XADD                 │
│      ▼              ▼                      │                      │
│  ┌──────────────┐  ┌───────────────────────┴────┐                  │
│  │ PostgreSQL   │  │      cdc-collector         │                  │
│  │ Replica      │  │  (Python 常驻进程)          │                  │
│  │ Port: 5433   │  │  复制槽 redis_cdc_slot      │                  │
│  │ (只读备份)    │  │  发布 cdc_pub (9张业务表)    │                  │
│  └──────────────┘  │  解析 pgoutput → JSON 事件  │                  │
│                    └────────────────────────────┘                  │
└────────────────────────────────────────────────────────────────────┘
```

---

## 3. 组件清单

| 容器 | 镜像 | 端口 | 职责 |
|------|------|------|------|
| postgres-primary | timescale/timescaledb:latest-pg16 | 5432 | 主写库，`wal_level=logical`，TimescaleDB 超表 |
| postgres-replica | timescale/timescaledb:latest-pg16 | 5433 | 流复制只读副本（读写分离/容灾） |
| redis | redis:7-alpine | 6380→6379 | **CDC 事件总线（Stream）** + 缓存 |
| cdc-collector | 与 backend 同镜像 | — | 监听 WAL 逻辑复制，推送变更到 Redis Stream |
| backend | python:3.10-slim 自建 | 8000 | REST API + Stream 消费者 + WebSocket |
| frontend | nginx 自建 | 3000 | React 管理界面 + 实时 Dashboard |

对比整改前：**少了 zookeeper、kafka、kafka-connect 三个容器**（约省 2GB+ 内存），
多了一个轻量的 cdc-collector（几十 MB）。

---

## 4. CDC 链路详解（表变动监听）

```
业务写入 (INSERT/UPDATE/DELETE)
  → PostgreSQL WAL 日志（wal_level=logical）
  → 发布 cdc_pub（只含 9 张业务表）+ 复制槽 redis_cdc_slot（pgoutput 插件）
  → cdc-collector：解析 pgoutput 二进制协议，还原成带列名的 before/after
  → XADD cdc:events（Redis Stream，maxlen≈10000 限长）
  → backend 消费者组 kpi-consumer-group：XREADGROUP 读取 → 处理 → XACK
  → 写入 kpi_events（TimescaleDB 超表）
  → 前端 Dashboard 经 /api/kpi/recent-events 与 WebSocket 实时展示
```

### 4.1 采集端（cdc-collector）

代码：`backend/app/cdc/`

- `pgoutput.py`：pgoutput 逻辑复制协议解析器（proto_version=1）。
  维护 relation_id → 表结构映射（来自 R 消息），把 I/U/D 消息还原成
  `{op, schema, table, before, after}`，值为文本格式。
- `collector.py`：常驻进程。创建/复用复制槽 → `START_REPLICATION` →
  逐条解析 WAL → `XADD` 到 `cdc:events` → `send_feedback` 确认 LSN。
  异常自动 5 秒重连，进度由复制槽持久化，**断线重启不丢事件**。

关键 PG 对象（由 `docker/postgres/init/05_publication.sql` 创建）：

| 对象 | 值 | 说明 |
|------|-----|------|
| Publication | `cdc_pub` | 只发布 9 张业务表；不用 FOR ALL TABLES，避免阻止 create_hypertable |
| Replication Slot | `redis_cdc_slot` | pgoutput 插件，记录消费进度（LSN） |
| REPLICA IDENTITY | FULL | UPDATE/DELETE 的 WAL 携带完整旧行，消费端才能算出 changed_fields |

### 4.2 事件格式

Stream 中每条消息一个 `payload` 字段，内容为 JSON（保持 Debezium 风格便于迁移）：

```json
{
  "op": "u",
  "schema": "public",
  "table": "students",
  "before": {"id": "31", "class_name": "测试班", "...": "..."},
  "after":  {"id": "31", "class_name": "整改验证班", "...": "..."},
  "ts_ms": 1783472753553
}
```

`op`: c=INSERT, u=UPDATE, d=DELETE。

### 4.3 消费端（backend）

代码：`backend/app/streams/consumer.py`

- 启动时 `XGROUP CREATE cdc:events kpi-consumer-group $ MKSTREAM`（幂等）。
- `XREADGROUP ... BLOCK 5000 COUNT 100` 批量拉取，处理成功后 `XACK`。
- 每条变更换算成 KPI 事件写入 `kpi_events` 超表：
  INSERT→+1、DELETE→-1、UPDATE→0 并记录 `changed_fields`。
- 消费者名含主机名，多副本 backend 时同组自动分摊消息。

---

## 5. Kafka 方案 vs Redis 方案对比

| 维度 | 旧：Debezium + Kafka + ZK | 新：collector + Redis Stream |
|------|---------------------------|------------------------------|
| 容器数 | +3（zk/kafka/connect） | +1（cdc-collector） |
| 内存占用 | 2~3 GB（JVM×3） | < 100 MB |
| 启动时间 | 2~3 分钟（Connect 最慢） | 秒级 |
| CDC 引擎 | Debezium（成熟、功能全） | 自研 pgoutput 解析（~100 行） |
| 消息保留 | 按 retention 保留，可重放全量 | maxlen 限长（默认 1 万条），只保留近期 |
| 消费模型 | consumer group + offset | consumer group + pending list（语义接近） |
| 断点续传 | Kafka offset + Debezium slot | PG 复制槽 LSN（同样不丢） |
| 横向扩展 | partition 级并行 | 单 stream 无分区（本规模足够） |
| 适用规模 | 大流量、多下游、需重放 | 中小流量、单一下游、追求轻量 |

**取舍说明**：事件在「PG → Redis」段由复制槽保证不丢；进入 Stream 后若消费严重滞后
且超过 maxlen，最旧的未消费消息可能被裁剪——这是换取轻量的主要代价。KPI 场景
可接受；若换成对账类场景，应调大 maxlen 或改回 Kafka。

---

## 6. 时序数据层

系统使用 TimescaleDB 扩展管理两类时序数据：

1. **system_logs_ts** - 系统日志（时序表）
   - 按天分区（`chunk_time_interval = 1 day`）
   - 用于与普通表 `system_logs` 进行性能对比

2. **kpi_events** - KPI 指标事件（时序表）
   - 按小时分区（`chunk_time_interval = 1 hour`）
   - 接收 CDC 事件转化的 KPI 指标
   - 附带 `kpi_summary_1min` 连续聚合视图（1分钟粒度）

---

## 7. 数据库 Schema 关系图

```
departments (院系)
    ├── teachers (教师) [1:N]
    ├── students (学生) [1:N]
    └── courses  (课程) [1:N]

semesters (学期) ──┐
courses   (课程) ──┼──► course_schedules (课程安排)
teachers  (教师) ──┘         ↓
classrooms(教室) ──────────►│
                              │
students  (学生) ──────────► enrollments (选课)
                                  ↓
                              grades (成绩)
                              attendance (考勤)
```

---

## 8. 安全设计

- JWT Token 认证（HS256，24小时有效期）
- bcrypt 密码哈希（cost factor 12）
- CORS 配置
- 数据库连接池（asyncpg，最大 30 连接）

---

## 9. 性能基准测试结果

### 时序表 vs 普通表（100万条）

| 查询类型 | 普通表 | TimescaleDB | 优势 |
|---------|-------|-------------|------|
| 近1小时计数 | 4.6ms | 4.7ms | 相当 |
| 24小时分组统计 | 36.5ms | 16.5ms | **TimescaleDB +122%** |
| 7天按小时聚合 | 91.5ms | 55.6ms | **TimescaleDB +65%** |
| 全表服务分组 | 58.0ms | 73.4ms | 普通表略快 |
| 插入速率 | 5,828 rows/s | 4,169 rows/s | 普通表 +40% |
| 磁盘大小 | 194 MB | 按分区 | TimescaleDB 可压缩 |

**结论**: TimescaleDB 在范围聚合查询（时间窗口统计）上有显著优势，特别适合大数据量的时序分析场景。

---

## 10. 端口汇总

| 服务 | 宿主机端口 | 容器端口 |
|------|-----------|---------|
| Frontend | 3000 | 80 |
| Backend API | 8000 | 8000 |
| PostgreSQL Primary | 5432 | 5432 |
| PostgreSQL Replica | 5433 | 5432 |
| Redis | 6380 | 6379 |

---

## 11. 相关文档

- 学习文档（原理 + 代码走读 + 动手实验）：[learning-guide.md](learning-guide.md)
- 部署手册（一键部署 + 运维 + 故障排查）：[deployment-manual.md](deployment-manual.md)
