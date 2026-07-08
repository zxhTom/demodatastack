# TimescaleDB 工具集

## 现在请用 `table_convert.py`（合并版，推荐入口）

普通表 <-> TimescaleDB 超表**双向**转换 + 造数据，合并自下面三个旧脚本：

```bash
python3 table_convert.py status                          # 先看每张表当前状态，不改任何东西
python3 table_convert.py to-hyper --group event -y        # 普通表 -> 超表
python3 table_convert.py to-plain --group event -y        # 超表 -> 普通表
python3 table_convert.py seed --module curve --start 2025-01-01 --days 7   # 造数据
```

两个方向都不会丢数据（改名备份 + 行数校验，详见脚本内注释），共用同一份
`tables.ini` 清单，不用再分别维护两套配置。完整用法见下面 **§9 table_convert.py 使用说明**。

> `convert_to_timescale.py` / `timescale_migrate_event.py` / `timescale_revert_event.py`
> 三个脚本已被 `table_convert.py` 取代，仅保留在本目录供参考旧实现，**新的转换操作
> 请一律用 `table_convert.py`**。以下 §1~§2 描述的是旧脚本①（`convert_to_timescale.py`），
> 已不建议使用。

目录下文件：

| 文件 | 作用 |
| --- | --- |
| `table_convert.py` | **【推荐】** 双向转换 + 造数据入口，见 §9 |
| `tables.ini` | `table_convert.py` 用的表清单（23 张：事件10+曲线12+日志1） |
| `db.env.example` | 数据库连接配置模板，复制成 `db.env` 改成你自己的 |
| `dbconfig.py` | 共享的连接配置加载器 |
| `benchmark.py` | 普通表 vs 超表 性能对比，输出 Markdown 报告（独立于转换，继续可用） |
| `convert_to_timescale.py` | ⚠️ 已废弃，被 `table_convert.py to-hyper` 取代 |
| `timescale_migrate_event.py` | ⚠️ 已废弃，被 `table_convert.py to-hyper` 取代 |
| `timescale_revert_event.py` | ⚠️ 已废弃，被 `table_convert.py to-plain` 取代 |
| `tables.example.ini` / `tables_log.ini` / `tables_meter_curve.ini` | ⚠️ 旧脚本的清单，已被 `tables.ini` 合并取代 |

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

脚本有两种模式，用 `--new-table` 控制：

| `--new-table` | 行为 | 原表 |
| --- | --- | --- |
| `yes`（默认） | 新建一张 `<原表名>_ts` 超表，拷贝原表结构和数据 | **保留不变**，可两表并排对比 |
| `no` | **原地把原表本身转成超表**（数据随之迁移） | 被替换；**替换前自动把原表建表 SQL 备份到 `backups/`** |

```bash
# 先 dry-run 看看会执行哪些 SQL（不连库、不改数据）
python3 convert_to_timescale.py tables.example.ini --dry-run

# 【默认 / 新建模式】真正执行（原表 sensor_data 保留，新建超表 sensor_data_ts）
python3 convert_to_timescale.py my_tables.ini

# 换个后缀，比如 _hyper -> sensor_data_hyper
python3 convert_to_timescale.py my_tables.ini --suffix _hyper

# 后缀表已存在时先删后建（默认会因为表已存在而报错跳过）
python3 convert_to_timescale.py my_tables.ini --drop-existing

# 【替换模式】原地把原表转成超表，替换前先把原表建表 SQL 备份到 backups/
python3 convert_to_timescale.py my_tables.ini --new-table no

# 替换模式自定义备份目录
python3 convert_to_timescale.py my_tables.ini --new-table no --backup-dir /path/to/backups

# 看每条执行的 SQL
python3 convert_to_timescale.py my_tables.ini -v
```

> 🛡️ **替换模式的安全网**：`--new-table no` 在替换每张表之前，会先从系统目录重建该表的
> 建表 SQL（列定义 + 约束 + 索引），写到 `backups/<表名>_<时间戳>.sql`（默认目录为项目根
> 的 `backups/`，可用 `--backup-dir` 改）。**备份失败则跳过该表、不做替换**。原地转换走
> `create_hypertable(..., migrate_data => true)`，若原表存在不含时间列的主键/唯一约束会失败，
> 此时该表事务回滚、保持原样（备份文件仍保留）。

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

---

## 9. table_convert.py 使用说明（推荐入口）

### 9.1 数据不丢失是怎么保证的

两个方向都是「新建一张目标结构的表 → 全量复制数据 → 行数校验 → 改名互换」，
原表**改名**为备份表而不是删除，任何一步失败都整体回滚、原表保持原样：

| 方向 | 命令 | 备份表名 |
| --- | --- | --- |
| 普通表 → 超表 | `to-hyper` | `<表>_pg_old` |
| 超表 → 普通表 | `to-plain` | `<表>_ts_old` |

行数校验：复制完数据后比较 `count(源)` 与 `count(新表)`，不一致直接回滚、不改名，
原表**完全不动**。校验通过才会做改名。确认新数据没问题后，备份表需要**手动**
`DROP TABLE` 清理，脚本不会自动删除任何备份。

### 9.2 关于「跳过」——之前 revert 3 张表被跳过是怎么回事

`to-plain`（原 `timescale_revert_event.py`）只会转换**当前确实是超表**的表。
如果某张表配置里写了要转，但它此刻本来就是普通表，脚本会跳过并打印原因，例如：

```
[跳过]  d_alarm_event: 当前是普通表，没有需要转换的超表状态
        （如果它本应是超表，说明它从未被成功转换过——用 status 命令确认，
        需要的话用 to-hyper 转换）
```

