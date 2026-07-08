#!/usr/bin/env python3
"""通用 PostgreSQL 表变动监听工具：PG 逻辑复制 → Redis Stream。

独立于本仓库的 backend 服务，只依赖 psycopg2 + redis，可以整个 db/cdc_tools/
目录拷到任意一台能访问目标 PostgreSQL 的机器上运行，用于监听「其他 postgres」
上任意指定的表集合。

子命令：
  setup     在目标库上准备 CDC 所需对象（wal_level 检查、publication、
            REPLICA IDENTITY、复制槽、可选的专用只读复制角色）
  run       启动采集器（前台常驻进程），监听 WAL 并推送变更到 Redis Stream
  verify    核对「PG → 采集器 → Redis」整个链路是否发生数据丢失
  status    快速查看当前状态（不做核对，可高频调用）
  teardown  清理复制槽 / publication / Redis 状态（卸载）

用法：
  cp cdc.example.ini my_source.ini   # 按需修改
  python3 cdc_tool.py -c my_source.ini setup --apply
  python3 cdc_tool.py -c my_source.ini run
  python3 cdc_tool.py -c my_source.ini verify
  python3 cdc_tool.py -c my_source.ini status
  python3 cdc_tool.py -c my_source.ini teardown --purge-redis -y

详细说明见同目录 README.md。
"""
import argparse
import configparser
import json
import logging
import os
import select
import signal
import struct
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

import psycopg2
import psycopg2.errors
import redis
from psycopg2.extras import LogicalReplicationConnection

logger = logging.getLogger("cdc_tool")


# ── pgoutput 协议解析（与 backend/app/cdc/pgoutput.py 保持一致的实现） ──────────

UNCHANGED_TOAST = "__unchanged_toast__"


def _cstring(buf: bytes, pos: int):
    end = buf.index(b"\x00", pos)
    return buf[pos:end].decode("utf-8"), end + 1


def _tuple_data(buf: bytes, pos: int):
    (ncols,) = struct.unpack_from(">H", buf, pos)
    pos += 2
    values = []
    for _ in range(ncols):
        kind = buf[pos:pos + 1]
        pos += 1
        if kind == b"n":
            values.append(None)
        elif kind == b"u":
            values.append(UNCHANGED_TOAST)
        else:  # 't' 文本格式
            (length,) = struct.unpack_from(">I", buf, pos)
            pos += 4
            values.append(buf[pos:pos + length].decode("utf-8"))
            pos += length
    return values, pos


class PgOutputParser:
    def __init__(self):
        self.relations = {}  # rel_id -> (schema, table, [列名])

    def parse(self, payload: bytes):
        if not payload:
            return None
        tag = payload[:1]
        if tag == b"R":
            self._parse_relation(payload)
            return None
        if tag not in (b"I", b"U", b"D"):
            return None

        (rel_id,) = struct.unpack_from(">I", payload, 1)
        rel = self.relations.get(rel_id)
        if rel is None:
            return None
        schema, table, cols = rel

        pos = 5
        before = after = None
        while pos < len(payload):
            sub = payload[pos:pos + 1]
            pos += 1
            values, pos = _tuple_data(payload, pos)
            row = dict(zip(cols, values))
            if sub in (b"K", b"O"):
                before = row
            elif sub == b"N":
                after = row
            else:
                break

        op = {b"I": "c", b"U": "u", b"D": "d"}[tag]
        return {"op": op, "schema": schema, "table": table,
                "before": before, "after": after}

    def _parse_relation(self, payload: bytes):
        pos = 1
        (rel_id,) = struct.unpack_from(">I", payload, pos)
        pos += 4
        schema, pos = _cstring(payload, pos)
        table, pos = _cstring(payload, pos)
        pos += 1  # replica identity 标志
        (ncols,) = struct.unpack_from(">H", payload, pos)
        pos += 2
        cols = []
        for _ in range(ncols):
            pos += 1  # 列标志位
            name, pos = _cstring(payload, pos)
            pos += 8  # 类型 OID + atttypmod
            cols.append(name)
        self.relations[rel_id] = (schema, table, cols)


