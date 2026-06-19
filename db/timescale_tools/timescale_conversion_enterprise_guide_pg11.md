# PostgreSQL 11 普通表转 TimescaleDB 超表企业实施指南

本文档面向本项目 `db/timescale_tools` 下的转换脚本与测试脚本，重点说明 PostgreSQL 11 背景下普通表转换为 TimescaleDB hypertable 的先决条件、实施步骤、验证方法、收益评估、回退策略和版本性能演进。

当前转换清单 `tables_log.ini` 只有一张待评估表：

| 表 | 时间分区列 | chunk_interval | segmentby | compress_after |
| --- | --- | --- | --- | --- |
| `sys_fep_comm_log` | `start_time` | `1 day` | `device_id` | `7 days` |

> 重要限制：PostgreSQL 11 只能使用较老的 TimescaleDB 版本。Tiger Data 官方兼容矩阵显示，PostgreSQL 11 支持 TimescaleDB `1.7.x`、`2.0.x`、`2.1.x-2.3.x`，从 `2.4.x` 开始不再支持 PostgreSQL 11。当前 `convert_to_timescale.py` 使用较新的 `create_hypertable(..., by_range(...))` 写法；在 PostgreSQL 11 兼容的 TimescaleDB 版本上必须先验证 API 是否支持，必要时改为旧签名：
>
> ```sql
> SELECT create_hypertable(
>   'sys_fep_comm_log_ts',
>   'start_time',
>   chunk_time_interval => INTERVAL '1 day',
>   if_not_exists => TRUE
> );
> ```

## 1. 企业级转换先决条件

### 1.1 平台与版本

1. PostgreSQL 版本为 11 时，先确认 `SELECT version();` 与 `SELECT extversion FROM pg_extension WHERE extname='timescaledb';`。
2. TimescaleDB 必须已安装并可加载：`shared_preload_libraries` 包含 `timescaledb`，数据库内可执行 `CREATE EXTENSION IF NOT EXISTS timescaledb;`。
3. 如果必须长期停留 PostgreSQL 11，建议锁定 TimescaleDB `2.3.x` 或已验证的 `1.7.x`/`2.0.x`，不要使用本文后面提到的 `2.7+`、`2.13+`、`2.20+` 性能能力。
4. 如果目标是使用新版 TimescaleDB 的列存、稀疏索引、向量化执行等能力，应先规划 PostgreSQL 主版本升级，而不是直接在 PostgreSQL 11 上转换。

### 1.2 表模型适配性

待转换表必须满足：

1. 有稳定的时间列，类型建议为 `timestamptz`、`timestamp` 或 `date`。本项目 `sys_fep_comm_log` 使用 `start_time`。
2. 时间列无空值，且业务写入不会大量写入极远未来或极远过去的数据。TimescaleDB 会自动对分区列施加 `NOT NULL` 约束。
3. 查询或生命周期管理以时间为核心：最近 N 小时/天查询、按时间聚合、按时间清理、按时间压缩归档。
4. 主键与唯一约束必须包含所有分区列。若未来对 `sys_fep_comm_log` 建唯一约束，不能只用 `id` 或 `device_id`，必须包含 `start_time`；如果有空间分区，还要包含空间分区列。
5. 外键、触发器、CDC、审计和 ORM 映射必须逐项确认。`convert_to_timescale.py` 的新建模式默认只复制列、默认值、生成列、存储参数和注释，不复制索引与约束。

### 1.3 业务与运行条件

1. 必须有完整备份和可恢复演练：物理备份或 `pg_basebackup`、WAL 归档、关键表逻辑备份。只生成 DDL 备份不足以保证数据不丢。
2. 必须有明确写入窗口策略。脚本新建 `_ts` 表并执行 `INSERT INTO ... SELECT * ...` 时，原表若持续写入，会出现转换后增量不一致。企业上线应选择停写窗口、双写、触发器同步或逻辑复制同步中的一种。
3. 必须评估 CDC。仓库 `docker/postgres/init/05_publication.sql` 中存在 `CREATE PUBLICATION dbz_publication FOR ALL TABLES;`，这类发布可能阻止创建 hypertable。转换前应临时调整 publication 或在无 CDC 的影子库执行。
4. 必须确认磁盘容量。新建模式会同时保留原表和 `_ts` 表，转换期间至少预留原表数据、索引、WAL、临时文件的额外空间，建议预留 2.5 倍以上目标表总大小。
5. 必须确认锁等待和事务时长。大表转换、索引重建、压缩策略创建都应在低峰执行，并设置可观测的 `lock_timeout`、`statement_timeout`。
6. 必须在预生产用生产级数据分布验证。不能只用 1 万行样例数据判断收益。

