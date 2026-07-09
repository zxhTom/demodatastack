#!/usr/bin/env python3
"""普通表 <-> TimescaleDB 超表 双向转换工具（合并版）。

合并自之前的三个脚本：
  convert_to_timescale.py       （已废弃，功能被本脚本 to-hyper 取代）
  timescale_migrate_event.py    （已废弃，功能被本脚本 to-hyper 取代）
  timescale_revert_event.py     （已废弃，功能被本脚本 to-plain 取代）
外加一个 seed 子命令，按 --module 转发给 db/meter_seed 下现成的造数据脚本。

两个方向共用同一份表清单（tables.ini），任何一个方向都不会丢数据：
  to-hyper：普通表 -> 超表。原表改名备份为 <表>_pg_old，新超表顶替原名。
  to-plain：超表 -> 普通表。原超表改名备份为 <表>_ts_old，新普通表顶替原名。
每一步都在事务里做行数校验，不一致就回滚、原表不动；备份表全程保留，
确认无误后再手动 DROP。

用法：
  python3 table_convert.py status
  python3 table_convert.py status --group log
  python3 table_convert.py to-hyper --group event -y
  python3 table_convert.py to-hyper --tables sys_fep_comm_log -y
  python3 table_convert.py to-hyper --group event --redo -y   # 从 _pg_old 重新迁移
  python3 table_convert.py to-plain --group curve -y
  python3 table_convert.py to-plain --tables d_alarm_event -y
  python3 table_convert.py seed --module curve --start 2025-01-01 --days 7
  python3 table_convert.py seed --module event --start 2025-01-01 --end 2025-01-31
  python3 table_convert.py seed --module log --start 2025-01-01 --days 7

可随时 Ctrl+C 中断（当前表处理完/回滚后才停，不会停在表中间），重新执行同样
的命令会自动跳过已完成的表、从断点继续；想强制重做已完成的表用 --redo；
支持 --progress-file 输出结构化进度、nohup 后台运行，详见 README.md §9.6-9.8。
"""
import argparse
import configparser
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime

import psycopg2
import psycopg2.errors

from dbconfig import get_dsn

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(_HERE, "tables.ini")
METER_SEED_DIR = os.path.normpath(os.path.join(_HERE, "..", "meter_seed"))

SEED_MODULES = {
    "curve": "seed_meter_data.py",   # 12 张 d_load_*/d_read_curve*/d_demand_curve 曲线表
    "event": "seed_event_data.py",   # 10 张事件表
    "log":   "seed_log_data.py",     # sys_fep_comm_log（FEP 通信日志）
}


# ── 配置解析 ─────────────────────────────────────────────────────────────────

def parse_tables(path):
    parser = configparser.ConfigParser()
    if not parser.read(path, encoding="utf-8"):
        sys.exit(f"[错误] 读不到配置文件: {path}")
    tables = {}
    for section in parser.sections():
        sec = parser[section]
        if "time_column" not in sec:
            sys.exit(f"[错误] 表 [{section}] 缺少必填字段 time_column")
        pk_raw = sec.get("pk_columns", "").strip()
        tables[section] = {
            "group":          sec.get("group", "other").strip(),
            "time_column":    sec.get("time_column").strip(),
            "chunk_interval": sec.get("chunk_interval", "7 days").strip(),
            "segmentby":      sec.get("segmentby", "").strip(),
            "orderby":        sec.get("orderby", "").strip(),
            "compress_after": sec.get("compress_after", "").strip(),
            "pk_columns":     [c.strip() for c in pk_raw.split(",") if c.strip()],
        }
    if not tables:
        sys.exit(f"[错误] 配置文件里没有任何表: {path}")
    return tables


def filter_tables(all_tables, group, tables_arg):
    if tables_arg:
        names = [t.strip() for t in tables_arg.split(",") if t.strip()]
        unknown = [n for n in names if n not in all_tables]
        if unknown:
            sys.exit(f"[错误] 配置文件里没有这些表: {unknown}")
        return {n: all_tables[n] for n in names}
    if group == "all":
        return dict(all_tables)
    picked = {n: cfg for n, cfg in all_tables.items() if cfg["group"] == group}
    if not picked:
        sys.exit(f"[错误] 配置文件里没有 group={group} 的表")
    return picked


# ── 中断处理 & 进度文件 ──────────────────────────────────────────────────────
# 设计目标：随时能 Ctrl+C，且不会留下半成品。
#   第一次 Ctrl+C：不立即杀进程，等当前这张表处理完（提交或回滚）再停。
#   第二次 Ctrl+C：立即强制退出——这也是安全的，因为当前表如果还没提交，
#   进程退出时连接断开，PostgreSQL 会自动回滚这张表未提交的事务，不会留半成品；
#   下次重跑时 to_hyper_one/to_plain_one 开头都会先 DROP TABLE IF EXISTS 清理
#   残留的 _ts_new/_plain_new 临时表。

_stop = {"requested": False}


