# CDC 对接手册

本手册分两部分：

- **第一部分：服务端操作手册** —— 给负责运维「源 PostgreSQL + 采集器」的团队。
- **第二部分：客户端对接说明** —— 给需要消费变更事件的下游团队（业务系统读取
  Redis Stream 即可，不需要接触 PostgreSQL）。

两边团队可以是同一批人，也可以是完全不认识的两拨人——这份文档就是为后一种
情况写的，两部分各自独立可读，不要求读者了解对方那部分的实现细节。

工具本体：`db/cdc_tools/cdc_tool.py`（单文件，只依赖 `psycopg2` + `redis`）。
命令的完整参数说明见同目录 [README.md](README.md)，本文档只讲"怎么把这套东西
用起来交付给别人"。

---

# 第一部分：服务端操作手册

## 1. 你要做的事情，一句话概括

在一台能同时访问「目标 PostgreSQL」和「Redis」的机器/容器上，跑一个常驻进程，
它会把指定表的每一次 INSERT/UPDATE/DELETE 实时推送成一条 Redis Stream 消息，
供下游按需消费。你不需要碰下游代码，下游也不需要碰你的 PostgreSQL。

```
[目标 PostgreSQL]  --WAL 逻辑复制-->  [采集器进程 cdc_tool.py run]  --XADD-->  [Redis Stream]  <--下游消费--  [客户端]
```

## 2. 环境要求

| 项目 | 要求 |
|------|------|
| PostgreSQL | ≥ 10（需要 `pgoutput` 插件，PG10+ 内置） |
| Python | ≥ 3.8 |
| 网络 | 采集器所在机器要能连通目标 PG 的 5432 端口 和 Redis 端口 |
| 权限 | 见 §5，最低要求：目标库有一个具备 `REPLICATION` 属性的账号 |
| 目标库能否重启 | 如果 `wal_level` 还不是 `logical`，需要重启一次 PostgreSQL 才能生效。提前确认好维护窗口 |

## 3. 安装

```bash
# 把整个目录拷到运行采集器的机器上（不需要拷本仓库其它部分）
scp -r db/cdc_tools/ user@collector-host:/opt/cdc_tools
ssh user@collector-host
cd /opt/cdc_tools
pip install -r requirements.txt
```

## 4. 配置

```bash
cp cdc.example.ini prod.ini
chmod 600 prod.ini          # 里面有明文密码，权限收紧
vim prod.ini
```

必填项（其余字段配置模板里都有默认值和注释，一般不用改）：

| 字段 | 位置 | 说明 |
|------|------|------|
| `host/port/dbname/user/password` | `[source]` | 目标 PostgreSQL 连接信息 |
| `tag` | `[source]` | 这套采集任务的标识，会写进每条事件的 `source` 字段——**这是你要给下游团队的关键信息之一**，见 §8 |
| `tables` | `[cdc]` | 要监听的表，`schema.table` 逗号分隔 |
| `url` | `[redis]` | Redis 连接串（下游团队消费用的也是这个 Redis，但通常不会给他们 URL 里带密码，见 §7） |
| `stream_key` | `[redis]` | Stream 的 key 名——**另一项要给下游团队的关键信息** |

## 5. 部署步骤

```bash
# 1. 先 dry-run，看看要在目标库做哪些改动（不会真的执行）
python3 cdc_tool.py -c prod.ini setup

# 2a. 如果输出提示"需要重启"（wal_level 不是 logical，或复制槽配额不够）：
#     和 DBA 协调维护窗口重启 PostgreSQL，重启后重新执行第 1 步确认
#     wal_level = logical，再继续下一步

# 2b. 确认无误后正式执行
python3 cdc_tool.py -c prod.ini setup --apply

# 3. 前台试跑，确认能正常连上并开始监听
python3 cdc_tool.py -c prod.ini run
# 看到 "复制已启动: slot=... publication=..." 说明成功，Ctrl+C 停止

# 4. 生产环境用 systemd 常驻（示例见 README.md §5），启动后：
systemctl enable --now cdc-collector
journalctl -u cdc-collector -f

# 5. 跑一次核对，确认链路没问题
python3 cdc_tool.py -c prod.ini verify
```

