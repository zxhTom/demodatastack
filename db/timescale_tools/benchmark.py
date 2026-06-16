#!/usr/bin/env python3
"""普通表 vs TimescaleDB 超表 —— 性能对比测试。

会自建两张结构完全相同的表（一张普通表、一张 hypertable），灌入相同的数据，
然后跑同一组查询/写入，统计耗时并输出 Markdown 报告。

用法：
    python3 benchmark.py                       # 默认：只测查询
    python3 benchmark.py --mode insert         # 只测写入
    python3 benchmark.py --mode both           # 查询 + 写入都测
    python3 benchmark.py --rows 2000000 --runs 5 --out report.md

主要选项：
    --mode {query,insert,both}  测试类型，默认 query
    --rows N                    数据规模（行数），默认 1,000,000
    --runs N                    每个查询重复次数取平均，默认 3
    --chunk-interval STR        hypertable 的 chunk 区间，默认 "1 day"
    --segmentby COL             开启压缩并按该列分段（默认 device_id），传空串则不压缩
    --out PATH                  报告输出路径，默认 benchmark_report.md
    --env-file PATH             数据库配置文件（默认同目录 db.env）
    --keep                      测试结束后保留测试表（默认会 DROP）
"""
import argparse
import os
import random
import statistics
import time
from datetime import datetime, timedelta

import psycopg2
from psycopg2.extras import execute_values

from dbconfig import get_dsn

PLAIN_TABLE = "bench_plain"
HYPER_TABLE = "bench_hyper"

DEVICES = [f"device_{i:03d}" for i in range(50)]
REGIONS = ["north", "south", "east", "west"]

DDL = """
CREATE TABLE {tbl} (
    ts          TIMESTAMPTZ      NOT NULL,
    device_id   TEXT             NOT NULL,
    region      TEXT             NOT NULL,
    temperature DOUBLE PRECISION NOT NULL,
    humidity    DOUBLE PRECISION NOT NULL,
    payload     JSONB
);
"""


def log(msg):
    print(msg, flush=True)


def gen_rows(n, base_time):
    """生成 n 行随机时序数据，时间分布在最近 30 天内。"""
    rows = []
    for i in range(n):
        ts = base_time - timedelta(seconds=random.randint(0, 86400 * 30))
        rows.append((
            ts,
            random.choice(DEVICES),
            random.choice(REGIONS),
            round(random.uniform(-10, 40), 2),
            round(random.uniform(0, 100), 2),
            '{"k": %d}' % (i % 100),
        ))
    return rows


def setup_tables(conn, chunk_interval, segmentby):
    log("[*] 重建测试表 ...")
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")
        for tbl in (PLAIN_TABLE, HYPER_TABLE):
            cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE;")
            cur.execute(DDL.format(tbl=tbl))
        # 普通表常规索引（公平起见给个时间索引）
        cur.execute(f"CREATE INDEX ON {PLAIN_TABLE} (ts DESC);")
        # 超表
        cur.execute(
            f"SELECT create_hypertable('{HYPER_TABLE}', "
            f"by_range('ts', INTERVAL '{chunk_interval}'));"
        )
        cur.execute(f"CREATE INDEX ON {HYPER_TABLE} (ts DESC);")
        if segmentby:
            cur.execute(
                f"ALTER TABLE {HYPER_TABLE} SET ("
                f"timescaledb.compress, "
                f"timescaledb.compress_segmentby = '{segmentby}', "
                f"timescaledb.compress_orderby = 'ts DESC');"
            )
    conn.commit()


def load_data(conn, total, batch=10000):
    """给两张表灌入完全相同的数据。返回 (普通表耗时, 超表耗时)。"""
    base_time = datetime.now()
    timings = {PLAIN_TABLE: 0.0, HYPER_TABLE: 0.0}
    inserted = 0
    sql_tpl = (
        "INSERT INTO {tbl} (ts, device_id, region, temperature, humidity, payload) "
        "VALUES %s"
    )
    while inserted < total:
        n = min(batch, total - inserted)
        rows = gen_rows(n, base_time)
        for tbl in (PLAIN_TABLE, HYPER_TABLE):
            with conn.cursor() as cur:
                t0 = time.perf_counter()
                execute_values(cur, sql_tpl.format(tbl=tbl), rows, page_size=batch)
                conn.commit()
                timings[tbl] += time.perf_counter() - t0
        inserted += n
        log(f"    灌数据 {inserted:,}/{total:,}\r")
    with conn.cursor() as cur:
        cur.execute(f"ANALYZE {PLAIN_TABLE};")
        cur.execute(f"ANALYZE {HYPER_TABLE};")
    conn.commit()
    return timings[PLAIN_TABLE], timings[HYPER_TABLE]


