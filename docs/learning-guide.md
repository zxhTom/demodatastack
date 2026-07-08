# 学习文档：PostgreSQL 表变动监听（CDC）× Redis Stream

本文面向想搞懂「不引入 Kafka，如何用最小组件实现 Postgres 表变动实时监听」的读者。
配套代码就在本仓库，每一节都可以在本地跑起来验证。

## 0. 这套栈教你什么

| 知识点 | 对应代码/配置 |
|--------|--------------|
| PG 逻辑复制（WAL、发布、复制槽、REPLICA IDENTITY） | `docker/postgres/init/05_publication.sql` |
| pgoutput 二进制协议解析 | `backend/app/cdc/pgoutput.py` |
| 逻辑复制客户端（START_REPLICATION / LSN 反馈） | `backend/app/cdc/collector.py` |
| Redis Stream 生产（XADD + maxlen 限长） | `backend/app/cdc/collector.py` |
| Redis Stream 消费者组（XREADGROUP / XACK / pending） | `backend/app/streams/consumer.py` |
| 事件落地时序库（TimescaleDB 超表） | `docker/postgres/init/04_timeseries.sql` |

---

## 1. PostgreSQL 逻辑复制基础

### 1.1 WAL 与 wal_level

PG 的一切修改先写 WAL（Write-Ahead Log）。`wal_level` 决定 WAL 里记多少信息：

- `minimal`：只够崩溃恢复
- `replica`：够物理复制（本项目的 postgres-replica 用的就是这个能力）
- `logical`：额外记录**足够还原行级变更**的信息 → CDC 的前提

本项目 `docker/postgres/primary/postgresql.conf`：

```ini
wal_level = logical
max_replication_slots = 10   # 每个 CDC 客户端占一个槽
max_wal_senders = 10
```

### 1.2 Publication（发布）

发布决定「哪些表的变更对外可见」：

```sql
CREATE PUBLICATION cdc_pub FOR TABLE
    departments, teachers, students, courses, semesters,
    enrollments, grades, attendance, course_schedules;
```

为什么不用 `FOR ALL TABLES`？两个坑：

1. 会把 `kpi_events` 等内部表也发布出去，消费端每写一条 KPI 又产生一条 WAL 事件（噪音，且有自激风险）。
2. TimescaleDB 的 `create_hypertable` 会拒绝在被 FOR ALL TABLES 发布覆盖的表上执行
   （报 `cannot create hypertable ... because it is part of a publication`）。

### 1.3 Replication Slot（复制槽）

复制槽是 PG 为每个 CDC 客户端维护的**书签**：记录客户端消费到的 LSN（日志序号），
并保证该位置之后的 WAL 不被清理。

```sql
SELECT pg_create_logical_replication_slot('redis_cdc_slot', 'pgoutput');
```

- 客户端断线重连后从上次确认的 LSN 继续 → **不丢事件**
- 代价：如果客户端一直不来消费，WAL 会持续堆积撑爆磁盘 → 见 §5 常见坑

### 1.4 REPLICA IDENTITY

默认（`DEFAULT`）时，UPDATE/DELETE 的 WAL 只带主键旧值。想拿到**完整旧行**
（用于对比哪些字段变了），要：

```sql
ALTER TABLE students REPLICA IDENTITY FULL;
```

本项目对 9 张业务表都设了 FULL，所以 KPI 事件里能记录 `changed_fields`。

### 1.5 pgoutput 协议速查

pgoutput 是 PG 内置的逻辑解码插件（Debezium 默认也用它），输出二进制消息，
每条消息第一个字节是类型标记：

| 标记 | 含义 | 关键内容 |
|------|------|----------|
| `B` | Begin | 事务开始 |
| `C` | Commit | 事务提交 |
| `R` | Relation | 表结构：relation_id、schema、表名、**列名列表** |
| `I` | Insert | relation_id + 新行（`N` + TupleData） |
| `U` | Update | relation_id + 旧行（`O`/`K`）+ 新行（`N`） |
| `D` | Delete | relation_id + 旧行（`K`/`O`） |

TupleData：2 字节列数，然后每列 1 字节类型（`n`=NULL，`u`=未变的 TOAST，
`t`=文本：4 字节长度 + 内容）。

