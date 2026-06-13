# 数据仓库可靠性分析与漏洞修复方案

## 1. 架构漏洞分析

### 漏洞 1：Kafka 消息丢失（消费者宕机）

**场景描述：**
Backend Kafka Consumer（aiokafka）宕机或重启时，Kafka 可能从最新 offset 继续消费，
造成宕机期间的 CDC 事件**永久丢失**，无法反映到 kpi_events 表。

**当前状态分析：**
```
Debezium → Kafka Topic (retention: 7天)
                    ↓
          Backend Consumer (auto.offset.reset=latest)
                    ↓
          如果宕机后重启 → 跳过了宕机期间的消息
```

**解决方案：**

1. **持久化 Offset**（已部分实现）：
   - 使用 `group_id = "kpi-consumer-group"` + Kafka 内置 offset 提交
   - 重启后自动从上次提交的 offset 继续消费
   - 将 `auto_offset_reset` 改为 `"earliest"` 确保不遗漏历史消息

2. **幂等消费**：
   - 在 kpi_events 表添加 `source_lsn` 列记录 PostgreSQL LSN
   - 插入前检查 LSN 是否已处理（避免重复）

3. **消息保留时间**：
   - Kafka 默认 7 天保留，足够服务恢复
   - 建议配置：`retention.ms = 604800000`（7天）

**修复代码示例：**
```python
# backend/app/kafka/consumer.py 改进
consumer = AIOKafkaConsumer(
    *CDC_TOPICS,
    bootstrap_servers=settings.kafka_bootstrap_servers,
    group_id="kpi-consumer-group",
    auto_offset_reset="earliest",          # 改为 earliest
    enable_auto_commit=False,              # 手动提交 offset
    value_deserializer=lambda m: json.loads(m.decode("utf-8")),
)

async for msg in consumer:
    await process_cdc_event(msg.topic, msg.value)
    await consumer.commit()                # 处理成功后再提交
```

---

### 漏洞 2：Debezium Replication Slot 积压

**场景描述：**
Debezium 使用 PostgreSQL Logical Replication Slot（`debezium_slot`）。
如果 Kafka Connect 长时间宕机，Replication Slot 会持续积累 WAL 日志，
导致 PostgreSQL 磁盘空间耗尽，**数据库无法写入**。

**危险程度：高** ⚠️

**解决方案：**

1. **监控 Replication Slot 积压**：
```sql
-- 监控 WAL 积压大小
SELECT slot_name, 
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS lag
FROM pg_replication_slots
WHERE slot_type = 'logical';
```

2. **设置 WAL 积压告警阈值**（运维手册中配置）：
```sql
-- PostgreSQL 参数
max_slot_wal_keep_size = 10GB  -- 超过则自动丢弃 slot
```

3. **Debezium 高可用部署**（生产建议）：
   - Kafka Connect 多节点集群（3节点）
   - 自动故障转移

---

### 漏洞 3：PostgreSQL 主库宕机

**场景描述：**
PostgreSQL Primary 宕机后，Replica 无法自动晋升为 Primary（需要手动操作），
系统进入**只读模式**，所有写入请求失败。

**当前状态：**
- Replica 处于 hot_standby 模式（可读）
- 无自动故障转移（Failover）

**解决方案：**

**方案 A（简单）：手动故障转移**
```bash
# 在 Replica 上执行
pg_ctl promote -D /var/lib/postgresql/data
# 然后修改应用 DATABASE_URL 指向 Replica
```

**方案 B（生产推荐）：Patroni 高可用**
```yaml
# Patroni 自动管理 PostgreSQL 主从切换
# 配合 etcd/consul 做 Leader Election
patroni:
  postgresql:
    parameters:
      wal_level: logical
  bootstrap:
    dcs:
      postgresql:
        parameters:
          max_replication_slots: 10
```

---

### 漏洞 4：Kafka 单点故障

**场景描述：**
当前 Kafka 只有 1 个 Broker，宕机后消息队列完全不可用。

**解决方案（生产环境）：**
```yaml
# docker-compose 扩展为 3 节点 Kafka 集群
kafka-1:
  KAFKA_BROKER_ID: 1
  KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 3
  KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: 3

kafka-2:
  KAFKA_BROKER_ID: 2

kafka-3:
  KAFKA_BROKER_ID: 3
```

Topics 设置 `replication.factor=3`，`min.insync.replicas=2`。