# ── 配置 ─────────────────────────────────────────────────────────────────────

@dataclass
class Config:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    tag: str
    tables: list
    publication: str
    slot: str
    heartbeat_interval: int
    redis_url: str
    stream_key: str
    stream_maxlen: int
    seq_key: str
    heartbeat_key: str
    checkpoint_prefix: str
    verify_cursor_key: str
    last_seq_key: str
    count_check_tables: list
    lag_warn_bytes: int


def split_tables(raw: str):
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "." not in part:
            part = f"public.{part}"
        out.append(part)
    seen = set()
    result = []
    for t in out:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def qualify(full_table: str) -> str:
    schema, _, table = full_table.partition(".")
    return f'"{schema}"."{table}"'


def load_config(path: str, tables_override: str = None) -> Config:
    if not os.path.isfile(path):
        sys.exit(f"[错误] 配置文件不存在: {path}（可复制 cdc.example.ini 创建）")
    p = configparser.ConfigParser()
    p.read(path, encoding="utf-8")

    def req(section, key):
        if not p.has_option(section, key):
            sys.exit(f"[错误] 配置缺少 [{section}] {key}")
        return p.get(section, key)

    host = req("source", "host")
    port = int(p.get("source", "port", fallback="5432"))
    dbname = req("source", "dbname")
    user = req("source", "user")
    password = p.get("source", "password", fallback="")
    tag = p.get("source", "tag", fallback=dbname)

    tables = split_tables(tables_override if tables_override else req("cdc", "tables"))
    if not tables:
        sys.exit("[错误] 表清单为空")
    publication = p.get("cdc", "publication", fallback="cdc_pub")
    slot = p.get("cdc", "slot", fallback=f"cdc_slot_{tag}")
    heartbeat_interval = int(p.get("cdc", "heartbeat_interval", fallback="15"))

    redis_url = p.get("redis", "url", fallback="redis://localhost:6379/0")
    stream_key = p.get("redis", "stream_key", fallback="cdc:events")
    stream_maxlen = int(p.get("redis", "stream_maxlen", fallback="20000"))
    seq_key = p.get("redis", "seq_key", fallback=f"cdc:seq:{tag}")
    heartbeat_key = p.get("redis", "heartbeat_key", fallback=f"cdc:heartbeat:{tag}")
    checkpoint_prefix = p.get("redis", "checkpoint_prefix", fallback=f"cdc:checkpoint:{tag}")
    verify_cursor_key = p.get("redis", "verify_cursor_key", fallback=f"cdc:verify:cursor:{tag}")
    last_seq_key = p.get("redis", "last_seq_key", fallback=f"cdc:verify:lastseq:{tag}")

    count_check_raw = p.get("verify", "count_check_tables", fallback="")
    count_check_tables = split_tables(count_check_raw) if count_check_raw.strip() else []
    lag_warn_mb = int(p.get("verify", "lag_warn_mb", fallback="1024"))

    return Config(
        host=host, port=port, dbname=dbname, user=user, password=password, tag=tag,
        tables=tables, publication=publication, slot=slot, heartbeat_interval=heartbeat_interval,
        redis_url=redis_url, stream_key=stream_key, stream_maxlen=stream_maxlen,
        seq_key=seq_key, heartbeat_key=heartbeat_key, checkpoint_prefix=checkpoint_prefix,
        verify_cursor_key=verify_cursor_key, last_seq_key=last_seq_key,
        count_check_tables=count_check_tables, lag_warn_bytes=lag_warn_mb * 1024 * 1024,
    )


def connect_plain(cfg: Config):
    return psycopg2.connect(host=cfg.host, port=cfg.port, dbname=cfg.dbname,
                             user=cfg.user, password=cfg.password)


