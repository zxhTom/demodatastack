# CDC 工具：任意 PostgreSQL 表变动监听 → Redis Stream

独立于本仓库其它服务的单文件 CLI（`cdc_tool.py` 只依赖 `psycopg2` + `redis`），
可以把整个 `db/cdc_tools/` 目录拷到任意能访问目标 PostgreSQL 的机器上运行，
用来监听**任意指定的一批表**，把变更实时推送到 Redis Stream，并能检测链路
中是否发生了数据丢失。

和本仓库 `backend/app/cdc/`（教务系统专用、写死了 9 张业务表）的关系：
这里是它的**通用化 / 可移植版本**——表集合、目标库、Redis 目标全部由配置文件
指定，不依赖 FastAPI 或本项目的数据库 schema。

## 0. 安装

```bash
cd db/cdc_tools
pip install -r requirements.txt
cp cdc.example.ini my_source.ini
vim my_source.ini   # 填目标库连接信息、要监听的表、Redis 地址
```

## 1. 功能一览（对应四个子命令）

| 子命令 | 做什么 |
|--------|--------|
| `setup` | 在目标 PostgreSQL 上准备 CDC 所需的一切：检查/设置 `wal_level`、创建 publication、`REPLICA IDENTITY FULL`、创建复制槽、可选创建专用只读复制角色 |
| `run` | 启动采集器（前台常驻进程），监听 WAL，把指定表的变更推送到 Redis Stream |
| `verify` | **核对整个链路（PG → 采集器 → Redis）是否发生数据丢失**，5 类独立检测 |
| `status` | 轻量状态查看（复制槽、心跳、Stream 长度），不做核对，可以高频调用 |
| `teardown` | 卸载：删除复制槽 / publication / Redis 状态键 |

## 2. 典型使用流程

```bash
# 1. 在目标库上准备 CDC 对象（先 dry-run 看看要做什么）
python3 cdc_tool.py -c my_source.ini setup
python3 cdc_tool.py -c my_source.ini setup --apply

# 如果提示需要重启 PostgreSQL（wal_level 不是 logical，或复制槽配额不够）：
#   重启目标库后，重新执行一次 setup --apply 完成剩余步骤

# 2. 启动采集器（生产环境建议用 systemd/supervisor 常驻，见 §5）
python3 cdc_tool.py -c my_source.ini run

# 3. 另开一个终端，核对数据完整性
python3 cdc_tool.py -c my_source.ini verify

# 4. 日常巡检（高频、轻量）
python3 cdc_tool.py -c my_source.ini status

# 5. 不再需要时清理
python3 cdc_tool.py -c my_source.ini teardown --purge-redis -y
```

## 3. `setup` 在目标库上具体做什么

1. **wal_level 检查**：不是 `logical` 时打印/执行 `ALTER SYSTEM SET wal_level='logical'`
   —— 这一步**需要重启 PostgreSQL** 才能生效，脚本会检测到并退出（exit code 2），
   提示你重启后重新运行。
2. **复制槽配额**：`max_replication_slots` / `max_wal_senders` 不够用时调大
   —— 同样需要重启。
3. **复制权限**：给配置里的连接用户加 `REPLICATION` 属性；也可以用
   `--create-role NAME --create-role-password PW` 单独建一个只读复制专用角色
   （只授予 `REPLICATION` + 对被监听表的 `SELECT`，不需要用超级用户跑常驻进程）。
4. **Publication**：`CREATE PUBLICATION` 或用 `ALTER PUBLICATION ADD/DROP TABLE`
   把已有发布同步成配置文件里的表集合（增量对齐，不会影响其它已发布的表）。
5. **REPLICA IDENTITY FULL**：对每张监听表设置，这样 UPDATE/DELETE 的 WAL
   才带得上完整旧行，`verify` 和下游消费者才能拿到 before/after 做对比。
6. **复制槽**：幂等创建（已存在则跳过）。

默认是 **dry-run**（只打印不执行），确认无误后加 `--apply` 才真正改库 ——
这是特意为「在别人的 / 生产的 postgres 上执行」设计的安全默认值。

## 4. `verify` 怎么检测数据丢失

「丢失」可能发生在链路的三个不同位置，`verify` 分别用不同手段检测，
外加一个端到端的行数交叉核对兜底：