---

### 漏洞 5：KPI 数据最终一致性

**场景描述：**
kpi_events 记录的是增量变化（+1/-1），而非当前总量快照。
如果某些 CDC 事件丢失，累积计数会产生偏差，**与实际数据不符**。

**解决方案：**

1. **定期对账**（每天执行一次）：
```python
async def reconcile_kpi():
    """从 PostgreSQL 直接统计真实数量，写入基准 KPI 事件"""
    stats = await get_kpi_stats_data(db)
    for metric, value in stats.items():
        await db.execute(
            "INSERT INTO kpi_events (metric_name, metric_value, tags) VALUES (%s, %s, %s)",
            (metric, value, json.dumps({"type": "reconcile"}))
        )
```

2. **使用快照+增量的混合模式**：
   - 每小时写入一次绝对值快照
   - 增量事件用于实时性
   - Dashboard 展示时优先使用最近快照值

---

## 2. 数据完整性保障机制

### 2.1 端到端数据追踪

```
PostgreSQL WAL LSN → Kafka Offset → kpi_events.source_lsn

检查点：
1. Debezium lag: 监控 Kafka Connect offset 落后于 WAL 的距离
2. Consumer lag: 监控 Consumer group 的 Kafka lag
3. DB count vs KPI count: 定期对账
```

### 2.2 数据丢失恢复流程

**Scenario A: Debezium 宕机 < Kafka retention 时间**
```
1. 重启 Kafka Connect
2. Debezium 自动从 Replication Slot 断点继续读取
3. 自动追赶积压的 WAL，重播到 Kafka
4. Consumer 自动处理积压消息（consumer group offset 未变）
✅ 零数据丢失
```

**Scenario B: Debezium 宕机 > Kafka retention 时间（极端情况）**
```
1. Kafka 中的历史消息已过期删除
2. 重建 Debezium Connector，snapshot.mode=initial
3. 对 PostgreSQL 做全量快照，重建 kpi_events 基准
4. 从快照后开始正常 CDC
⚠️ 宕机期间的增量事件丢失，但最终状态一致
```

---

## 3. 从数据仓库角度的改进建议

### 3.1 Lambda 架构

当前系统是简化的 **Kappa 架构**（仅流式处理）。
生产级数据仓库建议采用 **Lambda 架构**：

```
                      ┌─── Batch Layer ────────────────┐
                      │ 每日全量 ETL → 数据仓库 (DWH)   │
数据源 ──► Kafka ─────┤                                 ├──► Serving Layer → 查询
                      │ 每秒增量 CDC → kpi_events (TS)  │
                      └─── Speed Layer ─────────────────┘
```

### 3.2 分区策略优化

当前 kpi_events 按 1 小时分区，建议生产环境按业务量调整：
- 小业务量：1 天分区
- 中业务量：1 小时分区（当前）
- 大业务量：15 分钟分区

### 3.3 数据压缩

TimescaleDB 的列式压缩（需要 TimescaleDB 企业版）可以将存储空间减少 10-20 倍，
开源版可以使用 PostgreSQL 的 `pg_partman` + TOAST 压缩替代。

### 3.4 监控告警指标

| 指标 | 告警阈值 | 处理方式 |
|-----|---------|---------|
| Replication Slot WAL lag | > 1 GB | 立即检查 Kafka Connect |
| Kafka Consumer lag | > 10,000 条 | 扩容 Consumer 实例 |
| kpi_events 写入延迟 | > 5 秒 | 检查数据库连接池 |
| PostgreSQL 主从复制延迟 | > 10 秒 | 检查网络和负载 |

---

## 4. 总结

| 风险 | 严重度 | 当前状态 | 推荐修复 |
|------|-------|---------|--------|
| Kafka Consumer 宕机丢数据 | 中 | 🔴 使用 latest offset | 改为 earliest + 手动提交 |
| Replication Slot 积压耗尽磁盘 | 高 | 🔴 无监控 | 配置 max_slot_wal_keep_size |
| PostgreSQL 主库单点 | 高 | 🔴 无自动切换 | 部署 Patroni |
| Kafka 单点 | 中 | 🔴 单 Broker | 扩展为 3 节点集群 |
| KPI 数据偏差 | 低 | 🟡 无对账 | 每日定期对账 |
| JWT 无状态（无法吊销） | 低 | 🟡 无黑名单 | Redis 存储 JWT 黑名单 |