def connect_replication(cfg: Config):
    return psycopg2.connect(host=cfg.host, port=cfg.port, dbname=cfg.dbname,
                             user=cfg.user, password=cfg.password,
                             connection_factory=LogicalReplicationConnection)


# ── setup：在目标 PG 上准备 CDC 所需对象 ─────────────────────────────────────

def setup_cmd(cfg: Config, args):
    print(f"==> 目标库: {cfg.host}:{cfg.port}/{cfg.dbname}  (tag={cfg.tag})")
    print(f"    监听表 ({len(cfg.tables)}): {', '.join(cfg.tables)}")
    apply = args.apply
    if not apply:
        print("    [dry-run] 未加 --apply，只打印将要执行的操作，不修改数据库\n")

    conn = connect_plain(cfg)
    conn.autocommit = True
    cur = conn.cursor()
    need_restart = False

    # 1. wal_level
    cur.execute("SHOW wal_level;")
    wal_level = cur.fetchone()[0]
    print(f"[1/6] wal_level = {wal_level}")
    if wal_level != "logical":
        if apply:
            cur.execute("ALTER SYSTEM SET wal_level = 'logical';")
            print("      已执行 ALTER SYSTEM SET wal_level = 'logical'")
        else:
            print("      需要执行: ALTER SYSTEM SET wal_level = 'logical';")
        print("      ⚠ 该参数需要重启 PostgreSQL 才能生效")
        need_restart = True

    # 2. 复制槽/发送进程配额
    cur.execute("SHOW max_replication_slots;")
    mrs = int(cur.fetchone()[0])
    cur.execute("SHOW max_wal_senders;")
    mws = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM pg_replication_slots;")
    used_slots = cur.fetchone()[0]
    print(f"[2/6] max_replication_slots={mrs}(已用{used_slots})  max_wal_senders={mws}")
    if mrs - used_slots < 1:
        print("      ⚠ 复制槽配额不足")
        if apply:
            cur.execute("ALTER SYSTEM SET max_replication_slots = %s;", (mrs + 4,))
            cur.execute("ALTER SYSTEM SET max_wal_senders = %s;", (mws + 4,))
            print(f"      已调大 max_replication_slots/max_wal_senders 至 {mrs + 4}")
        else:
            print(f"      需要执行: ALTER SYSTEM SET max_replication_slots = {mrs + 4};")
        need_restart = True

    if need_restart:
        conn.close()
        print("\n[setup] 存在需要重启才能生效的配置变更。")
        print("        重启 PostgreSQL 后重新运行本命令以完成剩余步骤（发布/复制槽/权限）。")
        sys.exit(2)

    # 3. 复制权限
    cur.execute("SELECT rolreplication FROM pg_roles WHERE rolname = %s;", (cfg.user,))
    row = cur.fetchone()
    print(f"[3/6] 用户 {cfg.user} REPLICATION 权限 = {row[0] if row else '用户不存在'}")
    if row and not row[0]:
        if apply:
            cur.execute(f'ALTER USER "{cfg.user}" WITH REPLICATION;')
            print(f"      已授予 {cfg.user} REPLICATION 权限")
        else:
            print(f'      需要执行: ALTER USER "{cfg.user}" WITH REPLICATION;')

    # 4. 可选：创建专用只读复制角色
    if args.create_role:
        role = args.create_role
        pw = args.create_role_password or ""
        print(f"[4/6] 专用复制角色 {role}")
        if apply:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s;", (role,))
            if cur.fetchone():
                print("      角色已存在，跳过创建，仅刷新表权限")
            else:
                cur.execute(f'CREATE ROLE "{role}" WITH LOGIN REPLICATION PASSWORD %s;', (pw,))
                print(f"      ✓ 已创建角色 {role}")
            for t in cfg.tables:
                cur.execute(f'GRANT SELECT ON {qualify(t)} TO "{role}";')
            print(f"      ✓ 已授予 {len(cfg.tables)} 张表的 SELECT 权限")
        else:
            print(f"      需要执行: CREATE ROLE \"{role}\" WITH LOGIN REPLICATION PASSWORD '***'; "
                  f"并对每张表 GRANT SELECT")
    else:
        print("[4/6] 未指定 --create-role，跳过（沿用配置文件里的连接用户）")

    # 5. Publication
    cur.execute("SELECT 1 FROM pg_publication WHERE pubname=%s;", (cfg.publication,))
    pub_exists = cur.fetchone() is not None
    if pub_exists:
        cur.execute(
            "SELECT schemaname, tablename FROM pg_publication_tables WHERE pubname=%s;",
            (cfg.publication,),
        )
        existing = {f"{s}.{t}" for s, t in cur.fetchall()}
    else:
        existing = set()
    desired = set(cfg.tables)
    to_add = sorted(desired - existing)
    to_drop = sorted(existing - desired)

    if not pub_exists:
        print(f"[5/6] 创建 publication {cfg.publication}，包含 {len(cfg.tables)} 张表")
        if apply:
            tbl_list = ", ".join(qualify(t) for t in cfg.tables)
            cur.execute(f'CREATE PUBLICATION "{cfg.publication}" FOR TABLE {tbl_list};')
    else:
        print(f"[5/6] publication {cfg.publication} 已存在：新增 {len(to_add)} 张，移除 {len(to_drop)} 张")
        if apply:
            if to_add:
                cur.execute(f'ALTER PUBLICATION "{cfg.publication}" ADD TABLE '
                            f'{", ".join(qualify(t) for t in to_add)};')
            if to_drop:
                cur.execute(f'ALTER PUBLICATION "{cfg.publication}" DROP TABLE '
                            f'{", ".join(qualify(t) for t in to_drop)};')

    # 6. REPLICA IDENTITY FULL + 复制槽
    print(f"[6/6] REPLICA IDENTITY FULL（{len(cfg.tables)} 张表）+ 复制槽 {cfg.slot}")
    if apply:
        for t in cfg.tables:
            cur.execute(f"ALTER TABLE {qualify(t)} REPLICA IDENTITY FULL;")
        try:
            cur.execute("SELECT pg_create_logical_replication_slot(%s, 'pgoutput');", (cfg.slot,))
            print(f"      ✓ 复制槽已创建: {cfg.slot}")
        except psycopg2.errors.DuplicateObject:
            print(f"      · 复制槽已存在: {cfg.slot}")

    conn.close()

    if not apply:
        print("\n[setup] 以上为 dry-run，未对数据库做任何修改。确认无误后加 --apply 执行。")
    else:
        print("\n[setup] ✅ 完成。可以运行:")
        print(f"  python3 cdc_tool.py -c {args._config_path} run")