推荐额外用**专用只读复制角色**跑常驻进程，而不是直接用超级用户账号：

```bash
python3 cdc_tool.py -c prod.ini setup --apply \
  --create-role cdc_reader --create-role-password '<强密码>'
# 之后把 prod.ini 里 [source] 的 user/password 改成这个专用账号
```

## 6. 日常运维

| 操作 | 命令 |
|------|------|
| 查看当前状态（高频调用安全） | `python3 cdc_tool.py -c prod.ini status` |
| 核对是否丢数据 | `python3 cdc_tool.py -c prod.ini verify` |
| 接监控系统 | `python3 cdc_tool.py -c prod.ini verify --json`，退出码 0=正常 1=有问题 |
| 表清单变更 | 改 `prod.ini` 的 `tables`，重新 `setup --apply`（增量对齐，不用重建复制槽） |
| 下线/迁移 | `python3 cdc_tool.py -c prod.ini teardown --purge-redis -y` |

建议 cron 定期跑 verify 并接告警（示例见 README.md §5）。**核心指标只看两个**：

- `pg_replication_slots.wal_status` 变成 `lost` → 立即处理，这是唯一无法挽回的丢失场景
- Redis 里 `cdc:heartbeat:<tag>` 的时间戳长期不更新 → 采集进程挂了，赶紧重启

故障排查表见 README.md §8，这里不重复。

## 7. 安全建议

- 用 `--create-role` 建的专用角色只有 `REPLICATION` + 对监听表的 `SELECT`，不要图省事用超级用户跑常驻进程。
- `prod.ini` 含明文密码，`chmod 600`，不要提交进版本库（建议 `.gitignore` 里排除 `*.ini` 只保留 `cdc.example.ini`）。
- Redis 如果同时要给下游团队访问，**给他们的账号只需要 `Stream` 相关命令权限**
  （`XREAD`/`XREADGROUP`/`XACK`/`XGROUP`/`XPENDING`/`XCLAIM`），不需要能访问采集器自己用的
  `cdc:seq:*`、`cdc:heartbeat:*`、`cdc:checkpoint:*`、`cdc:verify:*` 这些内部状态键。
  用 Redis ACL 按 key 前缀隔离最省事：

  ```
  ACL SETUSER cdc_consumer on >密码 ~cdc:events* +@read +xreadgroup +xack +xgroup +xpending +xclaim +xautoclaim
  ```

## 8. 交付给客户端团队的信息清单

对接前，把下面这张表填好发给下游团队（对应第二部分 §2 的占位表）：

| 项目 | 值 |
|------|-----|
| Redis 地址 | `<host>:<port>` |
| Redis 认证 | 密码 / ACL 用户名密码 |
| 是否需要 TLS | — |
| Stream key | `<你配置的 stream_key>` |
| 本 Stream 里会出现哪些 `source` 取值 | `<tag1>, <tag2>, ...`（如果只有一个源就一个） |
| 各 `source` 对应哪些表 | 附上每个 tag 的 `tables` 清单 |
| 事件产生延迟预期 | 正常情况亚秒级到几秒 |
| 联系人 / on-call | — |

---

# 第二部分：客户端对接说明

> 你不需要连 PostgreSQL、不需要知道 CDC 是怎么实现的。你只需要知道：
> 有一个 Redis Stream，里面按时间顺序放着一批表的变更事件，JSON 格式，
> 用标准的 Redis Stream 消费者组模式读取即可。

## 1. 你会拿到什么

一个 Redis Stream（Redis 的一种数据结构，类似支持消费确认的消息队列）。
每条消息只有一个字段 `payload`，内容是一段 JSON，描述一次 INSERT / UPDATE / DELETE。