### 1.4 chunk 与压缩设计

`sys_fep_comm_log` 当前配置为 `chunk_interval = 1 day`。这适合日增量较大、按天或最近时间窗口查询的日志类表。chunk 过大时最近数据索引不易留在内存；chunk 过小时 chunk 数量过多，会增加规划开销并降低压缩效果。

经验约束：

1. 正在写入的 chunk 的索引总量应能放进约 `shared_buffers` 的 25%。
2. 高频写入或日索引增长较快时，用 `1 day` 或更短；低频写入可用 `7 days`。
3. `segmentby = device_id` 只有在常见查询经常按 `device_id` 过滤、分组或聚合时才合理；否则可能降低压缩查询效果。
4. `compress_after = 7 days` 表示 7 天前数据转为压缩/列存，适合“近期频繁写入、历史主要分析”的模式。若历史数据仍需频繁更新，应推迟压缩或不开压缩。

## 2. 转换步骤与验证步骤

### 2.1 推荐实施路径：新建超表、验证后切换

优先使用 `--new-table yes`，因为原表保留，回退最简单。

1. 盘点元数据：

```sql
SELECT count(*) AS rows,
       min(start_time) AS min_time,
       max(start_time) AS max_time
FROM sys_fep_comm_log;

SELECT pg_size_pretty(pg_total_relation_size('sys_fep_comm_log')) AS total_size;
```

2. 检查唯一约束和索引：

```sql
SELECT conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'sys_fep_comm_log'::regclass;

SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'sys_fep_comm_log';
```

3. 验证 dry-run：

```bash
cd db/timescale_tools
python3 convert_to_timescale.py tables_log.ini --dry-run
```

4. 在 PostgreSQL 11 的真实 TimescaleDB 版本上验证 dry-run 输出的 `create_hypertable` API。若不支持 `by_range()`，先调整脚本为旧签名。

5. 选择写入策略：

| 策略 | 适用场景 | 数据不丢要求 |
| --- | --- | --- |
| 停写窗口 | 可短暂停机或暂停日志写入 | 转换开始前停写，行数和校验通过后切换 |
| 双写 | 应用可同时写原表和 `_ts` 表 | 上线前后保留双写一段时间，校验增量 |
| 触发器同步 | 不易改应用但可加数据库触发器 | 原表 INSERT/UPDATE/DELETE 同步到 `_ts` 表 |
| 逻辑复制/影子库 | 大表、低停机要求 | 先全量，再持续同步 WAL 增量，最终短暂停写切换 |

6. 执行转换：

```bash
python3 convert_to_timescale.py tables_log.ini --suffix _ts
```

