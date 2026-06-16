#!/usr/bin/env python3
"""把普通表批量转换成 TimescaleDB 超表(hypertable)。

用法：
    python3 convert_to_timescale.py <清单文件.ini> [选项]

清单文件格式见 tables.example.ini 与 README.md。

选项：
    --env-file PATH   指定数据库配置文件（默认同目录 db.env）
    --dry-run         只打印将要执行的 SQL，不真正执行
    -v, --verbose     打印每条执行的 SQL
"""
import argparse
import configparser
import sys

import psycopg2

from dbconfig import get_dsn


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


def build_statements(section, cfg):
    """为单张表生成 (说明, SQL, 参数) 列表。"""
    schema, table = qualify(section)
    fq = ident(schema, table)
    rel_literal = (f"{schema}.{table}" if schema else table)

    time_column = cfg.get("time_column").strip()
    chunk_interval = cfg.get("chunk_interval", "7 days").strip()
    segmentby = cfg.get("segmentby", "").strip()
    orderby = cfg.get("orderby", "").strip()
    compress_after = cfg.get("compress_after", "").strip()
    space_partition = cfg.get("space_partition", "").strip()
    number_partitions = cfg.get("number_partitions", "").strip()
    migrate_data = _bool(cfg.get("migrate_data"), default=True)
    if_not_exists = _bool(cfg.get("if_not_exists"), default=True)

    stmts = []

    # 1) create_hypertable
    args = [f"'{rel_literal}'", f"by_range('{time_column}', INTERVAL '{chunk_interval}')"]
    create = (
        f"SELECT create_hypertable("
        f"'{rel_literal}', by_range('{time_column}', INTERVAL '{chunk_interval}'), "
        f"migrate_data => {str(migrate_data).lower()}, "
        f"if_not_exists => {str(if_not_exists).lower()});"
    )
    stmts.append((f"创建 hypertable（分区列={time_column}, chunk={chunk_interval}）", create))

    # 1b) 二级空间分区
    if space_partition:
        npart = number_partitions or "4"
        space = (
            f"SELECT add_dimension("
            f"'{rel_literal}', by_hash('{space_partition}', {npart}), "
            f"if_not_exists => true);"
        )
        stmts.append((f"添加空间分区（{space_partition} x {npart}）", space))

    # 2) 列存压缩（填了 segmentby 才开启）
    if segmentby:
        order_clause = orderby or f"{time_column} DESC"
        enable = (
            f"ALTER TABLE {fq} SET ("
            f"timescaledb.compress, "
            f"timescaledb.compress_segmentby = '{segmentby}', "
            f"timescaledb.compress_orderby = '{order_clause}');"
        )
        stmts.append((f"开启列存压缩（segmentby={segmentby}, orderby={order_clause}）", enable))

        # 3) 自动压缩策略
        if compress_after:
            policy = (
                f"SELECT add_compression_policy("
                f"'{rel_literal}', INTERVAL '{compress_after}', if_not_exists => true);"
            )
            stmts.append((f"添加自动压缩策略（{compress_after} 后压缩）", policy))

    return stmts


def main():
    ap = argparse.ArgumentParser(description="把普通表批量转成 TimescaleDB 超表")
    ap.add_argument("tables_file", help="表转换清单文件（INI）")
    ap.add_argument("--env-file", help="数据库配置文件路径（默认同目录 db.env）")
    ap.add_argument("--dry-run", action="store_true", help="只打印 SQL 不执行")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每条 SQL")
    args = ap.parse_args()

    tables = parse_tables(args.tables_file)
    dsn = get_dsn(args.env_file)

    print(f"[*] 共解析到 {len(tables)} 张待转换的表\n")

    if args.dry_run:
        for section, cfg in tables:
            print(f"--- {section} ---")
            for desc, sql in build_statements(section, cfg):
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
                for desc, sql in build_statements(section, cfg):
                    if args.verbose:
                        print(f"    SQL: {sql}")
                    with conn.cursor() as cur:
                        cur.execute(sql)
                    print(f"    ✓ {desc}")
                conn.commit()
                ok += 1
            except psycopg2.Error as e:
                conn.rollback()
                fail += 1
                print(f"    ✗ 失败: {str(e).strip()}")
        print(f"\n[完成] 成功 {ok} 张，失败 {fail} 张。")
    finally:
        conn.close()

    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