| # | 检测点 | 原理 | 触发条件 |
|---|--------|------|----------|
| 1 | 复制槽 WAL 状态 | 查 `pg_replication_slots.wal_status` | 值为 `lost` = PG 已经把某段 WAL 物理删除但采集器还没读到，**确定丢失**，且不可恢复 |
| 2 | 采集器心跳 | 采集器每 `heartbeat_interval` 秒写一次 Redis 心跳 | 心跳超过 3 倍间隔未更新 → 采集器可能挂了（本身不代表丢数据，因为复制槽会保住 WAL，但需要关注） |
| 3 | Stream 裁剪检测 | 比较上次 verify 记录的游标位置 与 Stream 当前最旧的消息 ID | 游标位置已经被 `MAXLEN` 挤出去 → 这段时间的事件**已经丢失且无法恢复**（这是牺牲 Kafka 长期保留换轻量所付出的代价，见 [architecture.md](../../docs/architecture.md) 的方案对比） |
| 4 | 序列跳号检测 | 采集器给每条推送事件编一个 Redis `INCR` 全局递增序号，verify 检查这批序号是否连续 | 出现跳号 → 推测采集器成功从 WAL 读到了变更、但 `XADD` 到 Redis 失败（网络抖动等），**该事件丢失** |
| 5 | 行数核对 | 记录基线行数，累加期间 Stream 中 INSERT(+1)/DELETE(-1) 的净变化，与源表当前实际行数比较 | 不一致 → **端到端兜底检测**，能捕获前 4 项都没覆盖到的问题（如解析器 bug、过滤条件写错） |

```bash
python3 cdc_tool.py -c my_source.ini verify           # 用配置里 count_check_tables 做行数核对
python3 cdc_tool.py -c my_source.ini verify --full     # 对全部监听表做行数核对（大表会有 COUNT(*) 开销，谨慎）
python3 cdc_tool.py -c my_source.ini verify --json     # 机器可读输出，接入监控系统
echo $?                                                 # 0=全部通过，1=有 FAIL 项
```

行数核对是**增量式**的：第一次运行只建立基线（不报错），此后每次运行都拿
「基线 + 期间净变化」与当前实际行数比较，通过后把基线推进到当前状态；
不通过则**不推进基线**，避免下一次核对时把问题悄悄"冲平"掩盖过去。

> 行数核对存在的正常噪音：如果采集器正在运行且验证时机恰好卡在事件产生
> 和推送到 Redis 之间，会短暂出现"实际行数"与"预期"不一致——这不是丢失，
> 是时间窗口问题。建议：业务低峰期或短暂停写后再跑一次确认。

## 5. 生产环境常驻运行

用 systemd 管理 `run` 子命令，崩溃自动拉起：

```ini
# /etc/systemd/system/cdc-collector.service
[Unit]
Description=CDC collector (PG -> Redis Stream)
After=network.target

[Service]
WorkingDirectory=/opt/cdc_tools
ExecStart=/usr/bin/python3 cdc_tool.py -c my_source.ini run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now cdc-collector
journalctl -u cdc-collector -f
```

建议配合 cron 定期跑 `verify --json`，接入你现有的监控/告警系统：

```bash
*/5 * * * * cd /opt/cdc_tools && python3 cdc_tool.py -c my_source.ini verify --json >> verify.log 2>&1 || echo "CDC verify FAILED" | mail -s alert you@example.com
```

## 6. 多实例 / 多租户部署

每个要监听的 PostgreSQL 实例各准备一份配置文件（不同的 `tag`），可以指向
**同一个 Redis**、共用**同一个 Stream**（各自的 `seq_key`/`heartbeat_key`/
`checkpoint_prefix` 靠 tag 自动区分，互不干扰），下游消费者用事件里的
`source` 字段区分来自哪个实例：

```bash
python3 cdc_tool.py -c tenant_a.ini setup --apply
python3 cdc_tool.py -c tenant_b.ini setup --apply
# 分别用 systemd 常驻各自的 run
```

这与 [tenant-dashboard](https://github.com/zxhTom/tenant-dashboard) 参考项目
「每租户库一个采集线程」的思路一致，这里做成了「每租户库一个进程 + 配置文件」，
便于独立重启/独立监控。

## 7. 卸载

```bash
# 采集器仍在跑的话先停掉（systemctl stop cdc-collector）
python3 cdc_tool.py -c my_source.ini teardown --purge-redis -y
# 需要连 publication 也删掉（确认没有别的订阅在用它）：
python3 cdc_tool.py -c my_source.ini teardown --drop-publication --purge-redis -y
```

**千万不要直接删数据库/进程而不跑 teardown** —— 复制槽如果没人清理，
PostgreSQL 会一直为它保留 WAL，采集器一旦真的不打算再启动，槽必须显式
`pg_drop_replication_slot`，否则 WAL 无限堆积最终把磁盘写满（这是 CDC
方案最经典的运维事故之一，详见 [learning-guide.md](../../docs/learning-guide.md) §5）。

## 8. 常见问题

- **`publication "cdc_pub" does not exist`（run 报错）**：先跑 `setup --apply`。
- **`wal_level` 改了但还是不对**：确认真的重启了 PostgreSQL（`ALTER SYSTEM`
  只是写配置，`wal_level` 是 `PGC_POSTMASTER` 级参数，`pg_reload_conf()` 不生效）。
- **想监听的表后来改了**：改配置文件的 `tables`，重新跑一次 `setup --apply`
  会自动做增量的 `ADD TABLE`/`DROP TABLE`；不需要删掉复制槽重建。
- **换了 Redis 实例**：新 Redis 上没有历史 `seq`/心跳/checkpoint 状态，
  相当于全部重新建立基线，`verify` 的行数核对第一次会是"首次核对"而非报错。