# ── run：采集器常驻进程 ───────────────────────────────────────────────────────

class Stats:
    def __init__(self):
        self.total = 0
        self.errors = 0
        self.per_table = defaultdict(int)
        self.last_lsn = None


def send_heartbeat(r, cfg: Config, stats: Stats):
    r.hset(cfg.heartbeat_key, mapping={
        "ts": time.time(),
        "total_events": stats.total,
        "errors": stats.errors,
        "last_lsn": stats.last_lsn or "",
        "tables": ",".join(cfg.tables),
    })
    r.expire(cfg.heartbeat_key, cfg.heartbeat_interval * 5)


def run_cmd(cfg: Config, args):
    running = {"v": True}

    def _stop(signum, _frame):
        print(f"\n收到信号 {signum}，准备停止...")
        running["v"] = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    logger.info("CDC 采集器启动: tag=%s tables=%s → redis stream=%s",
                cfg.tag, cfg.tables, cfg.stream_key)

    table_set = set(cfg.tables)
    while running["v"]:
        conn = None
        try:
            r = redis.from_url(cfg.redis_url)
            r.ping()
            conn = connect_replication(cfg)
            cur = conn.cursor()
            cur.start_replication(
                slot_name=cfg.slot, decode=False,
                options={"proto_version": "1", "publication_names": cfg.publication},
            )
            logger.info("复制已启动: slot=%s publication=%s", cfg.slot, cfg.publication)

            parser = PgOutputParser()
            stats = Stats()
            last_hb = 0.0

            while running["v"]:
                msg = cur.read_message()
                if msg:
                    event = parser.parse(msg.payload)
                    if event is not None:
                        full_table = f"{event['schema']}.{event['table']}"
                        if full_table in table_set:
                            seq = r.incr(cfg.seq_key)
                            event["seq"] = seq
                            event["source"] = cfg.tag
                            event["ts_ms"] = int(time.time() * 1000)
                            try:
                                r.xadd(cfg.stream_key, {"payload": json.dumps(event, ensure_ascii=False)},
                                       maxlen=cfg.stream_maxlen, approximate=True)
                                stats.total += 1
                                stats.per_table[full_table] += 1
                            except Exception as e:
                                stats.errors += 1
                                logger.error("XADD 失败（seq=%s 已消耗，该事件很可能丢失）: %s", seq, e)
                    stats.last_lsn = str(msg.data_start)
                    cur.send_feedback(flush_lsn=msg.data_start)
                else:
                    select.select([cur], [], [], 1.0)

                now = time.time()
                if now - last_hb > cfg.heartbeat_interval:
                    send_heartbeat(r, cfg, stats)
                    logger.info("心跳: 累计 %d 条，错误 %d 次，per_table=%s",
                                stats.total, stats.errors, dict(stats.per_table))
                    last_hb = now

            logger.info("已停止，共处理 %d 条，错误 %d 次", stats.total, stats.errors)
        except Exception as e:
            if not running["v"]:
                break
            logger.warning("采集异常: %s，5秒后重连...", e)
            time.sleep(5)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


