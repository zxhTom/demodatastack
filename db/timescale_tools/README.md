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
| `find_leftover_tables.py` | 扫描历次转换留下的临时/备份表，生成待审查清理 SQL，见 §10 |
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

配了 `pk_columns` 后，`to-hyper` 会跳过原主键的定义（列结构不同，没法照抄），
改成按 `pk_columns` 建一个包含分区列的复合主键——但**沿用原主键的名字**（细节见
§11.2），然后才调用 `create_hypertable`。`status` 命令也会对每张**普通表**做同样的
诊断，配置不对会在转换前就提示你：

```
✗ 当前主键 (comm_log_id) 不含分区列 start_time，需要在 tables.ini 给这张表加
  pk_columns = ...（须包含 start_time）
```

`to-plain`（转回普通表）不需要关心这个——超表上已经是 `pk_columns` 重建好的
复合主键，转回普通表会原样保留（不会恢复成最初的单列主键，但主键名字不变、
业务上依然是唯一约束，不影响使用）。

#### 独立唯一索引缺分区列——`drop_indexes`（d_alarm_event 的坑）

`pk_columns` 只管**主键**。TimescaleDB 的要求是**每一个唯一索引/约束**都要含分区列，
所以如果表上还有**独立的唯一索引**不含分区列，即使配了 `pk_columns` 也会报：

```
cannot create a unique index without the column "alarm_time" (used in partitioning)
```

`d_alarm_event` 就是这样：主键 `pk_d_alarm_event(content_id)` 之外，还有个独立唯一索引
`idx_pk_d_alarm_event(content_id)`——它和主键列完全一样，是**冗余重复**的（主键已保证
`content_id` 唯一）。这种索引没法带进超表，用 `drop_indexes` 让转换时**不重建它**：

```ini
[d_alarm_event]
time_column  = alarm_time
pk_columns   = content_id, alarm_time    # 主键补上分区列
drop_indexes = idx_pk_d_alarm_event      # 冗余唯一索引，转换后不重建（原表备份 _pg_old 仍保留）
```

`drop_indexes` 只是"转换后不重建"，原表作为 `_pg_old` 备份仍带着这些索引，不会真丢。
多个索引用逗号分隔。转换报这个错时脚本会打印明确指引，告诉你该配 `pk_columns` 还是
`drop_indexes`。（全库扫描确认过：23 张目标表里只有 `d_alarm_event` 有这个问题。）

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
python3 table_convert.py to-hyper --tables a --drop-backup -y   # 撞上上一轮遗留的 _pg_old 时，删旧备份后继续（见 §9.8）

# 超表 -> 普通表
python3 table_convert.py to-plain --dry-run
python3 table_convert.py to-plain --group curve -y
python3 table_convert.py to-plain --tables sys_fep_comm_log -y

