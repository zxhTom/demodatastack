# meter_seed

为 `c_meter` 中已有的电能表批量生成 12 张 `d_*` 负荷/抄表曲线表的整天数据，时间粒度固定为 15 分钟（每天 96 点）。

涉及的表：

```
d_load_voltage  d_load_current  d_load_power  d_load_power_r
d_load_power_v  d_load_instant  d_load_angle  d_load_status
d_read_curve    d_read_curve_r  d_read_curve_v d_demand_curve
```

各表中业务含义明确的字段（电压/电流/功率/功率因数/累计电量寄存器等）按物理关系生成；`d_read_curve*` 系列的累计电量寄存器会按尖峰平谷分时累加。`mp_id` 取 `meter_id`（这批表没有 `c_meter_mp_rela` 映射记录）。

## 准备

```bash
cd db/meter_seed
pip install psycopg2-binary
cp db.env.example db.env   # 填入真实数据库连接信息，db.env 已加入 .gitignore
```

`db.env` 中的连接信息加载优先级：环境变量 > `db.env` > 内置默认值（见 `dbconfig.py`）。也可以只填一行 `DB_DSN` 来覆盖拆分字段。

## 用法

```bash
python seed_meter_data.py --start 2025-01-01 --end 2025-01-31
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--start` / `--end` / `--days` | 日期范围，三者任选两个组合（如 `--start` + `--days`） |
| `--mode` | `fill`（默认，已存在的行跳过不覆盖）/ `overwrite`（存在则更新）/ `rebuild`（先删除范围内数据再重新生成，需配合 `--yes`） |
| `--meters` | 逗号分隔的 `meter_id` 列表，默认 `c_meter` 全部电表 |
| `--tables` | 逗号分隔的表名，默认上面列出的全部 12 张 |
| `--profile-id` | 写入各表的 `profile_id`，默认 `1` |
| `--batch-size` | 批量写入的行数，默认 `2000` |
| `--env-file` | 自定义 `db.env` 路径，默认脚本同目录下的 `db.env` |
| `--dry-run` | 只打印将要生成的行数，不连库写入 |
| `--sql-out` | 把生成的 INSERT/DELETE 语句写入指定文件，不直接执行写入（仍会连库读取 `c_meter`/累计电量基线，用于保证生成数据正确） |
| `--yes` | `rebuild` 模式下确认会先删除该时间范围内的现有数据（使用 `--sql-out` 时不需要，因为不会立即执行删除） |

示例：

```bash
# 先看一下会生成多少行，不实际写库
python seed_meter_data.py --start 2025-01-01 --days 7 --dry-run

# 只给指定电表补数据，缺的补上，已有的不动
python seed_meter_data.py --meters 1001,1002 --start 2025-01-01 --end 2025-01-07

# 重建某个时间范围内某几张表的数据（会先删除旧数据，需要 --yes 确认）
python seed_meter_data.py --tables d_load_power,d_load_voltage \
    --start 2025-02-01 --end 2025-02-28 --mode rebuild --yes

# 不直接写库，把要执行的 SQL 输出到文件，自己检查后再手动执行
python seed_meter_data.py --start 2025-01-01 --days 7 --sql-out seed.sql
```

## 累计电量基线

`d_read_curve` / `d_read_curve_r` / `d_read_curve_v` 三张表里的累计寄存器（`*_r` 字段）在生成起始日期之前会先从库里已有的历史数据中读取最后一条记录作为基线续接，保证累计值不会从 0 重新跳变（`rebuild`/`fill` 模式都适用）。