# ── verify：核对整个链路是否发生数据丢失 ─────────────────────────────────────

def _id_tuple(rid: str):
    ms, seq = rid.split("-")
    return (int(ms), int(seq))


def _redis_id_gt(a: str, b: str) -> bool:
    return _id_tuple(a) > _id_tuple(b)


def verify_cmd(cfg: Config, args):
    results = []  # (name, status, message)  status in PASS/WARN/FAIL
    r = redis.from_url(cfg.redis_url, decode_responses=True)

    # 1. 复制槽健康检查（源库）
    try:
        conn = connect_plain(cfg)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT active, wal_status,
                   pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS lag_bytes
            FROM pg_replication_slots WHERE slot_name = %s
            """,
            (cfg.slot,),
        )
        row = cur.fetchone()
        conn.close()
        if row is None:
            results.append(("复制槽存在性", "FAIL", f"复制槽 {cfg.slot} 不存在"))
        else:
            active, wal_status, lag_bytes = row
            if wal_status == "lost":
                results.append(("复制槽 WAL 状态", "FAIL",
                                 "WAL 已被清理（wal_status=lost），确定发生数据丢失，"
                                 "需要重建复制槽并对下游做一次全量核对"))
            else:
                results.append(("复制槽 WAL 状态", "PASS", f"wal_status={wal_status}"))
            results.append(("复制槽连接状态", "PASS" if active else "WARN",
                             "采集器已连接" if active else "当前无采集器连接（可能只是重启中）"))
            lag_mb = (lag_bytes or 0) / 1024 / 1024
            if lag_bytes is not None and lag_bytes > cfg.lag_warn_bytes:
                results.append(("WAL 积压", "WARN", f"{lag_mb:.1f} MB，超过告警阈值，存在磁盘风险"))
            else:
                results.append(("WAL 积压", "PASS", f"{lag_mb:.1f} MB"))
    except Exception as e:
        results.append(("复制槽检查", "FAIL", f"连接源库失败: {e}"))

    # 2. 采集器心跳
    hb = r.hgetall(cfg.heartbeat_key)
    if not hb:
        results.append(("采集器心跳", "WARN", "未找到心跳记录，采集器可能从未运行过"))
    else:
        age = time.time() - float(hb.get("ts", 0))
        if age > cfg.heartbeat_interval * 3:
            results.append(("采集器心跳", "FAIL",
                             f"心跳已 {age:.0f}s 未更新（阈值 {cfg.heartbeat_interval * 3}s），"
                             f"采集器可能已停止"))
        else:
            results.append(("采集器心跳", "PASS",
                             f"{age:.0f}s 前，累计推送 {hb.get('total_events', '?')} 条"))

    # 3. Stream 裁剪检测：上次核对位置是否已被 MAXLEN 挤掉
    cursor_id = r.get(cfg.verify_cursor_key) or "0"
    oldest = r.xrange(cfg.stream_key, min="-", max="+", count=1)
    if oldest:
        oldest_id = oldest[0][0]
        if cursor_id != "0" and _redis_id_gt(oldest_id, cursor_id):
            results.append(("Stream 裁剪检测", "FAIL",
                             f"上次核对位置 {cursor_id} 已被 MAXLEN 裁剪（当前最旧 {oldest_id}），"
                             f"期间产生的变更事件已丢失且无法恢复"))
        else:
            results.append(("Stream 裁剪检测", "PASS", "未发生裁剪丢失"))
    else:
        results.append(("Stream 裁剪检测", "WARN", "Stream 当前为空"))

    # 4. 分页扫描 (cursor_id, +]：序列跳号检测 + 逐表净增量（供行数核对用）
    seqs = []
    deltas = defaultdict(int)
    own_count = 0
    total_scanned = 0
    last_id = cursor_id
    while True:
        batch = r.xrange(cfg.stream_key, min=f"({last_id}", max="+", count=1000)
        if not batch:
            break
        for entry_id, fields in batch:
            total_scanned += 1
            last_id = entry_id
            try:
                ev = json.loads(fields["payload"])
            except Exception:
                continue
            if ev.get("source") != cfg.tag:
                continue
            own_count += 1
            if "seq" in ev:
                seqs.append(ev["seq"])
            table = f'{ev.get("schema", "public")}.{ev.get("table", "")}'
            op = ev.get("op")
            if op == "c":
                deltas[table] += 1
            elif op == "d":
                deltas[table] -= 1
        if len(batch) < 1000:
            break

    # 跳号检测要跨 verify 运行连续，不能只看本次批次内部——否则被烧掉的
    # seq 恰好落在两次 verify 之间就永远不会被两边任何一次的批次看到。
    # 用上次 verify 记录的最大 seq 作为本次判断的起点。
    last_seq_raw = r.get(cfg.last_seq_key)
    last_seq = int(last_seq_raw) if last_seq_raw is not None else None

    if own_count == 0:
        results.append(("序列跳号检测", "PASS",
                         f"本次核对区间无本源（{cfg.tag}）新事件"
                         f"（共扫描 {total_scanned} 条，含其他来源）"))
    else:
        seqs.sort()
        check_seqs = ([last_seq] + seqs) if last_seq is not None else seqs
        gaps = [(a, b) for a, b in zip(check_seqs, check_seqs[1:]) if b - a > 1]
        if gaps:
            desc = "; ".join(f"{a}→{b}(缺{b - a - 1}条)" for a, b in gaps[:5])
            results.append(("序列跳号检测", "FAIL",
                             f"发现 {len(gaps)} 处跳号，疑似 XADD 失败导致事件丢失: {desc}"))
        else:
            results.append(("序列跳号检测", "PASS", f"本次核对 {own_count} 条事件，序列连续"))
        r.set(cfg.last_seq_key, seqs[-1])

    # 5. 行数核对（端到端的最终裁判：即使前面几项都 PASS，也用真实行数交叉验证）
    check_tables = cfg.tables if args.full else cfg.count_check_tables
    if not check_tables:
        results.append(("行数核对", "WARN", "未配置 count_check_tables，跳过（加 --full 核对全部表）"))
    else:
        try:
            conn = connect_plain(cfg)
            cur = conn.cursor()
            for t in check_tables:
                ck_key = f"{cfg.checkpoint_prefix}:{t}"
                ck = r.hgetall(ck_key)
                cur.execute(f"SELECT COUNT(*) FROM {qualify(t)};")
                actual = cur.fetchone()[0]
                delta = deltas.get(t, 0)
                if not ck:
                    results.append((f"行数核对[{t}]", "PASS", f"首次核对，建立基线 count={actual}"))
                    r.hset(ck_key, mapping={"count": actual, "ts": time.time()})
                    continue
                expected = int(ck["count"]) + delta
                if actual == expected:
                    results.append((f"行数核对[{t}]", "PASS",
                                     f"count={actual} 与预期一致（基线{ck['count']} + 净变化{delta}）"))
                    r.hset(ck_key, mapping={"count": actual, "ts": time.time()})
                else:
                    results.append((f"行数核对[{t}]", "FAIL",
                                     f"count={actual} 预期={expected}"
                                     f"（基线{ck['count']} + 净变化{delta}），差异={actual - expected}。"
                                     f"若采集器正在运行，可能是事件尚未到达，建议静默片刻后重跑；"
                                     f"否则怀疑数据丢失"))
                    # FAIL 时不推进基线，避免掩盖持续扩大的差异
            conn.close()
        except Exception as e:
            results.append(("行数核对", "FAIL", f"查询源库失败: {e}"))

    # 推进 verify 游标（序列/裁剪检测的扫描进度，与行数核对基线是否推进无关）
    if last_id != cursor_id:
        r.set(cfg.verify_cursor_key, last_id)

    _print_report(results, args.json)
    fails = sum(1 for _, s, _ in results if s == "FAIL")
    sys.exit(1 if fails else 0)


def _print_report(results, as_json):
    fails = sum(1 for _, s, _ in results if s == "FAIL")
    warns = sum(1 for _, s, _ in results if s == "WARN")
    if as_json:
        print(json.dumps({
            "ok": fails == 0,
            "timestamp": datetime.utcnow().isoformat(),
            "summary": {"total": len(results), "pass": len(results) - fails - warns,
                        "warn": warns, "fail": fails},
            "checks": [{"check": n, "status": s, "message": m} for n, s, m in results],
        }, ensure_ascii=False, indent=2))
        return
    icon = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}
    print()
    for name, status, msg in results:
        print(f"  [{icon.get(status, '?')} {status:4}] {name}: {msg}")
    print(f"\n共 {len(results)} 项：PASS {len(results) - fails - warns}  WARN {warns}  FAIL {fails}")
    if fails:
        print("\n❌ 存在数据丢失风险，请检查上方 FAIL 项")
    elif warns:
        print("\n⚠️  基本正常，但有需要关注的告警项")
    else:
        print("\n✅ 全部检查通过，未发现数据丢失")


# ── status：轻量状态查看 ──────────────────────────────────────────────────────

def status_cmd(cfg: Config, _args):
    print(f"==> CDC 状态: tag={cfg.tag}  publication={cfg.publication}  slot={cfg.slot}")
    try:
        conn = connect_plain(cfg)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT active, wal_status, pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)
            FROM pg_replication_slots WHERE slot_name=%s
            """,
            (cfg.slot,),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            print(f"  复制槽: active={row[0]} wal_status={row[1]} lag={(row[2] or 0) / 1024 / 1024:.1f}MB")
        else:
            print("  复制槽: 不存在")
    except Exception as e:
        print(f"  复制槽查询失败: {e}")

    try:
        r = redis.from_url(cfg.redis_url, decode_responses=True)
        hb = r.hgetall(cfg.heartbeat_key)
        if hb:
            age = time.time() - float(hb.get("ts", 0))
            print(f"  心跳: {age:.0f}s 前，累计 {hb.get('total_events')} 条，"
                  f"错误 {hb.get('errors', 0)} 次，最近LSN={hb.get('last_lsn')}")
        else:
            print("  心跳: 无记录")
        print(f"  Stream {cfg.stream_key} 长度: {r.xlen(cfg.stream_key)}")
    except Exception as e:
        print(f"  Redis 查询失败: {e}")