def compress_hyper(conn):
    """压缩超表所有 chunk，返回压缩前后总大小（字节）。"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT compress_chunk(c, if_not_compressed => true) "
                "FROM show_chunks(%s) c;",
                (HYPER_TABLE,),
            )
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        return None, None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT before_compression_total_bytes, after_compression_total_bytes "
            "FROM hypertable_compression_stats(%s);",
            (HYPER_TABLE,),
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        return None, None
    return row[0], row[1]


# 查询集：{名称: SQL 模板（用 {tbl} 占位）}
QUERIES = {
    "最近1小时范围扫描": (
        "SELECT count(*) FROM {tbl} WHERE ts > now() - INTERVAL '1 hour';"
    ),
    "最近7天按天聚合均值": (
        "SELECT date_trunc('day', ts) d, avg(temperature) "
        "FROM {tbl} WHERE ts > now() - INTERVAL '7 days' "
        "GROUP BY d ORDER BY d;"
    ),
    "全表按设备分组聚合": (
        "SELECT device_id, count(*), avg(humidity) "
        "FROM {tbl} GROUP BY device_id ORDER BY 2 DESC;"
    ),
    "单设备最近100条明细": (
        "SELECT * FROM {tbl} WHERE device_id = 'device_001' "
        "ORDER BY ts DESC LIMIT 100;"
    ),
    "时间窗口+过滤聚合": (
        "SELECT region, avg(temperature) FROM {tbl} "
        "WHERE ts > now() - INTERVAL '30 days' AND temperature > 20 "
        "GROUP BY region;"
    ),
}


def time_query(conn, sql, runs):
    """跑 runs 次取平均耗时（毫秒），返回 (平均, 最快)。"""
    samples = []
    for _ in range(runs):
        with conn.cursor() as cur:
            t0 = time.perf_counter()
            cur.execute(sql)
            cur.fetchall()
            samples.append((time.perf_counter() - t0) * 1000)
    return statistics.mean(samples), min(samples)


def run_query_bench(conn, runs):
    results = []
    log("[*] 执行查询测试 ...")
    for name, tpl in QUERIES.items():
        p_avg, p_min = time_query(conn, tpl.format(tbl=PLAIN_TABLE), runs)
        h_avg, h_min = time_query(conn, tpl.format(tbl=HYPER_TABLE), runs)
        speedup = p_avg / h_avg if h_avg else float("inf")
        results.append({
            "name": name, "plain": p_avg, "hyper": h_avg,
            "plain_min": p_min, "hyper_min": h_min, "speedup": speedup,
        })
        log(f"    {name}: 普通 {p_avg:.1f}ms / 超表 {h_avg:.1f}ms (x{speedup:.2f})")
    return results


def run_insert_bench(conn, rows, runs):
    """重复 runs 轮：每轮往两张表各插入 rows 行，比较写入吞吐。"""
    log("[*] 执行写入测试 ...")
    base_time = datetime.now()
    sql_tpl = (
        "INSERT INTO {tbl} (ts, device_id, region, temperature, humidity, payload) "
        "VALUES %s"
    )
    agg = {PLAIN_TABLE: [], HYPER_TABLE: []}
    for r in range(runs):
        data = gen_rows(rows, base_time)
        for tbl in (PLAIN_TABLE, HYPER_TABLE):
            with conn.cursor() as cur:
                t0 = time.perf_counter()
                execute_values(cur, sql_tpl.format(tbl=tbl), data, page_size=10000)
                conn.commit()
                dt = time.perf_counter() - t0
            agg[tbl].append(dt)
        log(f"    第 {r + 1}/{runs} 轮完成")
    p_avg = statistics.mean(agg[PLAIN_TABLE])
    h_avg = statistics.mean(agg[HYPER_TABLE])
    return {
        "rows": rows,
        "plain_sec": p_avg, "hyper_sec": h_avg,
        "plain_tps": rows / p_avg, "hyper_tps": rows / h_avg,
    }


def fmt_bytes(n):
    if n is None:
        return "N/A"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def write_report(path, meta, query_res, insert_res, size_info):
    lines = []
    lines.append("# 普通表 vs TimescaleDB 超表 性能对比报告\n")
    lines.append(f"- 生成时间：{meta['time']}")
    lines.append(f"- 数据规模：{meta['rows']:,} 行")
    lines.append(f"- chunk 区间：{meta['chunk_interval']}")
    lines.append(f"- 压缩 segmentby：{meta['segmentby'] or '（未开启）'}")
    lines.append(f"- 每项重复取平均：{meta['runs']} 次")
    lines.append(f"- 测试模式：{meta['mode']}\n")

    if size_info and size_info[0] is not None:
        before, after = size_info
        ratio = before / after if after else float("inf")
        lines.append("## 存储 / 压缩\n")
        lines.append("| 指标 | 数值 |")
        lines.append("| --- | --- |")
        lines.append(f"| 压缩前大小 | {fmt_bytes(before)} |")
        lines.append(f"| 压缩后大小 | {fmt_bytes(after)} |")
        lines.append(f"| 压缩比 | {ratio:.2f}x |\n")

    if query_res:
        lines.append("## 查询性能（耗时越低越好）\n")
        lines.append("| 查询 | 普通表 平均(ms) | 超表 平均(ms) | 超表最快(ms) | 提速 |")
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        for r in query_res:
            lines.append(
                f"| {r['name']} | {r['plain']:.1f} | {r['hyper']:.1f} | "
                f"{r['hyper_min']:.1f} | x{r['speedup']:.2f} |"
            )
        avg_speed = statistics.mean(r["speedup"] for r in query_res)
        lines.append(f"\n> 平均提速：**x{avg_speed:.2f}**（>1 表示超表更快）\n")

    if insert_res:
        lines.append("## 写入性能（吞吐越高越好）\n")
        lines.append("| 指标 | 普通表 | 超表 |")
        lines.append("| --- | ---: | ---: |")
        lines.append(
            f"| 单轮插入 {insert_res['rows']:,} 行耗时(s) | "
            f"{insert_res['plain_sec']:.2f} | {insert_res['hyper_sec']:.2f} |"
        )
        lines.append(
            f"| 吞吐(行/秒) | {insert_res['plain_tps']:,.0f} | "
            f"{insert_res['hyper_tps']:,.0f} |"
        )
        lines.append("")

    lines.append("## 说明\n")
    lines.append("- 两张表结构、索引、数据完全一致，唯一差别是超表做了时间分区（及可选压缩）。")
    lines.append("- 数据量越大、时间范围查询越多，超表优势越明显；小数据集差距可能不明显甚至持平。")
    lines.append("- 写入测试中超表因为要路由 chunk，单条/小批量可能略慢，但大规模并发写入更稳定。")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    log(f"\n[完成] 报告已写入: {path}")


def create_temp_db(dsn, name):
    """在维护库(postgres)里创建一个全新的临时库，避开 FOR ALL TABLES 发布等限制。"""
    conn = psycopg2.connect(dsn, dbname="postgres")
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}";')
            cur.execute(f'CREATE DATABASE "{name}";')
    finally:
        conn.close()


def drop_temp_db(dsn, name):
    conn = psycopg2.connect(dsn, dbname="postgres")
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # 先踢掉残留连接，再删库
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid();",
                (name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{name}";')
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="普通表 vs TimescaleDB 超表 性能对比")
    ap.add_argument("--mode", choices=["query", "insert", "both"], default="query")
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--chunk-interval", default="1 day")
    ap.add_argument("--segmentby", default="device_id",
                    help="压缩分段列，传空串 '' 则不开压缩")
    ap.add_argument("--out", default="benchmark_report.md")
    ap.add_argument("--env-file")
    ap.add_argument("--db", help="测试用的临时库名（默认 bench_tmp_<pid>，跑完自动删除）")
    ap.add_argument("--keep", action="store_true", help="结束后保留临时库与测试表")
    args = ap.parse_args()

    dsn = get_dsn(args.env_file)
    tmp_db = args.db or f"bench_tmp_{os.getpid()}"

    log(f"[*] 创建隔离测试库: {tmp_db}（与你的业务库/CDC 完全隔离）")
    create_temp_db(dsn, tmp_db)
    conn = psycopg2.connect(dsn, dbname=tmp_db)

    size_info = None
    try:
        setup_tables(conn, args.chunk_interval, args.segmentby)
        log(f"[*] 灌入基准数据 {args.rows:,} 行 ...")
        load_data(conn, args.rows)

        # 开启压缩时顺便测压缩比
        if args.segmentby:
            size_info = compress_hyper(conn)

        query_res = run_query_bench(conn, args.runs) if args.mode in ("query", "both") else None
        insert_rows = max(10000, args.rows // 10)
        insert_res = run_insert_bench(conn, insert_rows, args.runs) if args.mode in ("insert", "both") else None

        meta = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "rows": args.rows, "runs": args.runs,
            "chunk_interval": args.chunk_interval,
            "segmentby": args.segmentby, "mode": args.mode,
        }
        write_report(args.out, meta, query_res, insert_res, size_info)
    finally:
        conn.close()
        if not args.keep:
            drop_temp_db(dsn, tmp_db)
            log(f"[*] 已删除临时库 {tmp_db}（如需保留请加 --keep）")
        else:
            log(f"[*] 已保留临时库 {tmp_db}")


if __name__ == "__main__":
    main()