## 2. 连接信息（找服务端团队要，见第一部分 §8）

| 项目 | 值 |
|------|-----|
| Redis 地址 | `_____________` |
| 认证信息 | `_____________` |
| Stream key | `_____________`（下文示例用 `cdc:events` 代替） |
| 关心哪些 `source` / 哪些表 | `_____________` |

## 3. 消息格式

```json
{
  "op": "u",
  "schema": "public",
  "table": "students",
  "before": {"id": "31", "class_name": "旧班级", "...": "..."},
  "after":  {"id": "31", "class_name": "新班级", "...": "..."},
  "seq": 128,
  "source": "mydb_prod",
  "ts_ms": 1783472753553
}
```

| 字段 | 说明 |
|------|------|
| `op` | `c`=INSERT，`u`=UPDATE，`d`=DELETE |
| `schema` / `table` | 变更发生的表 |
| `before` | 变更前的完整行（INSERT 时为 `null`；UPDATE/DELETE 时是完整旧行，不是只有主键） |
| `after` | 变更后的完整行（DELETE 时为 `null`） |
| `seq` | 该来源（`source`）内的全局递增序号，仅用于服务端自己的丢失检测，客户端一般不需要用它，但可以拿它做去重的辅助键 |
| `source` | 标识事件来自哪个源库（见第一部分 §4 的 `tag`），如果只对接一个源可以忽略 |
| `ts_ms` | 采集器观测到该事件的毫秒时间戳（不是数据库事务提交时间，会有小延迟） |

⚠️ **所有值都是文本格式的字符串**（即使原本是数字/日期），需要自行按业务字段类型转换，
例如 `after["id"]` 是 `"31"` 而不是 `31`。

## 4. 推荐消费方式：消费者组（Consumer Group）

不要用 `XREAD` 简单轮询整个流，要用消费者组——它能记录你消费到哪了，
进程重启后自动从上次位置续读，不用自己维护游标。

```bash
# 只需创建一次（幂等，已存在会报 BUSYGROUP，忽略即可）
# $ 表示从"现在"开始，不读历史积压；如需从头消费全部历史改成 0
redis-cli XGROUP CREATE cdc:events my-service-group $ MKSTREAM
```

Python 示例（`pip install redis`）：

```python
import json
import redis

r = redis.from_url("redis://:password@host:6379/0", decode_responses=True)
GROUP = "my-service-group"
CONSUMER = "worker-1"          # 同组多个进程要给不同的名字
STREAM = "cdc:events"

try:
    r.xgroup_create(STREAM, GROUP, id="$", mkstream=True)
except redis.ResponseError as e:
    if "BUSYGROUP" not in str(e):
        raise

while True:
    resp = r.xreadgroup(GROUP, CONSUMER, {STREAM: ">"}, count=100, block=5000)
    for _stream, messages in resp or []:
        for msg_id, fields in messages:
            event = json.loads(fields["payload"])
            if event.get("source") != "mydb_prod":   # 只关心自己的来源，多源共用同一 Stream 时需要过滤
                r.xack(STREAM, GROUP, msg_id)
                continue
            try:
                handle_event(event)                   # 你的业务处理逻辑
                r.xack(STREAM, GROUP, msg_id)          # 处理成功才确认
            except Exception:
                logging.exception("处理失败，先不 ACK，等待重试")
                # 不 ACK：消息留在 pending list，可用 XCLAIM/XAUTOCLAIM 认领重试（见 §5）
```

其它语言等价 API：Node.js 用 `ioredis` 的 `xreadgroup`/`xack`；Java 用 Jedis/Lettuce 的
`xreadGroup`/`xack`；命令语义完全一致，照抄上面的流程翻译即可。

## 5. 必须处理的可靠性问题

**5.1 至少一次投递（at-least-once），消息可能重复**