# ── teardown：卸载清理 ────────────────────────────────────────────────────────

def teardown_cmd(cfg: Config, args):
    conn = connect_plain(cfg)
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("SELECT active FROM pg_replication_slots WHERE slot_name=%s;", (cfg.slot,))
    row = cur.fetchone()
    if row is None:
        print(f"复制槽 {cfg.slot} 不存在，无需清理")
    else:
        if row[0] and not args.force:
            print(f"⚠ 复制槽 {cfg.slot} 当前处于活跃状态（采集器仍在运行）")
            print("  请先停止 run 进程，或加 --force 强制处理")
            conn.close()
            sys.exit(1)
        if not args.yes:
            ans = input(f"确认删除复制槽 {cfg.slot} ? [y/N] ").strip().lower()
            if ans != "y":
                conn.close()
                sys.exit("已取消")
        cur.execute("SELECT pg_drop_replication_slot(%s);", (cfg.slot,))
        print(f"✓ 已删除复制槽 {cfg.slot}")

    if args.drop_publication:
        cur.execute(f'DROP PUBLICATION IF EXISTS "{cfg.publication}";')
        print(f"✓ 已删除 publication {cfg.publication}")

    conn.close()

    if args.purge_redis:
        r = redis.from_url(cfg.redis_url, decode_responses=True)
        keys = [cfg.seq_key, cfg.heartbeat_key, cfg.verify_cursor_key, cfg.last_seq_key]
        keys += list(r.scan_iter(f"{cfg.checkpoint_prefix}:*"))
        if keys:
            r.delete(*keys)
        print(f"✓ 已清理 Redis 状态键（{len(keys)} 个）；共享 Stream {cfg.stream_key} 本身未删除")