7. 补建必要索引。至少按真实 SQL 增加常用索引，例如：

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sys_fep_comm_log_ts_start_time
ON sys_fep_comm_log_ts (start_time DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sys_fep_comm_log_ts_device_time
ON sys_fep_comm_log_ts (device_id, start_time DESC);
```

8. 分析统计信息：

```sql
ANALYZE sys_fep_comm_log;
ANALYZE sys_fep_comm_log_ts;
```

9. 验证数据一致性：

```sql
SELECT
  (SELECT count(*) FROM sys_fep_comm_log) AS plain_rows,
  (SELECT count(*) FROM sys_fep_comm_log_ts) AS hyper_rows;

SELECT
  (SELECT min(start_time) FROM sys_fep_comm_log) AS plain_min,
  (SELECT min(start_time) FROM sys_fep_comm_log_ts) AS hyper_min,
  (SELECT max(start_time) FROM sys_fep_comm_log) AS plain_max,
  (SELECT max(start_time) FROM sys_fep_comm_log_ts) AS hyper_max;
```

10. 验证 hypertable 元数据：

```sql
SELECT *
FROM timescaledb_information.hypertables
WHERE hypertable_name = 'sys_fep_comm_log_ts';

SELECT chunk_schema, chunk_name, range_start, range_end
FROM timescaledb_information.chunks
WHERE hypertable_name = 'sys_fep_comm_log_ts'
ORDER BY range_start;
```

11. 切换前做性能验收。通过后在停写窗口内 rename：

```sql
BEGIN;
LOCK TABLE sys_fep_comm_log IN ACCESS EXCLUSIVE MODE;
LOCK TABLE sys_fep_comm_log_ts IN ACCESS EXCLUSIVE MODE;
ALTER TABLE sys_fep_comm_log RENAME TO sys_fep_comm_log_plain_bak;
ALTER TABLE sys_fep_comm_log_ts RENAME TO sys_fep_comm_log;
COMMIT;
```

12. 切换后验证应用、CDC、监控和备份任务。

### 2.2 原地转换路径

`python3 convert_to_timescale.py tables_log.ini --new-table no` 会在转换前备份原表 DDL，然后对原表执行 `create_hypertable(... migrate_data => true)`。

企业环境不建议把原地转换作为首选，因为：

1. PostgreSQL/TimescaleDB 不提供“把 hypertable 原地变回普通表”的简单反向命令。
2. DDL 备份不是数据备份，不能覆盖误操作、错误写入或转换后业务不兼容。
3. 一旦业务在转换后继续写入，回退需要从 hypertable 拷贝数据或使用 PITR。

只有在表较小、停机窗口明确、备份可恢复、并已在预生产演练成功时，才考虑原地转换。

## 3. 如何验证转换带来的效益

### 3.1 先定义收益口径

转换收益不能只看单次 SQL 耗时。建议至少记录：

1. 查询平均耗时、p95、p99。
2. `EXPLAIN (ANALYZE, BUFFERS)` 中的扫描行数、命中块、读取块、规划时间和执行时间。
3. 写入吞吐：行/秒、批量提交耗时、WAL 量。
4. 存储：表大小、索引大小、压缩前后大小。
5. 运维收益：按时间删除是否可由 `DELETE` 变为 `drop_chunks`，历史聚合是否可由全量扫描变为 continuous aggregate。

### 3.2 `sys_fep_comm_log` 应测试的场景

由于仓库没有 `sys_fep_comm_log` 的 DDL 和业务 SQL，以下是基于清单字段的可验证场景。最终结论必须以真实 SQL 和真实数据分布为准。

| 场景 | 普通表转超表可能提升的原因 | 建议 SQL 形态 | 预期提升数据量 |
| --- | --- | --- | --- |
| 最近时间窗口查询 | chunk exclusion 只扫描最近 chunk，避免扫描全表或大索引范围 | `WHERE start_time >= now() - interval '1 hour'` | `<100万行` 通常不明显；`100万-500万行` 取决于索引；`>=1000万行` 且时间窗口命中 `<20%` 数据时通常开始明显 |
| 最近 N 天按设备聚合 | 时间裁剪叠加 `device_id` 分段/索引，历史压缩后可减少 I/O | `WHERE start_time >= ... GROUP BY device_id` | `>=500万行` 可观察；`>=5000万行` 更容易稳定收益 |
| 单设备最近明细 | `(device_id, start_time DESC)` 索引叠加 chunk 裁剪 | `WHERE device_id=? ORDER BY start_time DESC LIMIT 100` | 数据总量 `>=1000万行` 且单设备跨度长时更明显；若普通表已有同等复合索引，小数据下差距有限 |
| 按小时/天汇总 | `time_bucket`、chunk 裁剪、后续 continuous aggregate | `time_bucket('1 hour', start_time)` | 原始查询 `>=1000万行` 有机会提升；若做 continuous aggregate，亿级历史聚合收益更明显 |
| 历史数据压缩查询 | 7 天前数据压缩，减少存储和 I/O | 查询 7 天前的冷数据，按 `device_id` 或时间聚合 | 当历史数据达到数 GB 或 `>=1000万行` 后才值得重点评估 |
| 数据保留清理 | `drop_chunks` 元数据级删除替代大批量 `DELETE` | 删除 90 天前数据 | 大表最明显；从千万级开始，批量 `DELETE` 的锁、WAL、膨胀成本会显著高于按 chunk 删除 |

不应期待明显提升的场景：

1. 不带 `start_time` 条件的全表随机查询。
2. 只按业务主键或非时间列查单行，且普通表已有高选择性索引。
3. 小表，例如几十万行以内、缓存命中率很高的表。
4. 频繁 UPDATE/DELETE 历史行，尤其是已压缩 chunk。
5. 报表总是扫完整历史且没有 continuous aggregate。

### 3.3 数据量阈值的企业判定

固定阈值不存在，因为收益取决于硬件、索引、时间跨度、查询选择性和缓存命中率。建议用下面标准做准入：

| 数据规模 | 是否建议转换 | 判定 |
| --- | --- | --- |
| `<100万行` | 通常不建议为了性能转换 | 除非需要保留策略、压缩或统一时序模型 |
| `100万-500万行` | 可验证，不预设收益 | 只有时间窗口查询占比高时才可能明显 |
| `500万-1000万行` | 建议用生产 SQL 做 A/B 压测 | 若最近窗口查询 p95 改善 `>=30%`，可进入灰度 |
| `1000万-5000万行` | 通常值得评估 | chunk 裁剪、压缩、保留策略开始体现价值 |
| `>=5000万行` 或表大小数十 GB | 强烈建议评估 | 时间范围查询、历史压缩、删除保留的运维收益通常更明显 |
| `>=1亿行` | 应纳入时序架构设计 | 同时评估 chunk、压缩、continuous aggregate、归档和分区索引 |

### 3.4 A/B 压测方法

1. 从 `pg_stat_statements` 提取 `sys_fep_comm_log` 的 Top SQL，按总耗时、p95、高频调用排序。
2. 建立 `sys_fep_comm_log` 和 `sys_fep_comm_log_ts` 两张同数据表。
3. 在两张表上建立等价业务索引。不要拿“无索引普通表”对比“有索引超表”。
4. 每条 SQL 至少跑 5-10 次，丢弃首次冷启动结果，记录平均、最小、p95。
5. 对每条 SQL 保存：

```sql
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT ...
FROM sys_fep_comm_log
WHERE ...;

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT ...
FROM sys_fep_comm_log_ts
WHERE ...;
```

6. 记录结果到 `performance_comparison_template.csv`，用 Excel 打开即可计算提速倍数。
7. 验收建议：
   - 核心查询 p95 至少改善 `30%` 或提速 `>=1.3x`。
   - 写入吞吐下降不超过 `10%-15%`，或仍满足峰值 SLA。
   - 历史数据压缩后存储节省达到预期，例如 `>=40%`；实际压缩率必须以本表字段为准。
   - 回退演练成功，且能证明转换窗口内无数据丢失。

## 4. 数据不丢失的回退策略

### 4.1 新建模式回退

转换完成但未切换前：

1. 继续使用原表 `sys_fep_comm_log`。
2. 删除或保留 `_ts` 表均可：

```sql
DROP TABLE IF EXISTS sys_fep_comm_log_ts CASCADE;
```

已切换后：

1. 如果切换期间停写，且原表保留为 `sys_fep_comm_log_plain_bak`，可直接 rename 回退：

```sql
BEGIN;
LOCK TABLE sys_fep_comm_log IN ACCESS EXCLUSIVE MODE;
LOCK TABLE sys_fep_comm_log_plain_bak IN ACCESS EXCLUSIVE MODE;
ALTER TABLE sys_fep_comm_log RENAME TO sys_fep_comm_log_ts_failed;
ALTER TABLE sys_fep_comm_log_plain_bak RENAME TO sys_fep_comm_log;
COMMIT;
```

2. 如果切换后已经有新写入，必须先把增量从当前 hypertable 回灌普通表，再 rename。增量边界建议使用切换时间或单调递增主键：

```sql
INSERT INTO sys_fep_comm_log_plain_bak
SELECT *
FROM sys_fep_comm_log
WHERE start_time >= TIMESTAMPTZ '切换时间'
ON CONFLICT DO NOTHING;
```

3. 若没有唯一键可做 `ON CONFLICT`，必须先设计去重键或使用临时表比对校验，不能盲目插入。

### 4.2 原地转换回退

原地转换后，保守回退方式有两类：

1. 使用 PITR 或物理备份恢复到转换前时间点。这是最可靠的数据完整性方案。
2. 新建普通表并从 hypertable 拷贝数据，再切回：

```sql
CREATE TABLE sys_fep_comm_log_plain_restore
(LIKE sys_fep_comm_log INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING IDENTITY INCLUDING STORAGE INCLUDING COMMENTS);

INSERT INTO sys_fep_comm_log_plain_restore
SELECT * FROM sys_fep_comm_log;

-- 补建约束和索引，校验行数、min/max、抽样 checksum 后再 rename 切换
```

无论哪种回退，都必须完成：

1. 行数一致。
2. 时间范围一致。
3. 关键业务维度分组计数一致，例如按天、按 `device_id`。
4. 抽样 checksum 一致。
5. 应用读写冒烟测试通过。
6. CDC/备份/监控恢复到预期状态。

## 5. TimescaleDB 大版本性能演进

### 5.1 PostgreSQL 11 可用范围

| TimescaleDB 版本 | PostgreSQL 11 可用性 | 理论性能/能力收益 | 对本项目的含义 |
| --- | --- | --- | --- |
| `1.7.x` | 支持 | continuous aggregate 实时聚合、压缩和 hypertable 查询相关修复 | 可用于 PG11 老环境，但功能和性能优化较老 |
| `2.0.x` | 支持 | distributed hypertable GA、continuous aggregate API 改进、更多社区功能 | 单节点表转换可用，但需检查脚本 API 兼容 |
| `2.1.x-2.3.x` | 支持 | `2.2` 引入 Skip Scan 优化 `DISTINCT ON`；`2.3` 支持向压缩 chunk 插入并改善分布式插入 | PG11 环境建议优先验证 `2.3.x`，但仍不具备后续版本的列存优化 |
| `2.4.x+` | 不支持 PG11 | 后续大量性能优化不可直接使用 | 需要先升级 PostgreSQL |

### 5.2 升级 PostgreSQL 后可获得的后续能力

| TimescaleDB 版本 | 官方发布说明中的性能方向 | 说明 |
| --- | --- | --- |
| `2.7.x` | continuous aggregate 查询性能与存储优化、`now()` 规划时间优化、COPY 插入性能改善、PG14 UPDATE/DELETE chunk 排除 | 适合大量聚合与导入场景 |
| `2.11.x` | 压缩 chunk 上 DML、实时层级 continuous aggregate 性能改善、分布式 COPY 优化、减少压缩约束检查解压 | 历史压缩数据可维护性更好 |
| `2.13.x` | 压缩 chunk 排序路径、压缩元组 WAL 减少、`sum()` 向量化聚合 | 压缩历史分析更快，但需要新 PG |
| `2.15.x` | 压缩表达式下推、btree 列 min/max 稀疏索引、文本过滤向量化 | 有利于压缩数据上的过滤与聚合 |
| `2.18.x` | columnstore 二级索引、GROUP BY/过滤 SIMD 向量化 | 有利于列存分析查询 |
| `2.20.x` | columnstore bloom filter 点查最高 `6x`、严格约束 UPSERT 最高 `10x`、columnstore SkipScan 对选择性查询 `2000x-2500x`、bool 条件分析 `30%-45%` | 这些是官方特定场景数字，不能外推到所有 SQL |
| `2.28.x` | 压缩数据上 `first()`/`last()` 通过批元数据加速、continuous aggregate 刷新更轻、`CASE` 表达式进入向量化路径 | 对“每组最新值”和复杂聚合有帮助 |

结论：如果企业约束是 PostgreSQL 11，转换收益主要来自 hypertable 时间分区、chunk 裁剪、索引设计、保留策略和较早期压缩能力；不要把 `2.20+` 的列存性能数字作为 PostgreSQL 11 项目的承诺。

## 6. Excel 记录模板

模板文件：`db/timescale_tools/performance_comparison_template.csv`。

使用方式：

1. 用 Excel 打开 CSV。
2. 每条 SQL、每个数据量、每种 chunk/压缩组合填写一行。
3. 保留 `EXPLAIN` 输出文件路径或报告链接，便于审计。
4. 用 `speedup_x`、`latency_reduction_pct`、`storage_saving_pct` 判断是否达标。

## 7. 资料来源

1. Tiger Data 官方 PostgreSQL/TimescaleDB 兼容矩阵：<https://www.tigerdata.com/docs/self-hosted/latest/upgrades/upgrade-pg>
2. Tiger Data hypertable 原理：<https://docs2.tigerdata.com/docs/learn/hypertables/understand-hypertables>
3. Tiger Data 主键、时间列和唯一约束规则：<https://docs2.tigerdata.com/docs/learn/data-model/primary-keys-time-and-uniqueness>
4. Tiger Data chunk sizing 建议：<https://docs2.tigerdata.com/docs/learn/hypertables/sizing-hypertable-chunks>
5. Tiger Data hypertable 索引说明：<https://docs2.tigerdata.com/docs/learn/hypertables/hypertable-indexes>
6. Tiger Data 同库迁移普通表到 hypertable：<https://www.tigerdata.com/docs/self-hosted/latest/migration/same-db>
7. TimescaleDB 官方 release notes：<https://github.com/timescale/timescaledb/blob/main/CHANGELOG.md>