**关键设计**：`I/U/D` 消息里只有 relation_id 没有表名，所以解析器必须缓存 `R` 消息
建立 relation_id → (表名, 列名) 映射——PG 保证在发送某表第一条变更前，先发它的 `R` 消息。
这正是 `pgoutput.py` 里 `self.relations` 字典的作用。参考项目 tenant-dashboard 的解析器
没有处理 `R` 消息，只能拿到 `col_0/col_1` 这样的匿名列；本项目补全了这一步。

---

## 2. Redis Stream 基础（Kafka 平替的关键）

### 2.1 为什么不用 Pub/Sub 或 List？

| 结构 | 问题 |
|------|------|
| Pub/Sub | 不落盘、消费者离线期间的消息**直接丢失** |
| List (LPUSH/BRPOP) | 消息被 pop 后就没了，无法多消费组、无确认机制 |
| **Stream** | 持久化、支持消费者组、ACK/pending 重投、按 ID 范围查询 —— 最接近 Kafka |

### 2.2 与 Kafka 概念对照

| Kafka | Redis Stream | 本项目取值 |
|-------|--------------|-----------|
| Topic | Stream key | `cdc:events` |
| Producer | `XADD` | cdc-collector |
| Consumer Group | `XGROUP` | `kpi-consumer-group` |
| poll() | `XREADGROUP BLOCK` | backend 消费循环 |
| commit offset | `XACK` | 处理成功后确认 |
| 未提交消息 | PEL（Pending Entries List） | `XPENDING` 可查 |
| retention | `MAXLEN` 裁剪 | `maxlen ~ 10000` |

### 2.3 常用命令（可直接对本项目实操）

```bash
docker exec redis redis-cli XLEN cdc:events                  # 流长度
docker exec redis redis-cli XRANGE cdc:events - + COUNT 3    # 看最早3条
docker exec redis redis-cli XINFO STREAM cdc:events          # 流元信息
docker exec redis redis-cli XINFO GROUPS cdc:events          # 消费组进度/lag
docker exec redis redis-cli XPENDING cdc:events kpi-consumer-group  # 未ACK消息
```

---

## 3. 代码走读

### 3.1 采集端 `backend/app/cdc/collector.py`

```
run_once():
    redis.ping()                                  # Redis 不通就别启动
    conn = psycopg2.connect(..., connection_factory=LogicalReplicationConnection)
    ensure_slot(conn)                             # 幂等创建复制槽
    cur.start_replication(slot_name=..., decode=False,
        options={"proto_version": "1", "publication_names": "cdc_pub"})
    cur.consume_stream(CdcCollector(redis))       # 阻塞循环，逐条回调
```

回调 `CdcCollector.__call__` 三件事：

1. `parser.parse(msg.payload)` —— pgoutput 二进制 → 事件 dict（控制消息返回 None）
2. `XADD cdc:events {payload: json} MAXLEN ~ 10000`
3. `msg.cursor.send_feedback(flush_lsn=msg.data_start)` —— 告诉 PG「这条我拿到了」，
   复制槽推进书签。**先推 Redis 再确认 LSN**，顺序保证了 at-least-once。

外层 `main()` 是无限重连循环：任何异常等 5 秒重来，进度靠复制槽，不靠内存状态。

### 3.2 协议解析 `backend/app/cdc/pgoutput.py`

- `_parse_relation()`：解析 `R` 消息存表结构。
- `parse()`：对 `I/U/D` 取出 relation_id 查映射，循环解析子消息
  （`O`/`K`→before，`N`→after），列名和值 zip 成 dict。
- 约 100 行，无第三方依赖。对照 §1.5 的协议表读，一遍就能看懂。

### 3.3 消费端 `backend/app/streams/consumer.py`

```
start_stream_consumer():                    # 随 FastAPI lifespan 启动
    XGROUP CREATE cdc:events kpi-consumer-group $ MKSTREAM   # 幂等
    loop:
        msgs = XREADGROUP ... {cdc:events: ">"} COUNT 100 BLOCK 5000
        for msg: process_cdc_event(json.loads(payload)); XACK
```

`process_cdc_event`：查 `METRIC_MAP` 把表名映射成指标名，
`OP_DELTA` 把 c/u/d 映射成 +1/0/-1，UPDATE 时用 before/after 对比出
`changed_fields`，最后写一行 `kpi_events`。

