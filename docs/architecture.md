# 教务管理系统 - 架构设计文档

## 1. 系统概述

本系统是一套基于微服务 + 事件驱动架构的教务管理平台，集成了实时数据仓库管道，支持：
- 教务数据管理（学生/教师/课程/成绩）
- 数据库变更实时捕获（CDC）
- 时序数据存储与分析
- 实时 KPI 监控仪表盘

---

## 2. 架构图

```
┌──────────────────────────────────────────────────────────────────────┐
│                           Docker Network (app-network)                │
│                                                                       │
│  ┌──────────────┐    ┌──────────────────────────────────────────────┐│
│  │   Frontend   │    │              Backend API (FastAPI)            ││
│  │  React + AntD│◄──►│   /api/auth  /departments  /students         ││
│  │  Port: 3000  │    │   /teachers  /courses  /enrollments /grades   ││
│  │  Nginx:80    │    │   /kpi (WebSocket)                            ││
│  └──────────────┘    │   Port: 8000                                  ││
│                      └───────┬──────────────┬─────────────────────── ││
│                               │              │                       ││
│         ┌─────────────────────▼──┐   ┌──────▼──────┐               ││
│         │   PostgreSQL Primary    │   │    Redis    │               ││
│         │   + TimescaleDB        │   │  Port: 6380 │               ││
│         │   Port: 5432           │   └─────────────┘               ││
│         │   WAL Level: logical   │                                  ││
│         └──┬──────────┬──────── ┘                                  ││
│            │ 流复制    │ 逻辑复制(WAL)                               ││
│  ┌─────────▼──────┐  ┌▼────────────────┐  ┌──────────────────────┐ ││
│  │ PostgreSQL      │  │ Debezium         │  │   Apache Kafka       │ ││
│  │ Replica         │  │ Kafka Connect    │  │ + Zookeeper          │ ││
│  │ Port: 5433      │  │ Port: 8083       │──►  Port: 9092/29092    │ ││
│  │ (只读备份)       │  │ pgoutput plugin  │  │ Topics: 10 个        │ ││
│  └─────────────────┘  └─────────────────┘  └──────────────────────┘ ││
│                                                          │            ││
│                              Backend Kafka Consumer ◄────┘            ││
│                              (aiokafka)                               ││
│                              ↓ 写入 kpi_events (TimescaleDB)          ││
│                              ↓ WebSocket 推送到前端 Dashboard          ││
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. 组件详情

### 3.1 数据层

| 组件 | 镜像 | 版本 | 用途 |
|------|------|------|------|
| PostgreSQL Primary | timescale/timescaledb | pg16 | 主写库，启用逻辑复制，安装 TimescaleDB |
| PostgreSQL Replica | timescale/timescaledb | pg16 | 只读副本，用于读取分离和容灾 |
| Redis | redis:7-alpine | 7.x | 缓存层（JWT黑名单、热数据） |

**PostgreSQL 关键配置：**
- `wal_level = logical`：支持 Debezium CDC
- `max_replication_slots = 10`：支持多个消费者
- `shared_preload_libraries = 'timescaledb'`：时序功能

### 3.2 消息队列层

| 组件 | 镜像 | 版本 | 用途 |
|------|------|------|------|
| Zookeeper | confluentinc/cp-zookeeper | 7.6.0 | Kafka 协调服务 |
| Kafka | confluentinc/cp-kafka | 7.6.0 | 消息队列，存储 CDC 事件 |
| Kafka Connect + Debezium | debezium/connect | 2.4 | CDC 捕获引擎 |

**CDC 流程：**
```
PostgreSQL WAL → pgoutput 插件 → Debezium Connector → Kafka Topics
```

### 3.3 应用层

| 组件 | 技术栈 | 端口 |
|------|--------|------|
| Backend API | FastAPI + SQLAlchemy + asyncpg | 8000 |
| Frontend | React + TypeScript + Ant Design + Vite | 3000 |

### 3.4 时序数据层

系统使用 TimescaleDB 扩展管理两类时序数据：

1. **system_logs_ts** - 系统日志（时序表）
   - 按天分区（`chunk_time_interval = 1 day`）
   - 用于与普通表 `system_logs` 进行性能对比

2. **kpi_events** - KPI 指标事件（时序表）
   - 按小时分区（`chunk_time_interval = 1 hour`）
   - 接收 Kafka CDC 事件转化的 KPI 指标
   - 附带 `kpi_summary_1min` 连续聚合视图（1分钟粒度）

---

## 4. 数据流

### 4.1 CRUD 数据流
```
用户浏览器 → nginx → React 前端 → Axios HTTP → FastAPI → SQLAlchemy → PostgreSQL
```

### 4.2 实时 CDC 数据流
```
PostgreSQL WAL 日志
  └─► Debezium Connector (pgoutput)
        └─► Kafka Topic (edumanage.public.{table})
              └─► Backend Kafka Consumer (aiokafka)
                    └─► kpi_events 表 (TimescaleDB)
                          └─► WebSocket 推送
                                └─► 前端 Dashboard 实时更新
```

### 4.3 主从复制流
```
PostgreSQL Primary (写入)
  └─► WAL Streaming → PostgreSQL Replica (只读)
```

---

## 5. 数据库 Schema 关系图

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

## 6. 安全设计

- JWT Token 认证（HS256，24小时有效期）
- bcrypt 密码哈希（cost factor 12）
- CORS 配置
- 数据库连接池（asyncpg，最大 30 连接）

---

## 7. 性能基准测试结果

### 7.1 时序表 vs 普通表（100万条）

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

## 8. 端口汇总

| 服务 | 宿主机端口 | 容器端口 |
|------|-----------|---------|
| Frontend | 3000 | 80 |
| Backend API | 8000 | 8000 |
| PostgreSQL Primary | 5432 | 5432 |
| PostgreSQL Replica | 5433 | 5432 |
| Redis | 6380 | 6379 |
| Zookeeper | 2181 | 2181 |
| Kafka | 9092, 29092 | 9092 |
| Kafka Connect | 8083 | 8083 |
