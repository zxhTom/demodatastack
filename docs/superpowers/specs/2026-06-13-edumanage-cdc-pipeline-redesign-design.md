# EduManage CDC 管道重构设计文档

- 日期：2026-06-13
- 定位：务实单机优化（保持 docker-compose，不堆 HA）
- 产出：设计 + 落地重构（改真实代码）
- 核心抉择：看板实时数字由 **CDC 驱动的 push 读模型** 产生

---

## 1. 背景与问题陈述

EduManage 是一套教务管理系统，同时充当"实时数据仓库"演示。当前实现（见 `docs/architecture.md`、`docs/reliability-analysis.md`）存在一个**结构性问题**和一组正确性/可靠性缺陷。

### 1.1 结构性问题：CDC 管道是装饰性的

- 看板实时数字（`/kpi/stats`、WebSocket `/kpi/ws`）来自**直接 `SELECT count(*)`** 基表，每 5 秒轮询（`backend/app/routers/kpi.py:84-86`）。
- `Debezium → Kafka → consumer → kpi_events` 写入的 ±1 增量事件，**没有任何代码读它来显示头条数字**；`kpi_events` 仅被 `/recent-events`（活动流）读取。
- `broadcast_kpi_update()` 定义但**从未被调用**（死代码）——"WebSocket 推送"实际不存在，是前端轮询。
- 连续聚合视图 `kpi_summary_1min` **无任何代码查询**。

后果：需求 #6"想看流程是否实时"无法成立——数字不经过 Kafka，整条 CDC 链路宕机看板照常刷新。

### 1.2 正确性 / 丢数据缺陷

| 问题 | 位置 | 现状 |
|------|------|------|
| Offset = `latest` + 自动提交 | `consumer.py:94-95` | 首启跳过积压；可能"提交了但处理失败"→ 真丢。与文档声称相反 |
| 零幂等 | consumer 全局 | 无 `source_lsn`/去重，重投递即重复计数 |
| `kpi_events` 语义混乱 | `04_timeseries.sql:87-95` | 混入 `random()*100` 噪声 + 真实 ±1；`AVG` 聚合无意义 |
| `create_kpi_event` 用 `flush()` 不 `commit()` | `kpi.py:73` | 手动事件可能不落库 |

### 1.3 效率 / 耦合 / 安全 / 运维缺陷

- WebSocket 每连接每 5s 打一次 `count(*)`，非推送。
- Consumer 与 API 同进程（同一 backend 容器），API 重启即中断 CDC。
- Replica 纯摆设：backend 只连 primary，读不分流。
- JWT 无法吊销：Redis 配了 URL 但 auth 服务无 blacklist/jti/revoke。
- 复制槽无监控 + 无 `max_slot_wal_keep_size`：Kafka Connect 长宕 → WAL 积压 → 撑爆磁盘 → 数据库停写（高危静默故障）。
- 无对账：增量丢失后计数永久漂移。
- `SECRET_KEY` 明文默认值；`datetime.utcnow()` 已弃用且产生 naive 时间对裸 TIMESTAMPTZ。

---

## 2. 目标架构

```
浏览器 ──ws──▶ FastAPI(API)  ◀── 只读列表/历史 ──▶ PG Replica
   ▲                │ 订阅 Redis "kpi:updates"
   │ 推送            ▼
   └──────────── Redis ──(pub/sub 桥 + JWT 黑名单)
                     ▲ publish
PG Primary(WAL logical) ─▶ Debezium ─▶ Kafka ─▶ cdc-consumer (独立容器)
       ▲ 对账读 count(*)                            │ earliest + 手动提交 + LSN 去重
       └──── 对账任务(60s) ───────────────────────────┼─▶ kpi_current  (读模型/总量, 事务更新)
                                                     ├─▶ kpi_events   (只追加事件日志/时序)
                                                     └─▶ publish Redis "kpi:updates"
```

YAGNI 边界（**故意不做**，仅作为文档中的"生产演进路径"保留）：
- ❌ 3 节点 Kafka 集群
- ❌ Patroni/etcd 自动故障转移
- ❌ 多消费者扩容

---

## 3. 组件设计

### 3.1 读模型 `kpi_current`（新增表）

看板"实时总量"的服务源。每 metric 一行：

```sql
CREATE TABLE kpi_current (
    metric_name  VARCHAR(100) PRIMARY KEY,
    metric_value BIGINT       NOT NULL DEFAULT 0,
    last_lsn     BIGINT       NOT NULL DEFAULT 0,  -- 该 metric 对应表已应用的最大 LSN 水位
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);
```

