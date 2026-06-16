#!/usr/bin/env python3
"""把普通表批量转换成 TimescaleDB 超表(hypertable)。

两种模式（用 --new-table 控制）：
  • --new-table yes（默认）：**不动原表**。按 <原表名> + 后缀（默认 _ts）新建一张
    超表，把原表结构和数据拷过去，原表原封不动保留，方便两张表对比。
  • --new-table no：**原地替换原表**。把原表本身转成超表（数据随之迁移）。替换前会
    先把原表的建表 SQL 备份到 backups/ 目录（文件名带时间戳），以防万一。

用法：
    python3 convert_to_timescale.py <清单文件.ini> [选项]

清单文件格式见 tables.example.ini 与 README.md。

选项：
    --env-file PATH   指定数据库配置文件（默认同目录 db.env）
    --new-table {yes,no}
                      yes=新建后缀超表保留原表(默认)；no=原地替换原表(替换前自动备份)
    --suffix STR      新超表名后缀（默认 _ts），如原表 sensor_data -> sensor_data_ts
                      （仅 --new-table yes 生效）
    --backup-dir PATH 原地替换模式下备份原表建表 SQL 的目录（默认项目 backups/）
    --drop-existing   若同名后缀表已存在，先 DROP 再重建（仅 --new-table yes 生效）
    --dry-run         只打印将要执行的 SQL，不真正执行
    -v, --verbose     打印每条执行的 SQL
"""
import argparse
import configparser
import os
import sys
from datetime import datetime

import psycopg2

from dbconfig import get_dsn

DEFAULT_SUFFIX = "_ts"
_HERE = os.path.dirname(os.path.abspath(__file__))
# 项目根目录下的 backups/（脚本在 db/timescale_tools/ 下，往上两级即项目根）
DEFAULT_BACKUP_DIR = os.path.normpath(os.path.join(_HERE, "..", "..", "backups"))


def _bool(val, default=True):
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")


def parse_tables(path):
    """读取 INI 清单，返回 [(section, {字段...}), ...]。"""
    parser = configparser.ConfigParser()
    # 保留 key 大小写（默认会转小写，这里我们本就用小写 key，无所谓）
    read = parser.read(path, encoding="utf-8")
    if not read:
        sys.exit(f"[错误] 读不到清单文件: {path}")

    tables = []
    for section in parser.sections():
        sec = parser[section]
        if "time_column" not in sec:
            sys.exit(f"[错误] 表 [{section}] 缺少必填字段 time_column")
        tables.append((section, sec))
    if not tables:
        sys.exit(f"[错误] 清单文件里没有任何表 section: {path}")
    return tables


def qualify(section):
    """把 'schema.table' 或 'table' 拆成安全的限定名片段。"""
    if "." in section:
        schema, _, table = section.partition(".")
        return schema, table
    return None, section


def ident(schema, table):
    """返回带引号的限定标识符，给 SQL 字面拼接用。"""
    if schema:
        return f'"{schema}"."{table}"'
    return f'"{table}"'


def target_name(cfg, table, suffix):
    """计算新超表名：优先用 section 里的 target，否则原表名加后缀。"""
    override = cfg.get("target", "").strip()
    return override if override else f"{table}{suffix}"


def count_rows(conn, fq):
    """返回某张表的行数（出错返回 None）。"""
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {fq};")
            return cur.fetchone()[0]
    except psycopg2.Error:
        conn.rollback()
        return None