# 造数据（转发给 db/meter_seed 下的现成脚本，--module 后面的参数原样透传）
python3 table_convert.py seed --module curve --start 2025-01-01 --days 7
python3 table_convert.py seed --module event --start 2025-01-01 --end 2025-01-31
python3 table_convert.py seed --module log --start 2025-01-01 --days 7 --count-per-day 5000
python3 table_convert.py seed --module curve --start 2025-01-01 --days 7 --dry-run   # 先看生成多少行
```

`--group` 取值 `event`/`curve`/`log`/`all`（默认 `all`），`--tables` 优先于 `--group`。
`-y`/`--yes` 跳过确认提示，用于脚本化调用；不加则每次都会先列出目标表清单要求确认。

`seed` 的 `--module` 支持 `curve`（12 张曲线表，转发给 `seed_meter_data.py`）、
`event`（10 张事件表，转发给 `seed_event_data.py`）、`log`（`sys_fep_comm_log`，转发给
`seed_log_data.py`，按抽样统计的真实生产分布造：Load Profile 占绝大多数且几乎全部
因 `UNIT_OFFLINE[2]` 失败，`Heart Beat`/`Push Object List` 几乎必定成功等，具体权重
见脚本内 `_TEMPLATES`）。`--module` 之后的所有参数原样转发给对应脚本，用法与直接调用
`seed_meter_data.py`/`seed_event_data.py`/`seed_log_data.py` 完全一致
（见 `db/meter_seed/README.md`）。
**注意**：没有显式传 `--env-file` 时用的是被转发脚本自己目录下的
`db/meter_seed/db.env`（即真实生产库配置）；如果显式传了 `--env-file`/`--sql-out`
等相对路径，按你**当前所在目录**解析，不是按 `db/meter_seed/` 解析。

### 9.5 已验证过的场景

以下场景已经在本机 Postgres 上用真实建表+写数据+转换+校验跑通过（用的是模拟
生产场景的临时测试表，非真实业务表；结果供你判断代码是否可信，不代表在你的
真实数据上跑过）：

- 普通表 → 超表：`pk_columns` 自动重建主键成功，行数与内容校验（`sum(device_id)`
  校验和）转换前后完全一致
- 超表 → 普通表：完整往返（to-hyper 再 to-plain）后行数与校验和依然完全一致
- 幂等跳过：重复执行 `to-hyper` 会正确识别"已是超表"并跳过，不会重复处理
- `status` 命令能在转换前正确诊断出"主键不含分区列"的表，并给出具体需要加什么配置
- `--dry-run` 会用真实只读连接做状态诊断（能显示会跳过还是会报错），不是盲目打印
  一遍固定的 SQL 模板
- **serial 列的序列归属**：转换前后序列正确改挂到新表，不会因为备份表将来被删
  而级联带走序列（这是实测中发现并修复的真实 bug——序列改挂的 SQL 曾经因为漏了
  一次 `conn.commit()` 而在连接关闭时被静默回滚，现已修复并验证）
- `--redo`（两个方向）：DROP 当前表前会先摘掉序列的 OWNED BY 归属，重命名完成后
  再挂回去，序列不会被级联删除，自增值也不会跳变错乱（实测 `nextval()` 在 redo
  前后连续无冲突）
- **真实信号中断**：用 `kill -TERM` 在 3 张表批量转换到一半时发送信号，验证到
  ①当前正在处理的这张表会完整跑完（不会砍在事务中间），②跑完立刻停止、不再
  开始下一张，③退出码 130，④进度文件正确记录 `run_interrupted`，⑤重新执行
  同样命令后正确跳过已完成的表、从断点继续，⑥全部转完后行数完全一致
- 新增表：往 `tables.ini` 加一个新的 `[section]`，不改任何代码，`status`/
  `to-hyper` 立刻就能识别并处理

### 9.6 可以放心随时中断（Ctrl+C / kill）

**第一次** Ctrl+C（或 `kill -TERM`）：不会立刻杀掉进程，而是等**当前正在处理的
这一张表**跑完（提交或回滚）才停，绝不会砍在一张表的事务中间。终端会打印：

```
[!] 收到 SIGINT：当前这张表处理完（提交或回滚）后就停，不会停在表中间。
再按一次 Ctrl+C 立即强制退出。
```

**第二次** Ctrl+C：立即强制退出。这也是安全的——如果当前表还没提交，进程一断开，
PostgreSQL 会自动把这张表未提交的事务回滚掉，不会留下写了一半的数据；下次重跑，
`to_hyper_one`/`to_plain_one` 开头都会先 `DROP TABLE IF EXISTS` 清理残留的
`_ts_new`/`_plain_new` 临时表，不需要你手动善后。

用 `kill -9`（SIGKILL，程序完全来不及反应）中断也是安全的，原理相同——只是
不会有"等当前表跑完"的礼貌等待，直接在事务层面回滚。

### 9.7 断点续传 / 完全重新跑

**默认行为就是断点续传**，不需要额外加参数：脚本每次处理一张表前，都会先查
数据库里这张表**现在实际是什么状态**，已经转换成功的表会打印 `[跳过]` 直接
跳过，不会重复处理。所以你中断后，重新执行**一模一样的命令**就会自动跳到还
没做完的表继续：

```bash
python3 table_convert.py to-hyper --group event -y     # 假设跑到 d_communication_event_log 时中断了
python3 table_convert.py to-hyper --group event -y     # 重新执行同一条命令，前面已完成的自动跳过，从这张继续
```

这个"续传"不是靠一个进度文件里记的状态（那种方式容易和数据库实际状态对不上），
而是每次都直接问数据库"你现在到底是什么状态"，所以哪怕你中途手动改过某张表、
或者进度文件丢了，续传结果也始终和数据库的真实状态一致。

**想完全重新跑**（哪怕某张表已经转换成功了，也要重新转一遍）用 `--redo`：

```bash
python3 table_convert.py to-hyper --group event --redo -y   # 从 _pg_old 备份重新迁移
python3 table_convert.py to-plain --group event --redo -y   # 从 _ts_old 备份重新转换
```

⚠️ **`--redo` 会丢弃数据**：它是"从备份那一刻的冻结快照重新来一遍"，如果备份
之后你又往当前表里写过新数据，这些新数据会在 redo 后消失（回到备份那一刻的
样子）。执行前会有明确的确认提示（`-y` 也不会跳过这条警告文字，只是跳过要不要
继续的交互确认），用之前确认清楚这真的是你要的。

### 9.8 来回转换撞上旧备份名怎么办——`--drop-backup`

`_pg_old`（to-hyper 的备份名）和 `_ts_old`（to-plain 的备份名）是跟"转换方向"
绑定的固定名字，不是跟"第几次转换"绑定的。如果同一张表在 plain↔hyper 之间来回
转过一圈，两个方向各自的备份名都已经被占用过一次——这时候再往同一个方向转第二
圈，就会撞上自己上一轮留下的备份，报错停止：

```
[错误]  a: 备份名 a_pg_old 已被占用，请先处理（重命名或 DROP）后重试，或加 --drop-backup 让脚本自动删除旧备份后继续
```

默认不会替你决定"这份旧备份还要不要"——毕竟这通常意味着这张表之前转换过、
备份还没来得及清理，脚本不会猜你的意图去覆盖。想让脚本自动处理，加 `--drop-backup`：

```bash
python3 table_convert.py to-hyper --tables a --drop-backup -y   # 撞名就先 DROP a_pg_old 再转换
python3 table_convert.py to-plain --tables a --drop-backup -y   # 撞名就先 DROP a_ts_old 再转换
```

⚠️ **`--drop-backup` 会删除数据**：被删掉的旧备份代表的是"上一轮转换之前"那一刻
的冻结快照，删掉后不可恢复。跟 `--redo` 一样，执行前会有明确的确认提示（`-y` 只
跳过交互确认，不会跳过这条警告文字）。没撞上备份名的表不受影响，`--drop-backup`
只在真的撞名时才会触发 DROP。

这个 DROP 和后面的建表/迁移在**同一个事务**里：如果本次转换后面的步骤失败（比如
行数校验不通过），整个事务回滚，这次 DROP 也会跟着回滚——旧备份会原样恢复，不会
出现"备份被删了，但新表没建成功"这种两头空的情况。

### 9.9 执行进度 & 后台运行

**前台跑**：每张表开始时会打印 `[N/总数]` 进度：

```
──────────────────────────────────────────────────────────────
[3/10] d_disconnector_event_log
  [MIGRATE] ...