- 不存历史（历史在 `kpi_events`）；只存"当前值 + 水位"。
- metric 与表的映射沿用现有 `METRIC_MAP`（students→student_count 等）。

### 3.2 consumer 处理流程（`cdc-consumer` 独立容器）

每条 CDC 事件按以下顺序处理（**一个 DB 事务内完成 ①②③，提交后才 ④⑤**）：

1. 解析 `op`（c/u/d/r）、`source.lsn`、`before/after`、`changed_fields`。
2. 幂等检查：`event.lsn > kpi_current.last_lsn[该表]`？否则**跳过**（重投递保护）。
3. 事务内：
   - ① `kpi_current.metric_value += delta`（c:+1, d:-1, u:0, r:0）
   - ② 追加 `kpi_events`（真实增量，带 `dimension={table,op,changed_fields,changes}`）
   - ③ `kpi_current.last_lsn = event.lsn`、`updated_at = now()`
4. 事务提交成功 → **手动 commit Kafka offset**。
5. → **`publish` Redis 频道 `kpi:updates`**（payload = 受影响 metric 的最新快照）。

Consumer 配置：
```python
AIOKafkaConsumer(
    *CDC_TOPICS,
    group_id="kpi-consumer-group",
    auto_offset_reset="earliest",   # 由 latest 改为 earliest
    enable_auto_commit=False,       # 手动提交
    ...
)
```

### 3.3 Bootstrap（首启 / 重建）

Consumer 启动时：
1. 跑一次**对账**：`SELECT count(*)` 各基表（读 primary）→ 覆盖写 `kpi_current.metric_value`。
2. 记录当时 `pg_current_wal_lsn()` 为所有表的 `last_lsn` 水位。
3. 之后只应用 `lsn > 水位` 的流事件。

→ 一举解决"快照 `r` 事件 + Kafka 历史积压"导致的双重计数。

### 3.4 定期对账任务

- 每 60s（consumer 容器内的后台任务），读 **primary** 重算 `count(*)` 覆盖 `kpi_current`，并 `publish` 一次校正快照。
- 增量层负责实时，对账层负责最终正确（诚实的 Kappa + 对账）。
- 对账**必须读 primary**（replica 有复制延迟，会引入假漂移）。

### 3.5 `kpi_events`（语义修正）

- 删除 `04_timeseries.sql` 中 `random()*100` 初始化噪声（第 86-95 行）。
- 仅存真实增量事件。
- 连续聚合 `kpi_summary_1min` 重定义：
  ```sql
  -- 由无意义的 AVG(metric_value) 改为：
  SUM(metric_value) AS net_change,   -- 每分钟净变化
  COUNT(*)          AS event_rate    -- 每分钟活动速率
  ```

### 3.6 Redis pub/sub 推送桥

- Consumer：`publish("kpi:updates", json(snapshot))`。
- 每个 API 实例：启动后台 `subscribe("kpi:updates")`，收到即推给本实例持有的所有 WebSocket 客户端。
- `broadcast_kpi_update()` 从死代码变为真推送的实现。
- WebSocket `/kpi/ws`：连接时推一次当前 `kpi_current` 快照；之后**只在有变化时被动推**，删除 5s 轮询循环。
- Redis 第一个真实职责达成。

### 3.7 读写分离（Replica 干活）

- 新增第二个只读 async engine 指向 `postgres-replica:5432`（宿主机 5433）。
- 路由规则：
  - 列表/历史只读端点（`/students` 列表、`/kpi/recent-events` 等）→ **replica**。
  - 写操作、`/kpi/stats` 实时读、对账 → **primary**（正确性优先）。
- 通过独立的 `get_read_db` 依赖注入区分。

### 3.8 JWT 可吊销（Redis 第二个真实职责）

- 签发 token 时加入 `jti`（uuid4）。
- 登出 / 封号：把 `jti` 写 Redis 黑名单，TTL = token 剩余寿命。
- `get_current_user` 依赖：解出 jti 后查黑名单，命中则 401。

### 3.9 可靠性 / 可观测

- `postgresql.conf` 增加 `max_slot_wal_keep_size = 2GB`：宁可让槽失效（显式可恢复故障）也不撑爆磁盘。
- 新增 `/health/cdc` 端点，一屏返回：
  - 复制槽 lag（`pg_replication_slots` WAL 距离）
  - consumer 各表 `last_lsn`
  - 最后事件时间
  - `kpi_current` vs `count(*)` 漂移量
