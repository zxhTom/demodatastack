#!/usr/bin/env python3
"""扫描历次转换（convert_to_timescale.py / timescale_migrate_event.py /
timescale_revert_event.py / table_convert.py / transtable.py）留下的
临时表 / 备份表，生成一份待审查的清理 SQL。

本脚本只读，不修改、不删除任何东西。生成的 SQL 文件里所有 DROP TABLE
默认全部用 -- 注释掉，你自己看过、确认要删的表把行首 -- 去掉再手动执行，
不要整段无脑跑。

用法：
    python3 find_leftover_tables.py
    python3 find_leftover_tables.py --env-file db.env --out cleanup_candidates.sql
"""
import argparse
import re
from datetime import datetime

import psycopg2

from dbconfig import get_dsn

# 按脚本来源分类的命名规律。kind 用于分组展示和生成 SQL 时的风险提示。
PATTERNS = [
    ("① 中途残留的临时表（to-hyper / table_convert.py）——正常流程里这类表建好后马上会\n"
     "   被 INSERT+改名 消费掉，能看到说明某次转换中途失败或被中断后没有被自动清理\n"
     "   （下次对同一张表重跑 to-hyper/to-plain 时脚本会自动 DROP 重建，所以留着也无害，\n"
     "   只是占地方）。删之前确认现在没有 table_convert.py 正在处理同一张表。",
     re.compile(r"^(.+)_ts_new$"), "orphan"),
    ("① 中途残留的临时表（to-plain / table_convert.py）——同上，to-plain 方向的残留。",
     re.compile(r"^(.+)_plain_new$"), "orphan"),
    ("② to-hyper 转换成功后的原表备份（改名保留，table_convert.py / timescale_migrate_event.py）\n"
     "   ——对应的现表（去掉后缀）此刻应该已经是转换后的超表。删前建议自己核对一下\n"
     "   现表的行数/关键数据是否符合预期，确认没问题再删。",
     re.compile(r"^(.+)_pg_old$"), "backup"),
    ("② to-plain 转换成功后的原表备份（改名保留，table_convert.py / timescale_revert_event.py）\n"
     "   ——对应的现表此刻应该已经是转换后的普通表。同上，删前自己核对一下。",
     re.compile(r"^(.+)_ts_old$"), "backup"),
    ("③ transtable.py 迁移前的整表快照备份（CREATE TABLE ... AS SELECT，日期后缀）\n"
     "   ——是某个具体时间点的完整快照，不是简单改名，删前确认不再需要回溯这个时间点。",
     re.compile(r"^(.+)_bak_(\d{8})$"), "backup_dated"),
    ("④ 单纯以 _ts 结尾的表——可能是旧版 convert_to_timescale.py（新建对比模式）生成\n"
     "   的并排超表（跟原表对比性能用，不是备份），但也可能只是恰好这么命名的正常业务/\n"
     "   schema 表，跟任何转换脚本都没关系（比如本仓库自己的 demodatastack 项目里，\n"
     "   system_logs_ts 就是 04_timeseries.sql 建库脚本直接建的固定表，专门给 benchmark.py\n"
     "   做性能对比用，从来没经过 convert_to_timescale.py，误删了会破坏项目自带的基准测试）。\n"
     "   ⚠️ 命名规律很宽泛，这一类历史上已经出过至少一次误判，删之前务必逐条确认这张表\n"
     "   到底是不是某次转换的产物，不要看到 _ts 结尾就当成备份表处理。",
     re.compile(r"^(.+)_ts$"), "sibling"),
]


