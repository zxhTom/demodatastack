#!/usr/bin/env python3
"""
时序表 vs 普通表 性能基准测试
测试场景：
  1. 批量插入 100 万条数据
  2. 范围查询（最近 1 小时）
  3. 时间聚合查询
  4. 扩展到 1000 万条重复测试
"""
import time
import random
import psycopg2
from psycopg2.extras import execute_batch
from datetime import datetime, timedelta
import argparse
import json

DSN = "host=localhost port=5432 dbname=edumanage user=postgres password=postgres123"

LEVELS   = ["INFO", "WARN", "ERROR", "DEBUG"]
SERVICES = ["auth", "enrollment", "grade", "schedule", "student"]
ACTIONS  = ["login", "create", "update", "delete", "query"]


def generate_batch(n: int, base_time: datetime):
    rows = []
    for i in range(n):
        rows.append((
            base_time - timedelta(seconds=random.randint(0, 86400 * 30)),
            random.choice(LEVELS),
            random.choice(SERVICES),
            random.randint(1, 100),
            random.choice(ACTIONS),
            "student",
            random.randint(1, 1000),
            f"192.168.{random.randint(1,255)}.{random.randint(1,255)}",
            random.randint(10, 5000),
            f"Log message {i}",
            json.dumps({"key": f"val_{i % 100}"}),
        ))
    return rows


def insert_data(conn, table: str, total: int, batch_size: int = 5000):
    base_time = datetime.now()
    inserted = 0
    t0 = time.time()
    with conn.cursor() as cur:
        while inserted < total:
            n = min(batch_size, total - inserted)
            rows = generate_batch(n, base_time)
            cols = "(log_time, level, service, user_id, action, resource_type, resource_id, ip_address, duration_ms, message, metadata)"
            sql = f"INSERT INTO {table} {cols} VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            execute_batch(cur, sql, rows, page_size=batch_size)
            conn.commit()
            inserted += n
            pct = inserted / total * 100
            elapsed = time.time() - t0
            print(f"  [{table}] {inserted:,}/{total:,} ({pct:.0f}%)  {elapsed:.1f}s", end="\r")
    elapsed = time.time() - t0
    print(f"\n  [{table}] 插入完成: {total:,} 条，耗时 {elapsed:.2f}s，速率 {total/elapsed:,.0f} rows/s")
    return elapsed


def run_queries(conn, table: str):
    results = {}
    with conn.cursor() as cur:
        # Q1: 最近 1 小时数据量
        t0 = time.time()
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE log_time > NOW() - INTERVAL '1 hour'")
        cnt = cur.fetchone()[0]
        results["last_1h_count"] = {"rows": cnt, "ms": round((time.time() - t0) * 1000, 2)}

        # Q2: 最近 24 小时按服务分组统计
        t0 = time.time()
        cur.execute(f"""
            SELECT service, level, COUNT(*), AVG(duration_ms)
            FROM {table}
            WHERE log_time > NOW() - INTERVAL '24 hours'
            GROUP BY service, level
            ORDER BY COUNT(*) DESC
        """)
        rows = cur.fetchall()
        results["24h_group_by"] = {"rows": len(rows), "ms": round((time.time() - t0) * 1000, 2)}

        # Q3: 最近 7 天按小时聚合
        t0 = time.time()
        if "ts" in table:
            cur.execute(f"""
                SELECT time_bucket('1 hour', log_time) AS hour, COUNT(*), AVG(duration_ms)
                FROM {table}
                WHERE log_time > NOW() - INTERVAL '7 days'
                GROUP BY hour ORDER BY hour DESC LIMIT 168
            """)
        else:
            cur.execute(f"""
                SELECT date_trunc('hour', log_time) AS hour, COUNT(*), AVG(duration_ms)
                FROM {table}
                WHERE log_time > NOW() - INTERVAL '7 days'
                GROUP BY hour ORDER BY hour DESC LIMIT 168
            """)
        rows = cur.fetchall()
        results["7d_hourly_agg"] = {"rows": len(rows), "ms": round((time.time() - t0) * 1000, 2)}

        # Q4: ERROR 级别 TOP 服务（全表）
        t0 = time.time()
        cur.execute(f"""
            SELECT service, COUNT(*) as cnt FROM {table}
            WHERE level = 'ERROR'
            GROUP BY service ORDER BY cnt DESC
        """)
        rows = cur.fetchall()
        results["error_by_service"] = {"rows": len(rows), "ms": round((time.time() - t0) * 1000, 2)}

    return results


def get_table_size(conn, table: str) -> str:
    with conn.cursor() as cur:
        cur.execute(f"SELECT pg_size_pretty(pg_total_relation_size('{table}'))")
        return cur.fetchone()[0]


def run_benchmark(total_rows: int):
    print(f"\n{'='*60}")
    print(f"  时序表 vs 普通表 性能基准测试")
    print(f"  测试数据量: {total_rows:,} 条")
    print(f"{'='*60}\n")

    conn = psycopg2.connect(DSN)
    conn.autocommit = False

    report = {"total_rows": total_rows, "tables": {}}

    for table in ["system_logs", "system_logs_ts"]:
        print(f"\n[{table}] 清空旧数据...")
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE {table}")
        conn.commit()

        print(f"[{table}] 开始插入 {total_rows:,} 条...")
        insert_time = insert_data(conn, table, total_rows)

        print(f"[{table}] 执行查询测试...")
        query_results = run_queries(conn, table)

        size = get_table_size(conn, table)
        print(f"[{table}] 表大小: {size}")

        report["tables"][table] = {
            "insert_time_s":  round(insert_time, 2),
            "insert_rate_rps": round(total_rows / insert_time),
            "table_size":      size,
            "queries":         query_results,
        }

    conn.close()

    print(f"\n{'='*60}")
    print("  基准测试报告")
    print(f"{'='*60}")
    print(f"\n数据量: {total_rows:,} 条\n")
    for table, data in report["tables"].items():
        print(f"[{table}]")
        print(f"  插入耗时: {data['insert_time_s']}s  速率: {data['insert_rate_rps']:,} rows/s  大小: {data['table_size']}")
        for q, r in data["queries"].items():
            print(f"  {q:<25} {r['ms']:>8.1f} ms  ({r['rows']} rows returned)")
        print()

    print(f"\n对比摘要:")
    t = report["tables"]
    sl  = t.get("system_logs", {})
    sts = t.get("system_logs_ts", {})
    if sl and sts:
        for q in sl["queries"]:
            ms_normal = sl["queries"][q]["ms"]
            ms_ts     = sts["queries"][q]["ms"]
            ratio     = ms_normal / ms_ts if ms_ts > 0 else 0
            faster    = "TimescaleDB" if ms_ts < ms_normal else "PostgreSQL"
            print(f"  {q:<30} 普通={ms_normal:.1f}ms  TimescaleDB={ms_ts:.1f}ms  {faster} 快 {abs(ratio-1)*100:.0f}%")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="时序表基准测试")
    parser.add_argument("--rows", type=int, default=1_000_000, help="测试数据量（默认100万）")
    args = parser.parse_args()
    run_benchmark(args.rows)