def build_statements(section, cfg, suffix, drop_existing, replace):
    """为单张表生成 (说明, SQL) 列表。

    replace=False：新建 <原表名+后缀> 超表并拷贝原表数据，原表不动。
    replace=True ：把原表本身原地转成超表（数据随 migrate_data 迁移），替换原表。
    """
    schema, table = qualify(section)
    src_fq = ident(schema, table)                      # 原表
    src_literal = f"{schema}.{table}" if schema else table

    time_column = cfg.get("time_column").strip()
    chunk_interval = cfg.get("chunk_interval", "7 days").strip()
    segmentby = cfg.get("segmentby", "").strip()
    orderby = cfg.get("orderby", "").strip()
    compress_after = cfg.get("compress_after", "").strip()
    space_partition = cfg.get("space_partition", "").strip()
    number_partitions = cfg.get("number_partitions", "").strip()
    migrate_data = _bool(cfg.get("migrate_data"), default=True)

    if replace:
        # 目标就是原表自身
        tgt_fq, tgt_literal = src_fq, src_literal
    else:
        new_table = target_name(cfg, table, suffix)
        tgt_fq = ident(schema, new_table)              # 新超表
        tgt_literal = f"{schema}.{new_table}" if schema else new_table

    stmts = []

    if replace:
        # 原地转换：直接把原表变成 hypertable，现有数据随之迁移进 chunk
        stmts.append((
            f"把原表 {src_literal} 原地转成 hypertable"
            f"（分区列={time_column}, chunk={chunk_interval}, 迁移现有数据）",
            f"SELECT create_hypertable("
            f"'{tgt_literal}', by_range('{time_column}', INTERVAL '{chunk_interval}'), "
            f"migrate_data => true, if_not_exists => true);",
        ))
    else:
        # 0) 可选：先删掉已存在的同名后缀表（只删我们建的后缀表，绝不碰原表）
        if drop_existing:
            stmts.append((
                f"删除已存在的目标表 {tgt_literal}（如有）",
                f"DROP TABLE IF EXISTS {tgt_fq} CASCADE;",
            ))

        # 1) 按原表结构新建一张空表（拷贝列/默认值/生成列/存储参数/注释；
        #    不拷贝索引和约束——含非时间列的主键/唯一约束会让 create_hypertable 失败）
        stmts.append((
            f"按 {src_literal} 的结构新建空表 {tgt_literal}",
            f"CREATE TABLE {tgt_fq} (LIKE {src_fq} "
            f"INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING IDENTITY "
            f"INCLUDING STORAGE INCLUDING COMMENTS);",
        ))

        # 2) 把新表变成 hypertable（此时是空表，无需 migrate_data）
        stmts.append((
            f"把 {tgt_literal} 变成 hypertable（分区列={time_column}, chunk={chunk_interval}）",
            f"SELECT create_hypertable("
            f"'{tgt_literal}', by_range('{time_column}', INTERVAL '{chunk_interval}'), "
            f"if_not_exists => true);",
        ))

    # 2b) 二级空间分区
    if space_partition:
        npart = number_partitions or "4"
        stmts.append((
            f"添加空间分区（{space_partition} x {npart}）",
            f"SELECT add_dimension("
            f"'{tgt_literal}', by_hash('{space_partition}', {npart}), "
            f"if_not_exists => true);",
        ))

    # 3) 新建模式才需要手动拷贝数据（原地替换由 migrate_data 完成）
    if not replace and migrate_data:
        stmts.append((
            f"拷贝 {src_literal} 的数据到 {tgt_literal}",
            f"INSERT INTO {tgt_fq} SELECT * FROM {src_fq};",
        ))

    # 4) 列存压缩（填了 segmentby 才开启）
    if segmentby:
        order_clause = orderby or f"{time_column} DESC"
        stmts.append((
            f"开启列存压缩（segmentby={segmentby}, orderby={order_clause}）",
            f"ALTER TABLE {tgt_fq} SET ("
            f"timescaledb.compress, "
            f"timescaledb.compress_segmentby = '{segmentby}', "
            f"timescaledb.compress_orderby = '{order_clause}');",
        ))

        # 5) 自动压缩策略
        if compress_after:
            stmts.append((
                f"添加自动压缩策略（{compress_after} 后压缩）",
                f"SELECT add_compression_policy("
                f"'{tgt_literal}', INTERVAL '{compress_after}', if_not_exists => true);",
            ))

    return stmts


def dump_create_table_sql(conn, schema, table):
    """从系统目录重建原表的建表 SQL（列定义 + 约束 + 索引），跨版本可用。"""
    reg = f"{schema}.{table}" if schema else table
    fq = ident(schema, table)
    parts = []
    with conn.cursor() as cur:
        # 列定义
        cur.execute(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), "
            "a.attnotnull, pg_get_expr(d.adbin, d.adrelid) "
            "FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped "
            "ORDER BY a.attnum;",
            (reg,),
        )
        coldefs = []
        for name, typ, notnull, default in cur.fetchall():
            piece = f'    "{name}" {typ}'
            if default is not None:
                piece += f" DEFAULT {default}"
            if notnull:
                piece += " NOT NULL"
            coldefs.append(piece)
        parts.append(f"CREATE TABLE {fq} (\n" + ",\n".join(coldefs) + "\n);")

        # 约束（主键/唯一/检查/外键）
        cur.execute(
            "SELECT conname, pg_get_constraintdef(oid) "
            "FROM pg_constraint WHERE conrelid = %s::regclass "
            "ORDER BY contype DESC, conname;",
            (reg,),
        )
        for conname, condef in cur.fetchall():
            parts.append(f'ALTER TABLE {fq} ADD CONSTRAINT "{conname}" {condef};')

        # 索引（排除主键及约束自带的索引，避免重复）
        cur.execute(
            "SELECT pg_get_indexdef(ix.indexrelid) "
            "FROM pg_index ix "
            "WHERE ix.indrelid = %s::regclass AND NOT ix.indisprimary "
            "AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = ix.indexrelid);",
            (reg,),
        )
        for (idxdef,) in cur.fetchall():
            parts.append(f"{idxdef};")
    conn.commit()
    return "\n".join(parts)


