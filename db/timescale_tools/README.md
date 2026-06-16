# TimescaleDB 工具集

把普通 PostgreSQL 表批量转成 TimescaleDB 超表(hypertable)，并对「普通表 vs 超表」做性能对比。

> ⚠️ **转换不会替换原表**：脚本①会新建一张 `<原表名>_ts` 的超表（后缀可改），
> 把原表结构和数据拷过去，**原表原封不动保留**，方便你两张表并排对比。

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
| `migrate_data` | | `true` | 是否把原表数据拷进新超表；`false` 则只建空结构。 |
| `target` | | `<原表名>_ts` | 直接指定新超表名，覆盖默认的「原表名 + 后缀」。 |

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

# 确认无误后真正执行（原表 sensor_data 保留，新建超表 sensor_data_ts）
python3 convert_to_timescale.py my_tables.ini

# 换个后缀，比如 _hyper -> sensor_data_hyper
python3 convert_to_timescale.py my_tables.ini --suffix _hyper

# 后缀表已存在时先删后建（默认会因为表已存在而报错跳过）
python3 convert_to_timescale.py my_tables.ini --drop-existing

# 看每条执行的 SQL
python3 convert_to_timescale.py my_tables.ini -v
```

脚本对每张表依次执行：按原表结构 `CREATE TABLE <原表名>_ts (LIKE ...)` 新建空表
→ `create_hypertable` 把它变超表 →（可选）`add_dimension` 空间分区
→ `INSERT INTO <新超表> SELECT * FROM <原表>` 把原表数据**全量拷过去**
→（可选）开启压缩 →（可选）`add_compression_policy`。
**全程只读原表，绝不修改/替换原表。**
每张表一个事务，**失败自动回滚且不影响其它表**，最后汇总成功/失败数量。

**数据保证 & 校验**：只要原表有数据且 `migrate_data = true`（默认），脚本会把数据全部
拷进新超表；每张表处理完会自动 `count(*)` 对比两边行数并打印结果：

```
==> 处理表 sensor_data
    ✓ 按 sensor_data 的结构新建空表 sensor_data_ts
    ✓ 把 sensor_data_ts 变成 hypertable（分区列=ts, chunk=7 days）
    ✓ 拷贝 sensor_data 的数据到 sensor_data_ts
    ✓ 行数校验通过：原表 1,250,000 行 = 超表 1,250,000 行
```

若不一致会打印 `⚠ 行数不一致` 提示你排查（多半是 `migrate_data=false` 或拷贝期间原表又有写入）。
若只想要空的超表结构、不拷数据，把该表的 `migrate_data` 设为 `false` 即可。

> 说明：新表用 `LIKE` 拷贝列/默认值/生成列/存储参数/注释，但**不拷贝索引和约束**
> ——因为含非时间列的主键/唯一约束会让 `create_hypertable` 失败。如需对比查询性能，
> 可在新超表上自行按需建索引。

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
| `--chunk-interval STR` | `1 day` | 超表 chunk 区间（仅自建模式） |
| `--segmentby COL` | `device_id` | 压缩分段列；传 `''` 则不开压缩（也就不出压缩比，仅自建模式） |
| `--plain-table NAME` | — | 指定**已有**的普通表名（须与 `--hyper-table` 同时给出） |
| `--hyper-table NAME` | — | 指定**已有**的超表名（须与 `--plain-table` 同时给出） |
| `--out PATH` | `benchmark_report.md` | 报告输出路径 |
| `--env-file PATH` | 同目录 `db.env` | 指定连接配置文件 |
| `--keep` | 否 | 结束后保留 `bench_plain` / `bench_hyper` 两张表，否则自动 DROP（仅自建模式） |

报告内容包括：存储/压缩比、各查询的普通表 vs 超表耗时与提速倍数、（如测写入）写入吞吐。

> 测的查询覆盖：最近 1 小时范围扫描、按天聚合、按设备分组、单设备明细、时间窗口+过滤聚合。
> 数据量越大、范围查询越多，超表优势越明显；小数据集差距可能不明显。

### 2.1 指定已有的两张表（不自建、不灌数据）

如果你已经有现成的两张表想直接对比（比如脚本①转出来的 `sensor_data` 普通表和
`sensor_data_ts` 超表），用 `--plain-table` / `--hyper-table` 指定即可：

```bash
# 基于现有数据直接跑查询对比
python3 benchmark.py --plain-table sensor_data --hyper-table sensor_data_ts
```

这种**指定模式**与默认的自建模式区别：

- **不建库、不建表、不灌数据、不清理**——直接连 `db.env` 配置的库，基于现有数据跑对比。
- 启动时会校验两张表确实存在，不存在直接报错退出。
- 两个参数必须**成对**出现，只给一个会报错。
- `--rows` / `--chunk-interval` / `--segmentby` / `--keep` 在此模式下不生效，报告里相应字段标注为「指定已有超表」。
- `--mode insert`/`both` 会**向你指定的两张表插入测试数据**，运行前会有明确告警；只对比查询请用默认的 `--mode query`。

> ⚠️ 内置查询假设表里有 `ts` / `device_id` / `region` / `temperature` / `humidity` 这些列
> （与自建表同构）。指定自己的表时，列名需匹配，否则查询会报错。

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