如果你的进程在"处理完业务逻辑"和"调用 XACK"之间崩溃，消息会在你重启后被重新投递一次。
**业务处理必须幂等**——推荐按 `(source, table主键)` 做 upsert，而不是简单地"收到 INSERT 就插入"，
否则重复消费会报主键冲突或产生脏数据。

**5.2 同一行的多次变更，跨消费者时不保证顺序**

消费者组内如果有多个 consumer 同时在线，Redis 按消息到达顺序轮流派发给不同 consumer，
**不像 Kafka 的 partition 那样能保证同一个 key 一定分给同一个消费者**。如果同一行连续
两次 UPDATE 被分给了两个不同的 consumer 并发处理，后处理完的可能会覆盖先处理完的结果，
即使它在 Stream 里的顺序更早。

处理办法（三选一）：
- 简单场景：只用**单个 consumer** 消费（吞吐够用的话最省心）；
- 高吞吐场景：消费后按业务主键做二次分片（哈希到多个内存队列/线程），保证同一行的事件
  在你自己的系统里是顺序处理的；
- 或者落库时用 `ts_ms`（或数据库自己的 `updated_at`）做"只有更新的时间戳更晚才覆盖"的
  条件写入，天然抗乱序。

**5.3 历史消息会被裁剪，不能依赖"随时能回放全部历史"**

Stream 有长度上限（服务端配置的 `stream_maxlen`），超过后旧消息会被自动清理。如果你的
消费者长时间下线，回来后可能已经有一段历史被裁剪掉、永久拿不到了。这不是 bug，是这套
方案用轻量换来的代价（服务端团队用 `verify` 命令监控这个风险，但客户端自己也应该：

- 监控自己的消费 lag（`XINFO GROUPS <stream>` 里的 `lag` 字段），不要让它持续增长；
- 长时间计划性下线前，评估离线期间的数据量是否会超过 `stream_maxlen`（找服务端团队问这个值）。

**5.4 卡住的消息（pending 但一直不 ACK）**

进程崩溃且来不及重启时，之前取走但没 ACK 的消息会一直挂在 pending list 里。定期检查并认领：

```python
# 认领超过 60 秒还没被处理完的消息，转交给当前 consumer 重新处理
claimed = r.xautoclaim(STREAM, GROUP, CONSUMER, min_idle_time=60_000, start_id="0-0", count=100)
```

## 6. 上线前自检清单

- [ ] 能连上 Redis，`XLEN cdc:events` 有返回值
- [ ] 创建消费者组成功，能读到消息并正确解析 JSON
- [ ] 故意让处理逻辑抛异常、不 ACK，确认消息会重新出现在 pending 里（`XPENDING`）
- [ ] 同一条消息重复投递两次，确认业务结果和投递一次时一致（幂等性测试）
- [ ] 杀掉消费进程再重启，确认能从上次位置继续、不会重复处理已 ACK 的消息、也不会丢消息
- [ ] 造一批并发变更，检查落库结果有没有因为乱序被覆盖错（§5.2）
- [ ] 长时间（超过预估的 `stream_maxlen` 覆盖窗口）不消费后再启动，确认知道自己可能错过了哪段数据，有没有兜底核对手段

## 7. 常见问题

- **消息里 before/after 有些字段是 `null`**：要么该列本身值就是 NULL，要么是 UPDATE 里
  没变化的大字段（TOAST，源库出于性能不会重发未变化的大字段原文）——正常现象，判断
  该字段是否"真的是 NULL"还是"未变化"，可以对比 `before` 和 `after` 里该字段是否都缺失。
- **同一条 UPDATE 收到两次一模一样的内容**：见 §5.1，做好幂等即可，不用当成 bug。
- **想要历史全量数据，不只是增量**：这套 Stream 只有增量变更，全量数据需要服务端团队
  另外提供一次性导出（比如 `pg_dump` 或直接查询源表），跟服务端团队协调。