- 毒丸保护（**轻量**，按决策）：处理失败重试 N 次（如 3 次），仍失败则记 ERROR 日志并跳过该消息（不建死信表）。

### 3.10 杂项硬伤修复

- `create_kpi_event`：`db.flush()` → `db.commit()`。
- 全量 `datetime.utcnow()` → `datetime.now(timezone.utc)`。
- `SECRET_KEY`：非 dev 环境强制从环境变量注入，禁止使用代码默认值（启动时校验）。

---

## 4. 数据流（重构后）

### 4.1 实时 KPI 流（核心，重构后真正端到端）
```
用户在前端录入数据 → PG Primary 写入 → WAL(logical)
  └─▶ Debezium(pgoutput) ─▶ Kafka topic edumanage.public.{table} (单分区)
        └─▶ cdc-consumer: 幂等检查 → 事务[kpi_current += delta, kpi_events 追加, last_lsn 更新]
              → commit offset → publish Redis "kpi:updates"
                    └─▶ API 订阅者 ─▶ WebSocket 推送 ─▶ 前端看板即时更新
```

### 4.2 对账流（校正）
```
每 60s: cdc-consumer 读 primary count(*) → 覆盖 kpi_current → publish 校正快照
```

### 4.3 只读分流
```
列表/历史端点 → read engine → PG Replica
```

---

## 5. 涉及改动清单（落地）

| 文件 | 改动 |
|------|------|
| `docker/postgres/init/04_timeseries.sql` | 删 random 噪声；新增 `kpi_current` 表；重定义连续聚合；新增 CDC topic 单分区约束（或在 connector 配置） |
| `docker/debezium/register-connector.sh` | topic 单分区；确认 `topic.prefix` |
| `docker/postgres/primary/postgresql.conf` | 增 `max_slot_wal_keep_size = 2GB` |
| `docker-compose.yml` | 新增 `cdc-consumer` 服务（同镜像不同 command）；backend 不再跑 consumer |
| `backend/app/kafka/consumer.py` | earliest+手动提交；LSN 去重；事务化更新 kpi_current+kpi_events；bootstrap 对账；定期对账；Redis publish；毒丸重试 |
| `backend/app/routers/kpi.py` | `/stats`、`/ws` 改读 `kpi_current`；WebSocket 纯推送；`broadcast_kpi_update` 接 Redis 订阅；`create_kpi_event` commit |
| `backend/app/models/education.py` | 新增 `KpiCurrent` model；时区修正 |
| `backend/app/database.py` | 新增 replica 只读 engine + `get_read_db` |
| `backend/app/routers/*.py` | 只读列表端点切 `get_read_db` |
| `backend/app/services/auth.py` | jti + Redis 黑名单 |
| `backend/app/config.py` | SECRET_KEY 强校验；replica url |
| `backend/app/main.py` | 启动 Redis 订阅后台任务；移除内嵌 consumer 启动 |
| `backend/app/routers/health.py`（新增） | `/health/cdc` |
| 文档 | 更新 `architecture.md`、`reliability-analysis.md`（标注已落地 vs 生产演进路径） |

---

## 6. 测试与验证

- **正确性**：录入 N 条 → 看板总量增 N；删除 → 减；重启 consumer（重投递）→ 总量不变（幂等验证）。
- **实时性**：前端录入到看板更新延迟（端到端经 Kafka）应 < 2s。
- **故障恢复**：停 cdc-consumer 期间录入数据 → 重启后追平（earliest 不丢）。
- **对账纠偏**：手工制造 kpi_current 偏差 → 60s 内对账自动校正。
- **复制槽**：停 Kafka Connect 观察 `/health/cdc` 的 slot lag 上升告警。
- **JWT 吊销**：登出后旧 token 立即 401。
- **读写分离**：列表查询命中 replica（可观测连接来源）。
- 用 Playwright 跑前端登录→录入→看板实时更新的端到端验证（需求 #4）。

---

## 7. 生产演进路径（不在本次落地范围）

供文档保留，演示数据仓库专业视角：
- Kafka 3 节点集群，`replication.factor=3`、`min.insync.replicas=2`。
- Patroni + etcd 实现 PG 自动故障转移。
- 消费者水平扩容（多分区 + 多实例消费者组）；届时 LSN 去重需改为 (table,partition) 水位或 (table,lsn) 去重集。
- 死信表 + 重放工具。
- Lambda 架构：批层每日全量 ETL + 速度层 CDC。