def _install_signal_handlers():
    def _handler(signum, _frame):
        name = signal.Signals(signum).name
        if _stop["requested"]:
            print(f"\n[!] 再次收到 {name}，立即强制退出（若当前表正在写库，连接断开后"
                  f"数据库会自动回滚这张表未提交的部分，不会产生半成品）。", flush=True)
            os._exit(130)
        _stop["requested"] = True
        print(f"\n[!] 收到 {name}：当前这张表处理完（提交或回滚）后就停，不会停在表中间。"
              f"再按一次 Ctrl+C 立即强制退出。", flush=True)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def write_progress(path, entry):
    """追加一行 JSON 到进度文件，供后台运行时另开终端 tail -f 查看结构化进度。"""
    if not path:
        return
    entry = dict(entry, ts=datetime.now().isoformat(timespec="seconds"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── DB 状态查询 ──────────────────────────────────────────────────────────────

def table_exists(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name=%s", (table,),
        )
        return cur.fetchone()[0] > 0


def is_hypertable(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM timescaledb_information.hypertables "
            "WHERE hypertable_schema='public' AND hypertable_name=%s", (table,),
        )
        return cur.fetchone()[0] > 0


def is_partitioned(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname=%s AND n.nspname='public'", (table,),
        )
        row = cur.fetchone()
        return row is not None and row[0] == 'p'


def count_rows(conn, table):
    with conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM "{table}";')
        return cur.fetchone()[0]


def pk_columns_of(conn, table):
    """返回当前主键列名的有序列表；没有主键返回 None。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey::int2[])
            WHERE i.indrelid = %s::regclass AND i.indisprimary
            ORDER BY array_position(i.indkey::int2[], a.attnum);
            """,
            (table,),
        )
        rows = [r[0] for r in cur.fetchall()]
    return rows or None


def compression_enabled(conn, table):
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT compression_enabled FROM timescaledb_information.hypertables "
                "WHERE hypertable_name=%s", (table,),
            )
            row = cur.fetchone()
        return bool(row and row[0])
    except psycopg2.Error:
        conn.rollback()
        return None


def diagnose_pk(current_pk, cfg):
    """判断该表要转成超表前，主键是否需要处理。返回 (need_rebuild, ok, detail)。"""
    time_col = cfg["time_column"]
    if cfg["pk_columns"]:
        return True, True, f"将重建为 ({', '.join(cfg['pk_columns'])})"
    if current_pk and time_col not in current_pk:
        return False, False, (
            f"当前主键 ({', '.join(current_pk)}) 不含分区列 {time_col}，"
            f"需要在 tables.ini 给这张表加 pk_columns = ...（须包含 {time_col}）"
        )
    return False, True, "无需重建" if current_pk is None else "已含分区列"


# ── status 子命令 ────────────────────────────────────────────────────────────

def status_cmd(dsn, targets):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    print(f"{'表名':<34}{'状态':<10}{'主键诊断':<50}{'备份':<20}")
    print("-" * 114)
    for table, cfg in sorted(targets.items()):
        if not table_exists(conn, table):
            print(f"{table:<34}{'不存在':<10}{'-':<50}{'-':<20}")
            continue
        if is_hypertable(conn, table):
            state = "超表"
            pk_detail = f"compression={compression_enabled(conn, table)}"
        elif is_partitioned(conn, table):
            state = "PG分区表"
            pk_detail = "-"
        else:
            state = "普通表"
            current_pk = pk_columns_of(conn, table)
            _, ok, detail = diagnose_pk(current_pk, cfg)
            pk_detail = ("✓ " if ok else "✗ ") + detail
        backups = [b for b in (f"{table}_pg_old", f"{table}_ts_old") if table_exists(conn, b)]
        print(f"{table:<34}{state:<10}{pk_detail:<50}{', '.join(backups) or '-':<20}")
    conn.close()


# ── 公共执行辅助 ─────────────────────────────────────────────────────────────

def _exec(conn, desc, sql, verbose):
    if verbose:
        print(f"    SQL: {sql}")
    with conn.cursor() as cur:
        cur.execute(sql)
    print(f"    ✓ {desc}")


def _print_dry_steps(steps):
    for line in steps:
        print(f"    -- {line}")


# ── 索引/约束原名保留 ──────────────────────────────────────────────────────────
# CREATE TABLE ... (LIKE src INCLUDING INDEXES/CONSTRAINTS) 不会保留原索引名和
# 原 PK/UNIQUE 约束名，会按新表名重新生成一套（比如 idx_alarm_device 会变成
# xxx_ts_new_device_id_idx）。CHECK 约束名不受影响，只有独立索引和 PK/UNIQUE
# 约束（连带它们背后的索引）会被改名。所以这里不用 INCLUDING INDEXES/CONSTRAINTS，
# 改成显式读取原表的索引/约束定义，改名阶段按原名重建，保证转换前后索引名一致。