def scan(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname,
                   COALESCE(c.reltuples::bigint, 0) AS approx_rows,
                   pg_size_pretty(pg_total_relation_size(c.oid)) AS size,
                   EXISTS (
                       SELECT 1 FROM timescaledb_information.hypertables h
                       WHERE h.hypertable_name = c.relname
                   ) AS is_hyper
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
            ORDER BY c.relname;
            """
        )
        return cur.fetchall()


def categorize(rows):
    by_name = {r[0]: r for r in rows}
    found = []
    for name, approx_rows, size, is_hyper in rows:
        for label, pattern, kind in PATTERNS:
            m = pattern.match(name)
            if not m:
                continue
            base = m.group(1)
            base_row = by_name.get(base)
            found.append({
                "label": label, "kind": kind, "name": name, "base": base,
                "approx_rows": approx_rows, "size": size, "is_hyper": is_hyper,
                "base_exists": base_row is not None,
                "base_rows": base_row[1] if base_row else None,
                "base_is_hyper": base_row[3] if base_row else None,
            })
            break
    return found


ORDER = ["orphan", "backup", "backup_dated", "sibling"]


def _fmt_rows(n):
    """reltuples 对刚建好、还没 ANALYZE 过的表是 -1（哨兵值），别原样打印成"-1行"。"""
    if n is None or n < 0:
        return "未知(待ANALYZE)"
    return f"{n:,}"


def print_report(found):
    if not found:
        print("没有发现匹配已知命名规律的临时/备份表。")
        return
    total_size_note = "（大小为近似值，来自 pg_total_relation_size；行数为 -1 表示该表还没 ANALYZE 过，行数未知）"
    print(f"共发现 {len(found)} 张疑似临时/备份表 {total_size_note}\n")
    for kind in ORDER:
        items = [f for f in found if f["kind"] == kind]
        if not items:
            continue
        print(f"{'=' * 90}\n{items[0]['label']}\n{'=' * 90}")
        for it in items:
            if it["base_exists"]:
                base_state = "超表" if it["base_is_hyper"] else "普通表"
                base_desc = f"现表 {it['base']}（{base_state}，约 {_fmt_rows(it['base_rows'])} 行）"
            else:
                base_desc = f"现表 {it['base']} 不存在！⚠️ 这种情况先别删，确认清楚原因"
            print(f"  {it['name']:<42} 约 {_fmt_rows(it['approx_rows']):>14} 行  {it['size']:>10}   对应 {base_desc}")
        print()


def generate_sql(found, out_path):
    lines = [
        "-- 临时/备份表清理候选清单（自动生成，只读扫描产生，不代表已确认可删）",
        f"-- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "-- 使用方法：自己逐条看过，确认要删的表，把该行开头的 -- 去掉再手动执行。",
        "-- 不要整段取消注释无脑跑一遍，尤其是第 ④ 类风险提示部分。",
        "",
    ]
    for kind in ORDER:
        items = [f for f in found if f["kind"] == kind]
        if not items:
            continue
        lines.append("")
        # 标签里含换行，每一行都要单独加 -- 前缀，否则中间几行会变成裸文本混进 SQL 文件
        lines += [f"-- {line}" for line in items[0]["label"].split("\n")]
        for it in items:
            base_note = (f"对应现表 {it['base']}"
                         + (f"（{'超表' if it['base_is_hyper'] else '普通表'}，约{_fmt_rows(it['base_rows'])}行）"
                            if it["base_exists"] else "（⚠️ 现表不存在，先别删）"))
            lines.append(f'-- DROP TABLE IF EXISTS "{it["name"]}";  '
                         f'-- 约{_fmt_rows(it["approx_rows"])}行 {it["size"]}，{base_note}')
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"[*] 已生成待审查 SQL: {out_path}")
    print("    全部默认注释，需要你自己去掉 -- 后再执行，本脚本不会替你删任何东西。")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", help="db.env 路径（默认同目录 db.env）")
    p.add_argument("--out", default="cleanup_candidates.sql", help="生成的 SQL 文件路径")
    args = p.parse_args()

    conn = psycopg2.connect(get_dsn(args.env_file))
    conn.autocommit = True
    rows = scan(conn)
    conn.close()

    found = categorize(rows)
    print_report(found)
    if found:
        generate_sql(found, args.out)


if __name__ == "__main__":
    main()