处理失败也会 XACK（KPI 允许丢单条，换取不堵队列）；如果业务要求严格，
应改成不 ACK 并用 `XAUTOCLAIM` 做重试——这是留给读者的练习。

---

## 4. 动手实验

前置：按[部署手册](deployment-manual.md)把栈跑起来。

### 实验 1：看一条变更的完整旅程

```bash
# 1. 造一条变更
docker exec postgres-primary psql -U postgres -d edumanage -c \
  "UPDATE students SET class_name='实验班' WHERE id=1;"

# 2. Redis Stream 里看到原始事件（before/after 完整旧新行）
docker exec redis redis-cli XRANGE cdc:events - + COUNT 5

# 3. kpi_events 里看到落地的 KPI 事件（含 changed_fields）
docker exec postgres-primary psql -U postgres -d edumanage -c \
  "SELECT metric_name, dimension->>'op' op, dimension->>'changed_fields' ch
   FROM kpi_events WHERE tags->>'source'='redis_cdc'
   ORDER BY event_time DESC LIMIT 3;"
```

### 实验 2：验证断线不丢事件（复制槽的作用）

```bash
docker stop cdc-collector                       # 停掉采集器
docker exec postgres-primary psql -U postgres -d edumanage -c \
  "UPDATE students SET class_name='离线期间改的' WHERE id=2;"
docker exec redis redis-cli XLEN cdc:events     # 记住当前长度 N
docker start cdc-collector && sleep 5
docker exec redis redis-cli XLEN cdc:events     # 变成 N+1：离线变更被补上了
```

原理：collector 停机期间 WAL 停在复制槽书签处不被清理，重启后从 LSN 续读。

### 实验 3：观察复制槽状态与堆积

```bash
docker exec postgres-primary psql -U postgres -d edumanage -c \
  "SELECT slot_name, active, confirmed_flush_lsn,
          pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)) AS lag
   FROM pg_replication_slots;"
```

`active=t` 表示 collector 在线；`lag` 是未消费 WAL 量，持续增长说明消费停了。

### 实验 4：消费者组语义

```bash
docker exec redis redis-cli XINFO GROUPS cdc:events
# lag=0 表示全部消费完；pending>0 表示有已投递未 ACK 的消息
```

---

## 5. 常见坑（本项目踩过的）

1. **复制槽遗忘 = 磁盘炸弹**。删掉 cdc-collector 却不删槽，WAL 会无限堆积：
   `SELECT pg_drop_replication_slot('redis_cdc_slot');`
2. **FOR ALL TABLES 发布挡住 create_hypertable**（见 §1.2）。老版本用 Debezium 默认
   建的 `dbz_publication FOR ALL TABLES` 就撞过这个坑，新 init 脚本已改为具名表发布。
3. **REPLICA IDENTITY 不是 FULL 时 UPDATE 拿不到旧行** → changed_fields 永远是空。
4. **maxlen 裁剪**：消费严重滞后时最旧消息会被丢。监控 `XINFO GROUPS` 的 lag。
5. **SQLAlchemy 属性名 ≠ 列名**：`KpiEvent.tags_data` 映射列 `tags`，构造时必须用
   `tags_data=`。旧 Kafka 消费者传 `tags=` 导致每条事件都写库失败——静默异常 + 只打
   一行 error 日志，这个 bug 在旧链路上存在了很久没被发现，教训是**消费端写库失败
   必须有告警或计数**，不能只 rollback 了事。
6. **一个复制槽只能一个消费者**。想再加一条独立管道（如审计），建第二个槽 + 发布，
   不要复用 `redis_cdc_slot`。

---

## 6. 延伸阅读

- PG 官方：Logical Decoding / pgoutput 协议（`protocol-logical-replication`）
- Redis 官方：Streams intro（`redis.io/docs/data-types/streams`）
- Debezium PostgreSQL Connector 文档（对比自研方案与工业级实现的差距：
  快照、DDL 演进、精确一次、类型系统）
- 参考项目：[tenant-dashboard](https://github.com/zxhTom/tenant-dashboard) 的
  `PG_CDC_Redis_Stream_方案.md` 与 `backend/pg_cdc_collector.py`