def backup_table_ddl(conn, section, backup_dir):
    """把原表建表 SQL 备份到 backup_dir，文件名带时间戳。返回备份文件路径。"""
    schema, table = qualify(section)
    os.makedirs(backup_dir, exist_ok=True)
    ddl = dump_create_table_sql(conn, schema, table)
    now = datetime.now()
    safe = ((f"{schema}." if schema else "") + table).replace(".", "_")
    path = os.path.join(backup_dir, f"{safe}_{now.strftime('%Y%m%d_%H%M%S')}.sql")
    header = (
        f"-- 原表建表备份: {(schema + '.' if schema else '') + table}\n"
        f"-- 备份时间: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"-- 用途: convert_to_timescale.py --new-table no 原地替换前的安全备份\n\n"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + ddl + "\n")
    return path


def main():
    ap = argparse.ArgumentParser(
        description="把普通表批量转成 TimescaleDB 超表（新建后缀表，不动原表）")
    ap.add_argument("tables_file", help="表转换清单文件（INI）")
    ap.add_argument("--env-file", help="数据库配置文件路径（默认同目录 db.env）")
    ap.add_argument("--new-table", choices=["yes", "no"], default="yes",
                    help="yes=新建后缀超表保留原表(默认)；no=原地替换原表(替换前自动备份原表建表SQL)")
    ap.add_argument("--suffix", default=DEFAULT_SUFFIX,
                    help=f"新超表名后缀（默认 {DEFAULT_SUFFIX}，仅 --new-table yes 生效）")
    ap.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR,
                    help=f"原地替换模式下备份原表建表 SQL 的目录（默认 {DEFAULT_BACKUP_DIR}）")
    ap.add_argument("--drop-existing", action="store_true",
                    help="若后缀表已存在则先 DROP 重建（默认跳过已存在的，仅 --new-table yes 生效）")
    ap.add_argument("--dry-run", action="store_true", help="只打印 SQL 不执行")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每条 SQL")
    args = ap.parse_args()

    replace = args.new_table == "no"
    tables = parse_tables(args.tables_file)
    dsn = get_dsn(args.env_file)

    print(f"[*] 共解析到 {len(tables)} 张待转换的表")
    if replace:
        print(f"[*] 模式：原地替换原表（替换前备份原表建表 SQL 到 {args.backup_dir}）\n")
    else:
        print(f"[*] 模式：新建超表 <原表名>{args.suffix}（原表保留不变，可对比）\n")

    if args.dry_run:
        for section, cfg in tables:
            print(f"--- {section} ---")
            if replace:
                print(f"-- 替换前会备份原表 {section} 的建表 SQL 到 {args.backup_dir}")
            for desc, sql in build_statements(section, cfg, args.suffix, args.drop_existing, replace):
                print(f"-- {desc}\n{sql}\n")
        print("[dry-run] 未连接数据库，未执行任何 SQL。")
        return

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    ok, fail = 0, 0
    try:
        # 确保扩展存在
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")
        conn.commit()

        for section, cfg in tables:
            print(f"==> 处理表 {section}")
            try:
                # 原地替换前先备份原表建表 SQL；备份失败则不动这张表
                if replace:
                    try:
                        path = backup_table_ddl(conn, section, args.backup_dir)
                        print(f"    ✓ 已备份原表建表 SQL: {path}")
                    except (psycopg2.Error, OSError) as be:
                        conn.rollback()
                        fail += 1
                        print(f"    ✗ 备份失败，跳过替换（原表未改动）: {str(be).strip()}")
                        continue

                for desc, sql in build_statements(section, cfg, args.suffix, args.drop_existing, replace):
                    if args.verbose:
                        print(f"    SQL: {sql}")
                    with conn.cursor() as cur:
                        cur.execute(sql)
                    print(f"    ✓ {desc}")
                conn.commit()
                ok += 1

                schema, table = qualify(section)
                src_fq = ident(schema, table)
                if replace:
                    # 原地替换：原表已变超表，只报告当前行数
                    n = count_rows(conn, src_fq)
                    conn.commit()
                    if n is not None:
                        print(f"    ✓ 原表已原地转为超表，当前 {n:,} 行")
                else:
                    # 校验：对比原表与新超表的行数，确认数据确实拷过去了
                    new_fq = ident(schema, target_name(cfg, table, args.suffix))
                    src_n = count_rows(conn, src_fq)
                    new_n = count_rows(conn, new_fq)
                    conn.commit()
                    if src_n is None or new_n is None:
                        print(f"    · 行数校验跳过（无法读取计数）")
                    elif src_n == new_n:
                        print(f"    ✓ 行数校验通过：原表 {src_n:,} 行 = 超表 {new_n:,} 行")
                    else:
                        print(f"    ⚠ 行数不一致：原表 {src_n:,} 行 ≠ 超表 {new_n:,} 行"
                              f"（检查 migrate_data 是否为 true，或表中途有写入）")
            except psycopg2.Error as e:
                conn.rollback()
                fail += 1
                print(f"    ✗ 失败: {str(e).strip()}")
        tail = "原表已被替换为超表（建表 SQL 已备份）。" if replace else "原表均未改动。"
        print(f"\n[完成] 成功 {ok} 张，失败 {fail} 张。{tail}")
    finally:
        conn.close()

    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