```

**想看结构化进度**（方便脚本/监控系统读取），加 `--progress-file`，会把每张表
处理完的结果追加写成一行 JSON：

```bash
python3 table_convert.py to-hyper --group event -y --progress-file progress.jsonl
tail -f progress.jsonl
# {"event": "table_done", "idx": 3, "total": 10, "table": "d_disconnector_event_log", "result": "ok", "ts": "..."}
# 全部跑完或被中断时还会各写一条 run_complete / run_interrupted 汇总
```

**后台运行**：用 `nohup` 丢后台，标准输出重定向到日志文件即可，不需要任何
特殊参数（已经做了行缓冲，重定向到文件也能 `tail -f` 实时看到）：

```bash
nohup python3 table_convert.py to-hyper --group event -y \
    --progress-file progress.jsonl > convert.log 2>&1 &
disown          # 可选：脱离当前 shell，关终端也不会被挂掉

tail -f convert.log          # 看详细执行日志
tail -f progress.jsonl       # 看结构化进度
```

**最简单的进度检查方式**：不需要进度文件，直接另开一个终端跑 `status`——它
直接查数据库当前状态，是最准确、最实时的进度视图：

```bash
python3 table_convert.py status --group event
```

停掉后台任务：`kill -TERM <pid>`（等当前表跑完再停，见 §9.6），或者
`kill -9 <pid>` 立即强制停止，都是安全的。

---

## 10. 清理历次转换留下的临时表 / 备份表

多次转换（尤其是试验、中断重跑、`--redo`）会积累不少 `_ts_new`/`_pg_old`/
`_ts_old`/`_plain_new` 这类表。`find_leftover_tables.py` 是一个**只读扫描脚本**，
会按命名规律找出这些表并生成一份**默认全部注释掉**的清理 SQL——不会自动删除
任何东西，你自己看过、确认要删的表把对应行的 `--` 去掉再手动执行。

```bash
python3 find_leftover_tables.py                          # 用同目录 db.env
python3 find_leftover_tables.py --env-file prod.env --out cleanup_candidates.sql
```

输出分四类，风险从低到高：

| 类别 | 来源 | 风险 |
| --- | --- | --- |
| ① `_ts_new`/`_plain_new` | 转换中途失败/被中断后没清理的残留临时表 | 低——正常流程这类表建完马上被消费掉，能看到说明某次跑到一半没善后，下次重跑会自动 `DROP IF EXISTS` 重建，留着也无害 |
| ② `_pg_old`/`_ts_old` | 转换成功后的原表改名备份 | 中——删之前自己核对一下现表的行数/关键数据 |
| ③ `_bak_YYYYMMDD` | `transtable.py`（更早的迁移脚本）留下的整表快照备份 | 中——是某个时间点的完整快照，删前确认不再需要回溯 |
| ④ `_ts` 结尾 | 可能是旧版 `convert_to_timescale.py`（新建对比模式）生成的并排超表，**也可能只是恰好这么命名的正常表，跟任何转换脚本都没关系** | **高，命名规律很宽泛**——务必逐条确认再删，不要整段无脑跑 |

生成的 SQL 文件里，每一类都有对应说明和风险提示，且每条 `DROP TABLE IF EXISTS`
后面都注明了约多少行、多大、对应哪张现表——方便你判断这张表现在是否还需要。

> ④ 类已经实测出过一次误判：拿本仓库自带的 demodatastack 演示库（`edumanage`）
> 测试这个扫描脚本时，它把 `system_logs_ts` 也列进了候选清单——但那张表其实是
> `docker/postgres/init/04_timeseries.sql` 建库脚本直接建的固定超表，专门给
> `benchmark.py` 做"普通表 vs 超表"性能对比用的，从来没经过任何转换脚本，
> 是这个项目的核心 schema 的一部分，删了会破坏项目自带的基准测试功能。
> 这不是针对你的 `eco_ma` 库的结论（那边根本没有这张表），只是提醒你：
> ④ 类命中的表，务必自己确认一下这张表的真实来历，别看到 `_ts` 结尾就当备份删。

---

## 11. 转换前后索引名保证一致

### 11.1 为什么需要专门处理

PostgreSQL 的 `CREATE TABLE tmp (LIKE src INCLUDING INDEXES)`（以及连带 `INCLUDING
CONSTRAINTS` 建出来的 PK/UNIQUE 约束）**不会保留原索引名**，会按新表名重新生成一套
默认命名。比如原表 `idx_alarm_event_device` 这个索引，转换后会变成类似
`d_alarm_event_ts_new_device_id_idx` 这种自动生成的名字——哪怕两张表的列结构、
数据完全一样，索引名也对不上了。这是 PostgreSQL 的标准行为，不是这个工具引入的，
但转换脚本如果直接用 `LIKE ... INCLUDING ALL`（`table_convert.py` 早期版本确实
这么写的）就会中招。（实测验证过：`CHECK` 约束不受影响，会正确保留原名，只有
独立索引和 PK/UNIQUE 约束会被改名。）

### 11.2 现在是怎么保证的

`table_convert.py` **不再用** `LIKE ... INCLUDING INDEXES/CONSTRAINTS`，改成：

1. 建新表时只拷列结构（`INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING STORAGE
   INCLUDING COMMENTS`），不含索引和约束
2. `create_hypertable` 传 `create_default_indexes => false`，不让 TimescaleDB
   自动加一个原表没有的时间列索引
3. 转换前先读出原表所有独立索引和 PK/UNIQUE 约束的**完整定义**
   （`pg_get_indexdef`/`pg_get_constraintdef`，唯一性、部分索引的 `WHERE` 条件、
   表达式索引都完整保留）
4. 原表改名成备份表后，**先把备份表上那些同名的索引/约束也改名让路**（改名不会
   影响它们的功能，只是空出名字），再在新表上用原名重建
5. `pk_columns` 强制重建主键的情况：新主键的列结构必然和原来不同（多了分区列），
   没法照抄原定义，但**沿用原主键的名字**

结果是：只要不用 `--redo`，转换前后**索引名、约束名逐一对应，一个不多一个不少**
（`create_default_indexes => false` 保证了这一点——没有 §11.1 那种"多出一个原表
没有的索引"的情况）。

### 11.3 `--redo` 的名字是从哪来的

`--redo` 是从冻结的备份表（`_pg_old`/`_ts_old`）重新构建，而备份表上的索引/约束
在它成为备份的那一刻已经被改名让路过（比如 `pk_idx_conv_custom` 会变成
`pk_idx_conv_custom_pgold`）。如果直接照抄备份表**此刻**的名字，会：既对不上
用户原本的真实名字，又会跟备份表自己身上还留着的同名对象在连接层面撞名——这两个
问题都是实测跑出来的真实 bug，不是理论推测。

修复方式：从备份表读取定义时，会按照当初改名用的后缀（`_pgold`/`_tsold`）把名字
**还原**回真实原名，再重建。这只对**用本工具当前版本**转换产生的备份表有效——
如果某张表是用更早版本的 `table_convert.py`、或者 `convert_to_timescale.py`/
`timescale_migrate_event.py`/`transtable.py` 这些旧脚本转换的，它的备份表上索引名
本来就已经是自动生成的乱码名字（§11.1 那种），不含"真实原名"这个信息，重新读出来
再重建也没法凭空恢复出用户最初取的名字——这种情况下 `--redo` 会照抄备份表上现有
的（可能已经是乱码的）名字，而不是报错，请知悉这个限制。

**你的 `eco_ma` 库还没有用本工具转换过任何真实表**，所以从现在开始转换，索引名
保证问题从一开始就不会发生。

### 11.4 已验证的场景

用一张同时有自定义命名的 PK、普通索引、唯一索引、部分索引（带 `WHERE` 条件）的
测试表，验证过以下全部场景转换前后索引名/定义完全一致：

- `to-hyper`（普通 → 超表）：4 个索引/约束名字、唯一性、`WHERE` 条件全部一致，
  且没有 TimescaleDB 自动加的多余索引
- `to-plain`（超表 → 普通表）：同上
- `to-hyper --redo`：从 `_pg_old` 重新迁移，正确还原出真实原名（不是备份表上
  临时改过的后缀名）
- `to-plain --redo`：从 `_ts_old` 重新转换，同上
- 同一张表反复 `to-hyper` → `to-plain` 来回转换、两个备份表同时存在的情况下，
  改名让路不再互相撞名（这也是实测跑出来才发现并修的 bug：早期版本两个方向用
  同一个改名后缀，反复转换会导致新一轮转换在改名让路这一步失败，事务会正确回滚、
  不丢数据，但会中断）
