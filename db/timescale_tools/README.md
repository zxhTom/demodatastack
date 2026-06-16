# TimescaleDB 工具集

把普通 PostgreSQL 表批量转成 TimescaleDB 超表(hypertable)，并对「普通表 vs 超表」做性能对比。

目录下文件：

| 文件 | 作用 |
| --- | --- |
| `db.env.example` | 数据库连接配置模板，复制成 `db.env` 改成你自己的 |
| `dbconfig.py` | 共享的连接配置加载器（两个脚本都用它） |
| `convert_to_timescale.py` | **脚本①** 按清单把普通表转成超表 |
| `tables.example.ini` | **清单模板** 描述要转哪些表、怎么分区/压缩 |
| `benchmark.py` | **脚本②** 普通表 vs 超表 性能对比，输出 Markdown 报告 |

---

## 0. 准备：配置数据库连接

```bash
cd db/timescale_tools
cp db.env.example db.env
# 编辑 db.env，填你自己的 host/port/库名/账号密码
vim db.env
```

连接信息的读取优先级：**环境变量 > db.env 文件 > 内置默认值**。
所以你也可以临时用环境变量覆盖，比如 `DB_HOST=1.2.3.4 python3 benchmark.py`。

验证配置是否正确：

```bash
python3 dbconfig.py          # 打印解析到的连接信息（密码已脱敏）
```

> 依赖：`pip install psycopg2-binary`。数据库需已安装 TimescaleDB 扩展
> （本项目 docker-compose 里的 `timescale/timescaledb:latest-pg16` 已自带）。

---

## 1. 脚本① 把普通表转成超表

### 1.1 写清单文件

清单是 INI 格式，**每个 `[section]` 就是一张要转换的表**，section 名即表名
（带 schema 写成 `[public.表名]`）。参照 `tables.example.ini`。

字段说明（只有 `time_column` 必填）：

| 字段 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `time_column` | ✅ | — | **分区列（时间维度）**，必须是 timestamp/timestamptz/date。这是超表的主分区键。 |
| `chunk_interval` | | `7 days` | 每个 chunk 覆盖的时间范围。经验法则：让单 chunk 数据量 ≈ 内存的 25%；写入越频繁区间越小。 |
| `segmentby` | | 空 | 列存压缩的分段列（`compress_segmentby`）。填查询最常用的过滤/分组列（如 `device_id`）。**填了才开启压缩。** |
| `orderby` | | `time_column DESC` | 压缩排序列（`compress_orderby`），影响压缩比和范围扫描，一般填时间列。 |
| `compress_after` | | 空 | 自动压缩策略：数据超过该时长后自动压缩（如 `30 days`）。需配合 `segmentby`。 |
| `space_partition` | | 空 | 二级（空间/哈希）分区列，多节点/高并行才需要。 |
| `number_partitions` | | `4` | `space_partition` 的分区数。 |
| `migrate_data` | | `true` | 表里已有数据时是否搬迁进超表（大表搬迁会持锁，生产先评估）。 |
| `if_not_exists` | | `true` | 表已是超表时跳过而非报错。 |

最小示例：

```ini
[sensor_data]
time_column = ts
```

完整示例：

```ini
[device_metrics]
time_column     = recorded_at
chunk_interval  = 1 day
segmentby       = device_id
orderby         = recorded_at DESC
compress_after  = 7 days
```

### 1.2 执行转换

```bash
# 先 dry-run 看看会执行哪些 SQL（不连库、不改数据）
python3 convert_to_timescale.py tables.example.ini --dry-run

# 确认无误后真正执行
python3 convert_to_timescale.py my_tables.ini

# 看每条执行的 SQL
python3 convert_to_timescale.py my_tables.ini -v
```

脚本会对每张表依次执行：`create_hypertable` →（可选）`add_dimension` 空间分区
→（可选）开启压缩 →（可选）`add_compression_policy`。每张表一个事务，
**失败自动回滚且不影响其它表**，最后汇总成功/失败数量。

> ⚠️ 普通表转超表的前提：分区列上**不能有不包含该列的 UNIQUE / 主键约束**。
> 若原表主键不含时间列，需要先把主键改成 `(原主键列, time_column)` 复合主键。

---

## 2. 脚本② 性能对比测试

自动建两张结构/索引/数据**完全相同**的表（一张普通表 `bench_plain`、一张超表
`bench_hyper`），灌入相同数据后跑同一组查询/写入，统计耗时并输出 Markdown 报告。

```bash
# 默认：只测查询，100 万行
python3 benchmark.py

# 只测写入
python3 benchmark.py --mode insert

# 查询 + 写入都测，200 万行，每项重复 5 次取平均
python3 benchmark.py --mode both --rows 2000000 --runs 5 --out report.md
```

主要参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--mode {query,insert,both}` | `query` | 测试类型，**默认只测查询** |
| `--rows N` | `1000000` | 灌入的数据行数 |
| `--runs N` | `3` | 每个查询重复次数取平均 |
| `--chunk-interval STR` | `1 day` | 超表 chunk 区间 |
| `--segmentby COL` | `device_id` | 压缩分段列；传 `''` 则不开压缩（也就不出压缩比） |
| `--out PATH` | `benchmark_report.md` | 报告输出路径 |
| `--env-file PATH` | 同目录 `db.env` | 指定连接配置文件 |
| `--keep` | 否 | 结束后保留 `bench_plain` / `bench_hyper` 两张表，否则自动 DROP |

报告内容包括：存储/压缩比、各查询的普通表 vs 超表耗时与提速倍数、（如测写入）写入吞吐。

> 测的查询覆盖：最近 1 小时范围扫描、按天聚合、按设备分组、单设备明细、时间窗口+过滤聚合。
> 数据量越大、范围查询越多，超表优势越明显；小数据集差距可能不明显。

---

## 常见问题

- **报 `extension "timescaledb" is not available`**：你的 PG 没装 TimescaleDB，
  用本项目的 docker（`timescale/timescaledb` 镜像）或自行安装扩展。
- **转换报 unique 约束相关错误**：见上面 ⚠️，把主键调整为包含时间列的复合主键。
- **想换库**：改 `db.env`，或临时 `DB_NAME=xxx python3 ...`。
- **报 `cannot create hypertable ... because it is part of a publication`**：
  本项目的 `edumanage` 库为了 CDC 建了 `dbz_publication FOR ALL TABLES`，
  它会把所有表（含你要转的表）纳入逻辑复制，从而**阻止建超表**。处理办法：
  1. 临时去掉发布 → 转换 → 重建为只含需要的表的具名发布：
     ```sql
     DROP PUBLICATION dbz_publication;                 -- 临时移除
     -- 这里运行 convert_to_timescale.py 完成转换
     CREATE PUBLICATION dbz_publication FOR TABLE 表A, 表B;  -- 重建（按需列出表）
     ```
     注意：`FOR ALL TABLES` 没法单独排除某张表，所以重建时要改成具名发布。
  2. 或在不带此发布的库上操作（脚本②的性能测试就是在独立临时库里跑，天然不受影响）。
- **脚本②会不会动我的业务数据**：不会。它每次新建一个 `bench_tmp_<pid>` 临时库，
  在里面建表、灌数据、测完即删，与业务库/CDC 完全隔离。