# ── main ─────────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default="cdc.ini", help="配置文件路径（默认 ./cdc.ini）")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("setup", help="在目标 PostgreSQL 上准备 CDC 所需对象")
    sp.add_argument("--apply", action="store_true", help="实际执行变更（默认只是 dry-run 打印）")
    sp.add_argument("--create-role", help="额外创建一个专用只读复制角色，仅授予所需权限")
    sp.add_argument("--create-role-password", help="配合 --create-role 使用")
    sp.add_argument("--tables", help="覆盖配置文件中的表清单（逗号分隔，如 public.students,public.orders）")

    sp = sub.add_parser("run", help="启动 CDC 采集器（前台常驻进程）")
    sp.add_argument("--tables", help="覆盖配置文件中的表清单（逗号分隔）")

    sp = sub.add_parser("verify", help="核对整个链路是否发生数据丢失")
    sp.add_argument("--full", action="store_true", help="对全部表做行数核对，忽略 count_check_tables 限制")
    sp.add_argument("--json", action="store_true", help="以 JSON 输出结果，便于监控系统采集")

    sub.add_parser("status", help="快速查看当前状态（不做核对，可高频调用）")

    sp = sub.add_parser("teardown", help="清理复制槽 / publication / Redis 状态")
    sp.add_argument("--drop-publication", action="store_true", help="同时删除 publication")
    sp.add_argument("--purge-redis", action="store_true", help="同时清理 Redis 中的状态键")
    sp.add_argument("--force", action="store_true", help="即使复制槽仍活跃也强制处理")
    sp.add_argument("-y", "--yes", action="store_true", help="跳过确认提示")

    return p


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_parser().parse_args()
    cfg = load_config(args.config, getattr(args, "tables", None))
    args._config_path = args.config
    {
        "setup": setup_cmd,
        "run": run_cmd,
        "verify": verify_cmd,
        "status": status_cmd,
        "teardown": teardown_cmd,
    }[args.command](cfg, args)


if __name__ == "__main__":
    main()