这不是 bug——**跳过是因为这张表压根就没有"超表状态"可以转回去**。回顾代码线索，
`db/meter_seed/transtable.py`（更早的一版迁移脚本）里实际处理的 8 张表清单是
`sys_fep_comm_log` + 7 张 `*_event_log` 表，唯独少了 `d_alarm_event`、
`d_recharge_event_log`、`d_special_event_log` 这 3 张——刚好和"revert 时跳过 3 张"对上。
最大可能是：当初做迁移用的就是 `transtable.py`，而它的表清单本来就没包含这 3 张，
所以它们从未被转换成超表，`to-plain` 检测到"现在就是普通表"于是正确地跳过。

用新的 `status` 子命令可以在做任何操作**之前**先确认这一点，不用等到跑了才发现：

```bash
python3 table_convert.py status --group event
```

会看到这 3 张表状态是"普通表"，其余 7 张是"超表"——一目了然，不需要再靠事后的
跳过日志去猜。

### 9.3 关于主键（PK）与分区列——sys_fep_comm_log 的坑

TimescaleDB 要求：**如果表上有主键/唯一约束，分区列必须包含在这个约束里**，否则
`create_hypertable` 会报错。`sys_fep_comm_log` 原本的主键是单独一列 `comm_log_id`，
不含时间列 `start_time`，直接转换会失败——这也是 `transtable.py` 里专门写了
`pk_drop`/`pk_add` 特殊处理这张表的原因。

`table_convert.py` 把这个处理泛化成了通用配置项 `pk_columns`，在 `tables.ini` 里：

```ini
[sys_fep_comm_log]
time_column = start_time
pk_columns  = comm_log_id, start_time
```

配了 `pk_columns` 后，`to-hyper` 会自动：新表建好后先删掉从原表继承来的主键约束，
再按 `pk_columns` 重建一个包含分区列的复合主键，然后才调用 `create_hypertable`。
`status` 命令也会对每张**普通表**做同样的诊断，配置不对会在转换前就提示你：

```
✗ 当前主键 (comm_log_id) 不含分区列 start_time，需要在 tables.ini 给这张表加
  pk_columns = ...（须包含 start_time）
```

`to-plain`（转回普通表）不需要关心这个——它直接 `LIKE` 现有超表的结构，超表上
已经是重建好的复合主键，转回普通表会原样保留（不会恢复成最初的单列主键，但
业务上依然是唯一约束，不影响使用）。

### 9.4 常用命令

```bash
# 查看状态（任何操作前先看这个）
python3 table_convert.py status                    # 全部 23 张表
python3 table_convert.py status --group curve       # 只看曲线表
python3 table_convert.py status --tables d_alarm_event,sys_fep_comm_log

# 普通表 -> 超表
python3 table_convert.py to-hyper --dry-run                # 先看会执行哪些 SQL
python3 table_convert.py to-hyper --group event -y          # 转事件表分组
python3 table_convert.py to-hyper --tables d_alarm_event -y
python3 table_convert.py to-hyper --group event --redo -y   # 从 _pg_old 备份重新迁移（比如改了 pk_columns 后重试）

# 超表 -> 普通表
python3 table_convert.py to-plain --dry-run
python3 table_convert.py to-plain --group curve -y
python3 table_convert.py to-plain --tables sys_fep_comm_log -y

# 造数据（转发给 db/meter_seed 下的现成脚本，--module 后面的参数原样透传）
python3 table_convert.py seed --module curve --start 2025-01-01 --days 7
python3 table_convert.py seed --module event --start 2025-01-01 --end 2025-01-31
python3 table_convert.py seed --module curve --start 2025-01-01 --days 7 --dry-run   # 先看生成多少行
```

`--group` 取值 `event`/`curve`/`log`/`all`（默认 `all`），`--tables` 优先于 `--group`。
`-y`/`--yes` 跳过确认提示，用于脚本化调用；不加则每次都会先列出目标表清单要求确认。

`seed` 的 `--module` 目前支持 `curve`（12 张曲线表，转发给 `seed_meter_data.py`）和
`event`（10 张事件表，转发给 `seed_event_data.py`）；`log`（`sys_fep_comm_log`）暂时
没有现成的造数据脚本。`--module` 之后的所有参数原样转发给对应脚本，用法与直接调用
`seed_meter_data.py`/`seed_event_data.py` 完全一致（见 `db/meter_seed/README.md`）。
**注意**：没有显式传 `--env-file` 时用的是被转发脚本自己目录下的
`db/meter_seed/db.env`（即真实生产库配置）；如果显式传了 `--env-file`/`--sql-out`
等相对路径，按你**当前所在目录**解析，不是按 `db/meter_seed/` 解析。

### 9.5 已验证过的场景

以下场景已经在本机 Postgres 上用真实建表+写数据+转换+校验跑通过（用的是模拟
`sys_fep_comm_log` 单列主键场景的临时测试表，非生产表）：

- 普通表 → 超表：`pk_columns` 自动重建主键成功，行数与内容校验（`sum(device_id)`
  校验和）转换前后完全一致
- 超表 → 普通表：完整往返（to-hyper 再 to-plain）后行数与校验和依然完全一致
- 幂等跳过：重复执行 `to-hyper` 会正确识别"已是超表"并跳过，不会重复处理
- `status` 命令能在转换前正确诊断出"主键不含分区列"的表，并给出具体需要加什么配置
- `--dry-run` 现在会用真实只读连接做状态诊断（能显示会跳过还是会报错），而不是
  盲目打印一遍固定的 SQL 模板
