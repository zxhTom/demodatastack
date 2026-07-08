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

详细说明见同目录 README.md。
"""
import argparse
import configparser
import os
import subprocess
import sys

import psycopg2
import psycopg2.errors

from dbconfig import get_dsn

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(_HERE, "tables.ini")
METER_SEED_DIR = os.path.normpath(os.path.join(_HERE, "..", "meter_seed"))

SEED_MODULES = {
    "curve": "seed_meter_data.py",   # 12 张 d_load_*/d_read_curve*/d_demand_curve 曲线表
    "event": "seed_event_data.py",   # 10 张事件表
    # log（sys_fep_comm_log）目前没有现成造数据脚本，先不注册
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


def pk_constraint_name(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid=%s::regclass AND contype='p';",
            (table,),
        )
        row = cur.fetchone()
    return row[0] if row else None


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


# ── to-hyper：普通表 -> 超表 ──────────────────────────────────────────────────

def _build_hypertable(conn, tmp, src, cfg, verbose):
    time_col = cfg["time_column"]
    _exec(conn, "清理残留临时表", f'DROP TABLE IF EXISTS "{tmp}";', verbose)
    _exec(conn, f"从 {src} 创建新普通表 {tmp}（含完整结构）",
          f'CREATE TABLE "{tmp}" (LIKE "{src}" INCLUDING ALL);', verbose)

    if cfg["pk_columns"]:
        tmp_pk = pk_constraint_name(conn, tmp)
        if tmp_pk:
            _exec(conn, f"删除继承来的主键 {tmp_pk}",
                  f'ALTER TABLE "{tmp}" DROP CONSTRAINT "{tmp_pk}";', verbose)
        _exec(conn, f"重建主键 ({', '.join(cfg['pk_columns'])})，纳入分区列 {time_col}",
              f'ALTER TABLE "{tmp}" ADD PRIMARY KEY ({", ".join(cfg["pk_columns"])});', verbose)

    _exec(conn, f"create_hypertable({tmp}, {time_col}, chunk={cfg['chunk_interval']})",
          f"SELECT create_hypertable('public.{tmp}', "
          f"by_range('{time_col}', INTERVAL '{cfg['chunk_interval']}'), "
          f"migrate_data => false, if_not_exists => true);", verbose)

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
            f'CREATE TABLE "{tmp}" (LIKE "{src}" INCLUDING ALL);',
        ]
        if cfg["pk_columns"]:
            steps += [f'ALTER TABLE "{tmp}" DROP CONSTRAINT <继承来的主键>;',
                      f'ALTER TABLE "{tmp}" ADD PRIMARY KEY ({", ".join(cfg["pk_columns"])});']
        steps += [
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
            steps += [f'DROP TABLE "{table}";  -- 删除当前（错误的）超表', f'ALTER TABLE "{tmp}" RENAME TO "{table}";']
        else:
            steps += [f'ALTER TABLE "{table}" RENAME TO "{bak}";', f'ALTER TABLE "{tmp}" RENAME TO "{table}";']
        _print_dry_steps(steps)
        return "ok"

    try:
        _build_hypertable(conn, tmp, src, cfg, verbose)

        src_n = count_rows(conn, src)
        tmp_n = count_rows(conn, tmp)
        if src_n != tmp_n:
            conn.rollback()
            print(f"  [错误]  {table}: 行数不一致（源 {src_n:,} ≠ 新超表 {tmp_n:,}），"
                  f"已回滚，原表未改动")
            return "error"
        print(f"    ✓ 行数校验通过：{src_n:,} 行")

        with conn.cursor() as cur:
            if hyper and redo:
                cur.execute(f'DROP TABLE "{table}";')
            else:
                cur.execute(f'ALTER TABLE "{table}" RENAME TO "{bak}";')
            cur.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}";')
        conn.commit()
        note = f"（备份 {bak} 保留不动）" if (hyper and redo) else f"（原表备份为 {bak}）"
        print(f"    ✓ 重命名完成 {note}")
        print(f"  [完成]  {table}: 已转为超表")
        return "ok"
    except psycopg2.Error as e:
        conn.rollback()
        print(f"  [错误]  {table}: {str(e).strip()}")
        print(f"          原表未改动；临时表 {tmp} 如残留可手动 DROP 后重试")
        return "error"


# ── to-plain：超表 -> 普通表 ──────────────────────────────────────────────────

def reown_shared_sequences(conn, table, old_table, verbose):
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


def to_plain_one(conn, table, dry_run, verbose):
    """返回 'skip' | 'ok' | 'error'。conn 必须是真实连接（dry-run 也要做实时诊断）。"""
    tmp = f"{table}_plain_new"
    bak = f"{table}_ts_old"

    if not table_exists(conn, table):
        print(f"  [跳过]  {table}: 表不存在")
        return "skip"
    if not is_hypertable(conn, table):
        print(f"  [跳过]  {table}: 当前是普通表，没有需要转换的超表状态"
              f"（如果它本应是超表，说明它从未被成功转换过——用 status 命令确认，"
              f"需要的话用 to-hyper 转换）")
        return "skip"
    if table_exists(conn, bak):
        print(f"  [错误]  {table}: 备份名 {bak} 已被占用，请先处理（重命名或 DROP）后重试")
        return "error"

    print(f"\n  [REVERT] {table} → 普通表（超表备份: {bak}）")

    if dry_run:
        _print_dry_steps([
            f'DROP TABLE IF EXISTS "{tmp}";',
            f'CREATE TABLE "{tmp}" (LIKE "{table}" INCLUDING ALL);',
            f'INSERT INTO "{tmp}" SELECT * FROM "{table}";  -- 压缩数据透明解压',
            '-- 行数校验：count(table) == count(tmp)，不一致则中止',
            f'ALTER TABLE "{table}" RENAME TO "{bak}";',
            f'ALTER TABLE "{tmp}" RENAME TO "{table}";',
        ])
        return "ok"

    try:
        _exec(conn, "清理残留临时表", f'DROP TABLE IF EXISTS "{tmp}";', verbose)
        _exec(conn, f"按超表结构创建普通表 {tmp}",
              f'CREATE TABLE "{tmp}" (LIKE "{table}" INCLUDING ALL);', verbose)

        print(f"    复制数据 {table} → {tmp}（全量，压缩 chunk 自动解压，可能较慢）…", flush=True)
        with conn.cursor() as cur:
            cur.execute(f'INSERT INTO "{tmp}" SELECT * FROM "{table}";')
            inserted = cur.rowcount
        print(f"    ✓ 数据复制完成，{inserted:,} 行")

        src_n = count_rows(conn, table)
        dst_n = count_rows(conn, tmp)
        if src_n != dst_n:
            conn.rollback()
            print(f"  [错误]  {table}: 行数不一致（超表 {src_n:,} ≠ 新表 {dst_n:,}），"
                  f"已回滚，原表未改动")
            return "error"
        print(f"    ✓ 行数校验通过：{src_n:,} 行")

        with conn.cursor() as cur:
            cur.execute(f'ALTER TABLE "{table}" RENAME TO "{bak}";')
            cur.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}";')
        conn.commit()
        print(f"    ✓ 重命名完成（超表备份为 {bak}）")

        reown_shared_sequences(conn, table, bak, verbose)
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
        _confirm(targets.keys(), "转为超表" if not args.redo else "从备份重新迁移")

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")
    conn.commit()

    ok = skip = error = 0
    for table, cfg in sorted(targets.items()):
        result = to_hyper_one(conn, table, cfg, args.redo, False, args.verbose)
        ok += result == "ok"
        skip += result == "skip"
        error += result == "error"
    conn.close()
    print(f"\n完成: 转换 {ok} 张，跳过 {skip} 张，失败 {error} 张")
    sys.exit(1 if error else 0)


def to_plain_cmd(dsn, targets, args):
    if args.dry_run:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        for table in sorted(targets):
            to_plain_one(conn, table, True, False)
        conn.close()
        print("\n[dry-run] 以上为将要执行的操作（已用真实连接做状态诊断），未对数据库做任何修改。")
        return

    if not args.yes:
        _confirm(targets.keys(), "转回普通表")

    conn = psycopg2.connect(dsn)
    conn.autocommit = False

    ok = skip = error = 0
    for table in sorted(targets):
        result = to_plain_one(conn, table, False, args.verbose)
        ok += result == "ok"
        skip += result == "skip"
        error += result == "error"
    conn.close()
    print(f"\n完成: 转换 {ok} 张，跳过 {skip} 张，失败 {error} 张")
    if ok:
        print("确认数据无误后，可手动 DROP 各 *_ts_old 备份表释放空间。")
    sys.exit(1 if error else 0)


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
        sys.exit(f"[错误] 未知 module: {module}，可选: {', '.join(SEED_MODULES)}"
                  f"（log/sys_fep_comm_log 暂无现成造数据脚本）")

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

    sp = sub.add_parser("to-hyper", help="普通表 -> 超表")
    add_filters(sp)
    sp.add_argument("--redo", action="store_true", help="对已是超表的表，从 _pg_old 备份重新迁移")
    sp.add_argument("--dry-run", action="store_true", help="只打印 SQL，不执行")
    sp.add_argument("-y", "--yes", action="store_true", help="跳过确认提示")
    sp.add_argument("-v", "--verbose", action="store_true", help="打印每条 SQL")

    sp = sub.add_parser("to-plain", help="超表 -> 普通表")
    add_filters(sp)
    sp.add_argument("--dry-run", action="store_true", help="只打印 SQL，不执行")
    sp.add_argument("-y", "--yes", action="store_true", help="跳过确认提示")
    sp.add_argument("-v", "--verbose", action="store_true", help="打印每条 SQL")

    sub.add_parser("seed", help=f"造数据，转发给 db/meter_seed（--module {{{'/'.join(SEED_MODULES)}}} ...）")

    return p


def main():
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
    main()