def get_index_and_constraint_defs(conn, table, strip_suffix=None):
    """返回 (constraints, indexes)：
    constraints = [(conname, contype, condef), ...]  # PK/UNIQUE/EXCLUDE（含 CHECK 之外的命名约束）
    indexes = [(indexname, indexdef), ...]  # 不背靠约束的独立索引
    CHECK 约束不在这里处理——LIKE INCLUDING CONSTRAINTS 本来就会正确保留它的原名。

    strip_suffix：从 _pg_old/_ts_old 备份表读取（--redo 场景）时必须传。备份表上的
    约束/索引名此刻是 rename_away_conflicting 改过的带后缀名（比如
    pk_xxx_pgold），不是真正的原名——如果原样拿去在新表上重建，会既对不上用户
    原来的名字，又会跟备份表自己身上还留着的同名对象连接层面撞名（实测复现过）。
    这里按已知后缀把名字和 indexdef 里嵌入的名字都还原回真正的原名。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT conname, contype, pg_get_constraintdef(oid) "
            "FROM pg_constraint WHERE conrelid=%s::regclass AND contype IN ('p','u','x');",
            (table,),
        )
        raw_constraints = cur.fetchall()
        backed_names = {c[0] for c in raw_constraints}
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname='public' AND tablename=%s;",
            (table,),
        )
        raw_indexes = [(n, d) for n, d in cur.fetchall() if n not in backed_names]

    def _strip(name):
        if strip_suffix and name.endswith(strip_suffix):
            return name[: -len(strip_suffix)]
        return name

    constraints = [(_strip(conname), contype, condef) for conname, contype, condef in raw_constraints]

    indexes = []
    for indexname, indexdef in raw_indexes:
        real_name = _strip(indexname)
        if real_name != indexname:
            indexdef = re.sub(rf'\bINDEX\s+{re.escape(indexname)}\b', f'INDEX {real_name}', indexdef, count=1)
        indexes.append((real_name, indexdef))

    return constraints, indexes


def _safe_suffixed(name, suffix):
    """PG 标识符最长 63 字节，加后缀超长就从中间截断，避免改名时报错。"""
    max_len = 63
    if len(name) + len(suffix) <= max_len:
        return name + suffix
    return name[: max_len - len(suffix)] + suffix


def _retarget_indexdef(indexdef, new_table):
    """indexdef 形如 'CREATE [UNIQUE] INDEX 名字 ON public.表名 USING ...'，
    只替换 ON 子句里的表名，索引名和其余定义原样保留。"""
    return re.sub(r'\bON\s+public\.\w+', f'ON "{new_table}"', indexdef, count=1)


def rename_away_conflicting(conn, table, constraints, indexes, verbose, tag):
    """table 是刚被改名成备份的旧表：它身上的约束/索引还叫原名（改表名不会
    连带改约束/索引名），这里给它们改名让路，腾出原名给新表按原名重建。
    改约束名会自动连带把背后的索引也一起改名（PG 行为），不用单独处理。

    tag 必须是调用方专属的后缀（比如 to-hyper 用 "pgold"、to-plain 用 "tsold"），
    不能用一个固定后缀——同一张表在 to-hyper/to-plain 之间来回转换时，_pg_old
    和 _ts_old 两个备份表会先后各自尝试"腾出同一个原名"，如果两边用同一个后缀，
    第二次转换时会撞上第一次转换留下的、还没清理的同名对象（实测复现过这个问题）。
    """
    suffix = f"_{tag}"
    for conname, _, _ in constraints:
        new_name = _safe_suffixed(conname, suffix)
        _exec(conn, f"备份表上的约束 {conname} 改名让路 → {new_name}",
              f'ALTER TABLE "{table}" RENAME CONSTRAINT "{conname}" TO "{new_name}";', verbose)
    for indexname, _ in indexes:
        new_name = _safe_suffixed(indexname, suffix)
        _exec(conn, f"备份表上的索引 {indexname} 改名让路 → {new_name}",
              f'ALTER INDEX "{indexname}" RENAME TO "{new_name}";', verbose)


def apply_indexes_and_constraints(conn, table, constraints, indexes, verbose,
                                  pk_columns=None, time_column=None):
    """在 table（改名后重新上位的新表）上按原名重建约束和独立索引。
    传了 pk_columns 时，原主键定义会被跳过，改用 pk_columns 的列重建
    （原主键列结构不含分区列，不能照抄），但如果原来有主键，沿用它的原名。
    to-plain 方向不需要重建主键，直接不传 pk_columns 即可（原样照抄所有约束）。
    """
    old_pk_name = next((c for c, t, _ in constraints if t == 'p'), None)
    for conname, contype, condef in constraints:
        if contype == 'p' and pk_columns:
            continue  # 用下面 pk_columns 重建的复合主键代替，列结构不同，不能照抄原定义
        _exec(conn, f"重建约束 {conname}",
              f'ALTER TABLE "{table}" ADD CONSTRAINT "{conname}" {condef};', verbose)

    if pk_columns:
        pk_name = old_pk_name or f"{table}_pkey"
        note = f"，沿用原主键名 {pk_name}" if old_pk_name else ""
        _exec(conn, f"重建主键 ({', '.join(pk_columns)})，纳入分区列 {time_column}{note}",
              f'ALTER TABLE "{table}" ADD CONSTRAINT "{pk_name}" '
              f'PRIMARY KEY ({", ".join(pk_columns)});', verbose)

    for indexname, indexdef in indexes:
        stmt = _retarget_indexdef(indexdef, table)
        _exec(conn, f"重建索引 {indexname}", stmt, verbose)


# ── to-hyper：普通表 -> 超表 ──────────────────────────────────────────────────

def _build_hypertable(conn, tmp, src, cfg, verbose):
    time_col = cfg["time_column"]
    _exec(conn, "清理残留临时表", f'DROP TABLE IF EXISTS "{tmp}";', verbose)
    _exec(conn, f"从 {src} 创建新表 {tmp}（仅列结构，索引/约束改名让路后按原名重建）",
          f'CREATE TABLE "{tmp}" (LIKE "{src}" '
          f'INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING STORAGE INCLUDING COMMENTS);', verbose)

    _exec(conn, f"create_hypertable({tmp}, {time_col}, chunk={cfg['chunk_interval']})",
          f"SELECT create_hypertable('public.{tmp}', "
          f"by_range('{time_col}', INTERVAL '{cfg['chunk_interval']}'), "
          f"migrate_data => false, if_not_exists => true, "
          # 不让 TimescaleDB 自动加一个默认的时间列索引——原表有没有这个索引由
          # 我们自己按原样重建（下面 apply_indexes_and_constraints），自动加的话
          # 会多出一个原表没有的索引，破坏"索引集合和原表完全一致"的保证。
          f"create_default_indexes => false);", verbose)

    if cfg["segmentby"]:
        orderby = cfg["orderby"] or f"{time_col} DESC"
        _exec(conn, f"开启列存压缩（segmentby={cfg['segmentby']}）",
              f'ALTER TABLE "{tmp}" SET ('
              f"timescaledb.compress, "
              f"timescaledb.compress_segmentby = '{cfg['segmentby']}', "
              f"timescaledb.compress_orderby = '{orderby}');", verbose)
        if cfg["compress_after"]:
            _exec(conn, f"添加自动压缩策略（{cfg['compress_after']} 后压缩）",
                  f"SELECT add_compression_policy('public.{tmp}', "
                  f"INTERVAL '{cfg['compress_after']}', if_not_exists => true);", verbose)

    print(f"    复制数据 {src} → {tmp}（全量，可能较慢）…", flush=True)
    with conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{tmp}" SELECT * FROM "{src}";')
        inserted = cur.rowcount
    print(f"    ✓ 数据复制完成，{inserted:,} 行")


def to_hyper_one(conn, table, cfg, redo, dry_run, verbose):
    """返回 'skip' | 'ok' | 'error'"""
    tmp = f"{table}_ts_new"
    bak = f"{table}_pg_old"

    if not table_exists(conn, table):
        print(f"  [跳过]  {table}: 表不存在")
        return "skip"

    hyper = is_hypertable(conn, table)
    if hyper and not redo:
        print(f"  [跳过]  {table}: 已经是超表（想强制重做加 --redo，会从 {bak} 重新迁移）")
        return "skip"
    if hyper and redo and not table_exists(conn, bak):
        print(f"  [跳过]  {table}: 已经是超表但找不到备份 {bak}，无法 --redo")
        return "skip"
    if not hyper and is_partitioned(conn, table):
        print(f"  [跳过]  {table}: 是 PG 原生分区表（暂不支持，需要另行处理）")
        return "skip"
    if not hyper and table_exists(conn, bak):
        # --redo 只在"现在就是超表"时才有意义（从 _pg_old 重新迁移）；现在已经是
        # 普通表的话 --redo 不适用，会走下面的普通迁移路径，同样需要这个校验，
        # 不能因为传了 --redo 就放过（这里曾经错误地放过，实测复现过这个 bug）。
        print(f"  [错误]  {table}: 备份名 {bak} 已被占用，请先处理（重命名或 DROP）后重试")
        return "error"

    src = bak if (hyper and redo) else table
    current_pk = pk_columns_of(conn, src)
    need_rebuild, pk_ok, pk_detail = diagnose_pk(current_pk, cfg)
    if not pk_ok:
        print(f"  [错误]  {table}: {pk_detail}")
        return "error"

    mode = "REDO（从备份重新迁移）" if (hyper and redo) else "MIGRATE"
    print(f"\n  [{mode}] {table} → 超表（源: {src}，PK 处理: {pk_detail}）")

    if dry_run:
        steps = [
            f'DROP TABLE IF EXISTS "{tmp}";',
            f'CREATE TABLE "{tmp}" (LIKE "{src}" INCLUDING DEFAULTS INCLUDING GENERATED '
            f'INCLUDING STORAGE INCLUDING COMMENTS);  -- 不含索引/约束',
            f"SELECT create_hypertable('public.{tmp}', by_range('{cfg['time_column']}', "
            f"INTERVAL '{cfg['chunk_interval']}'), ...);",
        ]
        if cfg["segmentby"]:
            steps += ['ALTER TABLE ... SET (timescaledb.compress, ...);',
                      f"SELECT add_compression_policy(..., INTERVAL '{cfg['compress_after']}', ...);"]
        steps += [
            f'INSERT INTO "{tmp}" SELECT * FROM "{src}";',
            '-- 行数校验：count(src) == count(tmp)，不一致则中止',
        ]
        if hyper and redo:
            steps += [f'DROP TABLE "{table}";  -- 删除当前（错误的）超表',
                      '-- 按原名重建索引/约束（PK 用 pk_columns 重建，其余照抄原定义）',
                      f'ALTER TABLE "{tmp}" RENAME TO "{table}";']
        else:
            steps += [f'ALTER TABLE "{table}" RENAME TO "{bak}";',
                      '-- 备份表上的索引/约束改名让路（原名腾给新表）',
                      '-- 按原名重建索引/约束（PK 用 pk_columns 重建，其余照抄原定义）',
                      f'ALTER TABLE "{tmp}" RENAME TO "{table}";']
        _print_dry_steps(steps)
        return "ok"

    try:
        strip_suffix = "_pgold" if (hyper and redo) else None
        constraints, indexes = get_index_and_constraint_defs(conn, src, strip_suffix=strip_suffix)
        _build_hypertable(conn, tmp, src, cfg, verbose)

        src_n = count_rows(conn, src)
        tmp_n = count_rows(conn, tmp)
        if src_n != tmp_n:
            conn.rollback()
            print(f"  [错误]  {table}: 行数不一致（源 {src_n:,} ≠ 新超表 {tmp_n:,}），"
                  f"已回滚，原表未改动")
            return "error"
        print(f"    ✓ 行数校验通过：{src_n:,} 行")

        owned_seqs = []
        if hyper and redo:
            owned_seqs = detach_owned_sequences(conn, table, verbose)
            with conn.cursor() as cur:
                cur.execute(f'DROP TABLE "{table}";')
            apply_indexes_and_constraints(conn, tmp, constraints, indexes, verbose,
                                          pk_columns=cfg["pk_columns"], time_column=cfg["time_column"])
            with conn.cursor() as cur:
                cur.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}";')
        else:
            with conn.cursor() as cur:
                cur.execute(f'ALTER TABLE "{table}" RENAME TO "{bak}";')
            rename_away_conflicting(conn, bak, constraints, indexes, verbose, tag="pgold")
            apply_indexes_and_constraints(conn, tmp, constraints, indexes, verbose,
                                          pk_columns=cfg["pk_columns"], time_column=cfg["time_column"])
            with conn.cursor() as cur:
                cur.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}";')
        conn.commit()
        note = f"（备份 {bak} 保留不动）" if (hyper and redo) else f"（原表备份为 {bak}）"
        print(f"    ✓ 重命名完成 {note}")

        if hyper and redo:
            reattach_sequences(conn, table, owned_seqs, verbose)
        else:
            reown_sequences_by_default_expr(conn, table, bak, verbose)
        conn.commit()  # 序列改挂也要提交，否则连接关闭时会被静默回滚
        print(f"  [完成]  {table}: 已转为超表")
        return "ok"
    except psycopg2.Error as e:
        conn.rollback()
        print(f"  [错误]  {table}: {str(e).strip()}")
        print(f"          原表未改动；临时表 {tmp} 如残留可手动 DROP 后重试")
        return "error"


# ── 序列（serial 列）归属处理 ──────────────────────────────────────────────────
# LIKE ... INCLUDING DEFAULTS 只会把 nextval('seq'...) 这段默认值表达式复制过去，
# 序列本身的 OWNED BY 归属不会跟着走，仍然挂在被改名/即将被 DROP 的旧表上。不处理
# 会有两个风险：①旧表被当成"过期备份"删掉时连带把序列级联删掉，新表的自增列就断了；
# ②--redo 直接 DROP 当前表时，如果序列还挂在它身上，同样会被级联删掉。

def find_owned_sequences(conn, table):
    """返回 [(seq_name, col_name), ...]：当前 OWNED BY table 的序列。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.relname, a.attname
            FROM pg_depend dep
            JOIN pg_class s ON s.oid = dep.objid AND s.relkind = 'S'
            JOIN pg_attribute a ON a.attrelid = dep.refobjid AND a.attnum = dep.refobjsubid
            WHERE dep.refobjid = %s::regclass AND dep.deptype = 'a'
            """,
            (table,),
        )
        return cur.fetchall()


def detach_owned_sequences(conn, table, verbose):
    """DROP TABLE 前，先把当前 OWNED BY table 的序列摘掉，避免级联删除。
    返回摘下来的 [(seq, col), ...]，DROP+改名完成后要用 reattach_sequences 挂回去。
    """
    owned = find_owned_sequences(conn, table)
    for seq, col in owned:
        _exec(conn, f"临时摘掉序列 {seq} 的 OWNED BY（避免 DROP TABLE 级联删掉它）",
              f'ALTER SEQUENCE "{seq}" OWNED BY NONE;', verbose)
    return owned


def reattach_sequences(conn, table, owned, verbose):
    for seq, col in owned:
        _exec(conn, f"序列 {seq} 重新挂回 {table}.{col}",
              f'ALTER SEQUENCE "{seq}" OWNED BY "{table}"."{col}";', verbose)


def reown_sequences_by_default_expr(conn, table, old_table, verbose):
    """table 是刚改名上位的新表，old_table 是刚被顶替下去（改名为备份）的旧表：
    序列此刻仍 OWNED BY old_table（改名不影响 OID，归属跟着旧名字走），
    这里按 table 上继承来的 nextval() 默认值表达式找到对应序列，改挂到 table 名下。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.attname, s.relname
            FROM pg_attribute a
            JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            JOIN pg_depend dep ON dep.refobjid = ('public.' || quote_ident(%s))::regclass
                 AND dep.deptype = 'a' AND dep.classid = 'pg_class'::regclass
            JOIN pg_class s ON s.oid = dep.objid AND s.relkind = 'S'
            WHERE a.attrelid = ('public.' || quote_ident(%s))::regclass
              AND pg_get_expr(d.adbin, d.adrelid) LIKE 'nextval(''' || s.relname || '%%'
            """,
            (old_table, table),
        )
        rows = cur.fetchall()
    for col, seq in rows:
        _exec(conn, f"序列 {seq} 改挂到 {table}.{col}",
              f'ALTER SEQUENCE "{seq}" OWNED BY "{table}"."{col}";', verbose)


def to_plain_one(conn, table, redo, dry_run, verbose):
    """返回 'skip' | 'ok' | 'error'。conn 必须是真实连接（dry-run 也要做实时诊断）。

    redo=True 时，如果当前已经是普通表，会从 {table}_ts_old 备份重新转换一遍
    （丢弃当前普通表里、上次转换之后可能新写入的数据，只保留备份那一刻的快照），
    与 to_hyper_one 的 --redo 语义对称。
    """
    tmp = f"{table}_plain_new"
    bak = f"{table}_ts_old"

    if not table_exists(conn, table):
        print(f"  [跳过]  {table}: 表不存在")
        return "skip"

    plain_already = not is_hypertable(conn, table)
    if plain_already and not redo:
        print(f"  [跳过]  {table}: 当前是普通表，没有需要转换的超表状态"
              f"（如果它本应是超表，说明它从未被成功转换过——用 status 命令确认，"
              f"需要的话用 to-hyper 转换；如果是想从 {bak} 备份重新转一遍，加 --redo）")
        return "skip"
    if plain_already and redo and not table_exists(conn, bak):
        print(f"  [跳过]  {table}: 当前是普通表但找不到备份 {bak}，无法 --redo")
        return "skip"
    if not plain_already and table_exists(conn, bak):
        print(f"  [错误]  {table}: 备份名 {bak} 已被占用，请先处理（重命名或 DROP）后重试")
        return "error"

    src = bak if (plain_already and redo) else table
    mode = "REDO（从备份重新转换）" if (plain_already and redo) else "REVERT"
    print(f"\n  [{mode}] {table} → 普通表（源: {src}）")

    if dry_run:
        steps = [
            f'DROP TABLE IF EXISTS "{tmp}";',
            f'CREATE TABLE "{tmp}" (LIKE "{src}" INCLUDING DEFAULTS INCLUDING GENERATED '
            f'INCLUDING STORAGE INCLUDING COMMENTS);  -- 不含索引/约束',
            f'INSERT INTO "{tmp}" SELECT * FROM "{src}";  -- 压缩数据透明解压',
            '-- 行数校验：count(src) == count(tmp)，不一致则中止',
        ]
        if plain_already and redo:
            steps += [f'DROP TABLE "{table}";  -- 丢弃当前普通表（用 {bak} 的内容取代）',
                      '-- 按原名重建索引/约束',
                      f'ALTER TABLE "{tmp}" RENAME TO "{table}";']
        else:
            steps += [f'ALTER TABLE "{table}" RENAME TO "{bak}";',
                      '-- 备份表上的索引/约束改名让路（原名腾给新表）',
                      '-- 按原名重建索引/约束',
                      f'ALTER TABLE "{tmp}" RENAME TO "{table}";']
        _print_dry_steps(steps)
        return "ok"

    try:
        strip_suffix = "_tsold" if (plain_already and redo) else None
        constraints, indexes = get_index_and_constraint_defs(conn, src, strip_suffix=strip_suffix)
        _exec(conn, "清理残留临时表", f'DROP TABLE IF EXISTS "{tmp}";', verbose)
        _exec(conn, f"从 {src} 创建普通表 {tmp}（仅列结构，索引/约束改名让路后按原名重建）",
              f'CREATE TABLE "{tmp}" (LIKE "{src}" '
              f'INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING STORAGE INCLUDING COMMENTS);', verbose)

        print(f"    复制数据 {src} → {tmp}（全量，压缩 chunk 自动解压，可能较慢）…", flush=True)
        with conn.cursor() as cur:
            cur.execute(f'INSERT INTO "{tmp}" SELECT * FROM "{src}";')
            inserted = cur.rowcount
        print(f"    ✓ 数据复制完成，{inserted:,} 行")

        src_n = count_rows(conn, src)
        dst_n = count_rows(conn, tmp)
        if src_n != dst_n:
            conn.rollback()
            print(f"  [错误]  {table}: 行数不一致（源 {src_n:,} ≠ 新表 {dst_n:,}），"
                  f"已回滚，原表未改动")
            return "error"
        print(f"    ✓ 行数校验通过：{src_n:,} 行")

        owned_seqs = []
        if plain_already and redo:
            owned_seqs = detach_owned_sequences(conn, table, verbose)
            with conn.cursor() as cur:
                cur.execute(f'DROP TABLE "{table}";')
            apply_indexes_and_constraints(conn, tmp, constraints, indexes, verbose)
            with conn.cursor() as cur:
                cur.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}";')
        else:
            with conn.cursor() as cur:
                cur.execute(f'ALTER TABLE "{table}" RENAME TO "{bak}";')
            rename_away_conflicting(conn, bak, constraints, indexes, verbose, tag="tsold")
            apply_indexes_and_constraints(conn, tmp, constraints, indexes, verbose)
            with conn.cursor() as cur:
                cur.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}";')
        conn.commit()
        note = f"（备份 {bak} 保留不动）" if (plain_already and redo) else f"（超表备份为 {bak}）"
        print(f"    ✓ 重命名完成 {note}")

        if plain_already and redo:
            reattach_sequences(conn, table, owned_seqs, verbose)
        else:
            reown_sequences_by_default_expr(conn, table, bak, verbose)
        conn.commit()  # 序列改挂也要提交，否则连接关闭时会被静默回滚
        print(f"  [完成]  {table}: 已转回普通表")
        return "ok"
    except psycopg2.Error as e:
        conn.rollback()
        print(f"  [错误]  {table}: {str(e).strip()}")
        print(f"          原超表未改动；临时表 {tmp} 如残留可手动 DROP 后重试")
        return "error"


# ── 子命令入口（带确认提示） ───────────────────────────────────────────────────

def _confirm(targets, verb, extra_note=""):
    print(f"以下 {len(targets)} 张表将执行【{verb}】{extra_note}：")
    for t in sorted(targets):
        print(f"  • {t}")
    ans = input("\n确认继续？[y/N] ").strip().lower()
    if ans != "y":
        sys.exit("已取消。")


def _run_batch(dsn, items, one_table_fn, progress_file):
    """跑一批表的公共主循环：进度前缀、可中断（先停当前表再退出）、进度文件、结果汇总。
    one_table_fn(conn, item) -> 'ok'|'skip'|'error'，item 是 (table, cfg) 或 table。
    """
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")
    conn.commit()

    _install_signal_handlers()
    total = len(items)
    ok = skip = error = processed = 0
    interrupted = False
    for idx, item in enumerate(items, 1):
        table = item[0] if isinstance(item, tuple) else item
        if _stop["requested"]:
            interrupted = True
            print(f"\n[中止] 已停止。完成 {processed}/{total} 张，剩余 {total - processed} 张未处理。"
                  f"重新执行同样的命令会自动跳过已完成的表、从这里继续。")
            break
        print(f"\n{'─' * 70}\n[{idx}/{total}] {table}", flush=True)
        result = one_table_fn(conn, item)
        processed += 1
        ok += result == "ok"
        skip += result == "skip"
        error += result == "error"
        write_progress(progress_file, {"event": "table_done", "idx": idx, "total": total,
                                       "table": table, "result": result})
    conn.close()
    write_progress(progress_file, {
        "event": "run_interrupted" if interrupted else "run_complete",
        "ok": ok, "skip": skip, "error": error, "processed": processed, "total": total,
    })
    return ok, skip, error, interrupted


def to_hyper_cmd(dsn, targets, args):
    if args.dry_run:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True  # 只读诊断，避免单张表查询出错拖垮后面表的事务
        for table, cfg in sorted(targets.items()):
            to_hyper_one(conn, table, cfg, args.redo, True, False)
        conn.close()
        print("\n[dry-run] 以上为将要执行的操作（已用真实连接做状态诊断），未对数据库做任何修改。")
        return

    if not args.yes:
        note = ("\n⚠️  --redo 会丢弃当前超表里、上次迁移之后新写入的数据，只保留 _pg_old "
                 "备份那一刻的快照，确认这是你想要的再继续" if args.redo else "")
        _confirm(targets.keys(), "转为超表" if not args.redo else "从 _pg_old 备份重新迁移（REDO）", note)

    items = sorted(targets.items())

    def _one(conn, item):
        table, cfg = item
        return to_hyper_one(conn, table, cfg, args.redo, False, args.verbose)

    ok, skip, error, interrupted = _run_batch(dsn, items, _one, args.progress_file)
    print(f"\n完成: 转换 {ok} 张，跳过 {skip} 张，失败 {error} 张"
          + ("（已中断，未处理的下次重跑会自动继续）" if interrupted else ""))
    sys.exit(1 if error else (130 if interrupted else 0))


def to_plain_cmd(dsn, targets, args):
    if args.dry_run:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        for table in sorted(targets):
            to_plain_one(conn, table, args.redo, True, False)
        conn.close()
        print("\n[dry-run] 以上为将要执行的操作（已用真实连接做状态诊断），未对数据库做任何修改。")
        return

    if not args.yes:
        note = ("\n⚠️  --redo 会丢弃当前普通表里、上次转换之后新写入的数据，只保留 _ts_old "
                 "备份那一刻的快照，确认这是你想要的再继续" if args.redo else "")
        _confirm(targets.keys(), "转回普通表" if not args.redo else "从 _ts_old 备份重新转换（REDO）", note)

    items = sorted(targets)

    def _one(conn, item):
        return to_plain_one(conn, item, args.redo, False, args.verbose)

    ok, skip, error, interrupted = _run_batch(dsn, items, _one, args.progress_file)
    print(f"\n完成: 转换 {ok} 张，跳过 {skip} 张，失败 {error} 张"
          + ("（已中断，未处理的下次重跑会自动继续）" if interrupted else ""))
    if ok:
        print("确认数据无误后，可手动 DROP 各 *_ts_old 备份表释放空间。")
    sys.exit(1 if error else (130 if interrupted else 0))


# ── seed 子命令：按 module 转发给 db/meter_seed 下的现成脚本 ───────────────────

def seed_cmd(raw_args):
    if "--module" not in raw_args:
        sys.exit(f"[错误] seed 需要 --module {{{'/'.join(SEED_MODULES)}}}，"
                  f"例如: table_convert.py seed --module curve --start 2025-01-01 --days 7")
    idx = raw_args.index("--module")
    if idx + 1 >= len(raw_args):
        sys.exit("[错误] --module 后面缺参数")
    module = raw_args[idx + 1]
    if module not in SEED_MODULES:
        sys.exit(f"[错误] 未知 module: {module}，可选: {', '.join(SEED_MODULES)}")

    script = os.path.join(METER_SEED_DIR, SEED_MODULES[module])
    if not os.path.isfile(script):
        sys.exit(f"[错误] 找不到脚本: {script}")

    rest = raw_args[:idx] + raw_args[idx + 2:]
    cmd = [sys.executable, script] + rest
    print(f"[*] module={module} → {SEED_MODULES[module]}")
    print(f"[*] 执行: {' '.join(cmd)}")
    print(f"[*] 未显式传 --env-file 时，{SEED_MODULES[module]} 默认读取它自己目录下的 "
          f"db/meter_seed/db.env；--env-file/--sql-out 等相对路径按你当前所在目录解析\n")
    sys.stdout.flush()
    r = subprocess.run(cmd)  # 不改 cwd：相对路径按调用者所在目录解析，脚本本身用绝对路径定位
    sys.exit(r.returncode)


# ── main ─────────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default=DEFAULT_CONFIG, help=f"表清单（默认 {DEFAULT_CONFIG}）")
    p.add_argument("--env-file", help="数据库连接配置文件路径（默认同目录 db.env）")
    sub = p.add_subparsers(dest="command", required=True)

    def add_filters(sp):
        sp.add_argument("--group", choices=["event", "curve", "log", "all"], default="all",
                        help="只处理指定分组（默认 all）")
        sp.add_argument("--tables", help="逗号分隔的表名，优先于 --group")

    sp = sub.add_parser("status", help="查看每张表当前状态（普通表/超表/PK 是否兼容分区列）")
    add_filters(sp)

    def add_run_options(sp):
        sp.add_argument("--dry-run", action="store_true", help="只打印将要做什么，不执行")
        sp.add_argument("-y", "--yes", action="store_true", help="跳过确认提示")
        sp.add_argument("-v", "--verbose", action="store_true", help="打印每条 SQL")
        sp.add_argument("--progress-file", help="把每张表的处理结果追加写入这个文件（JSON Lines），"
                                                 "配合 nohup 后台运行时用 tail -f 查看结构化进度")

    sp = sub.add_parser("to-hyper", help="普通表 -> 超表")
    add_filters(sp)
    sp.add_argument("--redo", action="store_true",
                    help="已是超表的表，从 _pg_old 备份重新迁移（完全重新跑，会丢弃上次迁移后的新数据）")
    add_run_options(sp)

    sp = sub.add_parser("to-plain", help="超表 -> 普通表")
    add_filters(sp)
    sp.add_argument("--redo", action="store_true",
                    help="已是普通表的表，从 _ts_old 备份重新转换（完全重新跑，会丢弃上次转换后的新数据）")
    add_run_options(sp)

    sub.add_parser("seed", help=f"造数据，转发给 db/meter_seed（--module {{{'/'.join(SEED_MODULES)}}} ...）")

    return p


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)  # nohup 重定向到文件时也能 tail -f 实时看到
    except AttributeError:
        pass

    # seed 子命令直接转发未知参数给底层脚本，绕开 argparse 子命令解析
    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        seed_cmd(sys.argv[2:])
        return

    args = build_parser().parse_args()
    dsn = get_dsn(args.env_file)
    all_tables = parse_tables(args.config)
    targets = filter_tables(all_tables, args.group, args.tables)

    if args.command == "status":
        status_cmd(dsn, targets)
    elif args.command == "to-hyper":
        to_hyper_cmd(dsn, targets, args)
    elif args.command == "to-plain":
        to_plain_cmd(dsn, targets, args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # 批量转换过程中的 Ctrl+C 由 _install_signal_handlers 优雅处理；
        # 这里兜底的是确认提示/status/dry-run 等还没装上信号处理器时的中断。
        sys.exit(130)
