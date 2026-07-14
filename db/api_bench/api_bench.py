#!/usr/bin/env python3
"""HES 三大类查询接口性能压测（曲线 / 事件+告警 / 通讯日志）。

对应 db/req.md 的需求，分三个阶段，可以分开跑也可以一条命令全跑：

  stats    统计三大类共 23 张表的真实数据量（逐表明细 + 三大类合计/平均，纯 PG 口径，
           转超表前后可对比）
  meters   分析 curve/event 各自"命中数据多"的活跃电表（按数据量排序），写入
           curve_meters.txt / event_meters.txt。配置里 meter_pool = file:xxx 引用，
           让每次随机抽的都是有数据的电表，接口耗时才测得准（不然抽到空电表秒回）。
           分析用的时间窗口就是各模块 [段] 里配的 time_start~time_end（与压测一致）
  bench    对 4 个接口做随机压测：你在配置文件里为每个接口指定「测试集」——
           一个电表集合（meter_pool）+ 一个时间范围（time_start~time_end），
           每一轮从这个集合里随机抽若干电表、在这个时间范围内随机取一段子窗口，
           组成请求参数去压测。抽多少电表、子窗口多宽、跑多少轮都可配。
             曲线/事件：随机电表 + 随机时间子窗口
             告警：随机时间子窗口（无电表参数）
             通讯日志：参数固定，重复请求测基线
           只测配置文件里出现了对应 [段] 的接口——不想测某个接口就不写它的段。
  report   汇总 stats + bench 的结果生成 Markdown 报告
  compare  给两份结果（--before before.json --after after.json）生成转换前后性能对比报告
  all      stats → bench → report 一次跑完

结果文件（-o，默认 bench_results.json）里保存了【每一轮的完整请求参数】，所以可以
转 TimescaleDB 前后用【完全相同的请求】对比：
  转换前： python3 api_bench.py bench -o before.json
  （转成 TimescaleDB）
  转换后： python3 api_bench.py bench --replay before.json -o after.json   # 同参数复测
  出报告： python3 api_bench.py report -o after.json                        # 生成 after.md
before.json / after.json 逐轮一一对应，直接比首次(冷)耗时即可，最有说服力。

用法：
  cp bench.example.ini bench.ini   # 填 base_url、认证头、各接口的测试集
  python3 api_bench.py stats                 # 只统计数据量（不打接口）
  python3 api_bench.py all                    # 完整跑一轮并出报告
  python3 api_bench.py bench -o before.json   # 结果（含每轮请求参数）存到指定文件
  python3 api_bench.py bench --replay before.json -o after.json   # 用上次的请求同参数复测
  python3 api_bench.py bench -v               # 打印请求参数+响应+一键curl+每次耗时
"""
import argparse
import configparser
import json
import os
import random
import socket
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import psycopg2

import dbconfig

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(_HERE, "bench.ini")
STATS_FILE = os.path.join(_HERE, "stats.json")
RESULTS_FILE = os.path.join(_HERE, "bench_results.json")
REPORT_FILE = os.path.join(_HERE, "REPORT.md")

# 三大类表清单与各表时间列（与 timescale_tools/tables.ini 的 group 划分一致）
TABLES = {
    "curve": {
        "d_load_voltage": "data_date",   "d_load_current": "data_date",
        "d_load_power": "data_date",     "d_load_power_r": "data_date",
        "d_load_power_v": "data_date",   "d_load_instant": "data_date",
        "d_load_angle": "data_date",     "d_load_status": "data_date",
        "d_read_curve": "data_date",     "d_read_curve_r": "data_date",
        "d_read_curve_v": "data_date",   "d_demand_curve": "data_date",
    },
    "event": {
        "d_alarm_event": "data_date",
        "d_communication_event_log": "event_time",
        "d_config_modification_event_log": "event_time",
        "d_disconnector_event_log": "event_time",
        "d_fraud_event_log": "event_time",
        "d_power_failure_event_log": "event_time",
        "d_power_quality_event_log": "event_time",
        "d_recharge_event_log": "event_time",
        "d_special_event_log": "event_time",
        "d_standard_event_log": "event_time",
    },
    "log": {
        "sys_fep_comm_log": "start_time",
    },
}
CATEGORY_CN = {"curve": "曲线", "event": "事件", "log": "通讯日志"}


# ── 配置 ─────────────────────────────────────────────────────────────────────

def load_bench_config(path):
    # 允许行内注释（值后面跟 # 注释），比如 rounds = 20  # 说明。
    # 只用 # 不用 ;——Cookie 值里常含 ; 分隔，不能当注释。
    p = configparser.ConfigParser(inline_comment_prefixes=("#",))
    # 认证头的 key 大小写必须原样保留（Authorization / Cookie）
    p.optionxform = str
    if not p.read(path, encoding="utf-8"):
        sys.exit(f"[错误] 读不到配置文件: {path}（可复制 bench.example.ini 创建）")
    if not p.has_option("api", "base_url"):
        sys.exit("[错误] 配置缺少 [api] base_url")

    headers = dict(p.items("headers")) if p.has_section("headers") else {}
    headers.setdefault("Content-Type", "application/json")
    # 对齐 curl 的默认行为，避免"curl 能过、脚本被网关拦"的假失败：
    # urllib 默认 UA 是 Python-urllib，且不发 Accept，有些网关/WAF 会据此区别对待。
    headers.setdefault("User-Agent", "curl/8.4.0")
    headers.setdefault("Accept", "*/*")

    # 四个接口共用的公共前缀（网关 context path），默认 /hes-web-api。
    # 规整成前面带 /、后面不带 / 的形式；填空串表示不加前缀。
    context_path = p.get("api", "context_path", fallback="/hes-web-api").strip()
    if context_path and not context_path.startswith("/"):
        context_path = "/" + context_path
    context_path = context_path.rstrip("/")

    # 全局默认（各接口段可覆盖）
    defaults = {
        "rounds": p.getint("bench", "rounds", fallback=20),
        "repeat": p.getint("bench", "repeat", fallback=3),
        "meters_per_round": _parse_range(p.get("bench", "meters_per_round", fallback="2-20")),
        "window_days": _parse_range(p.get("bench", "window_days", fallback="1-7")),
    }
    seed = p.getint("bench", "seed", fallback=42)
    delay = p.getfloat("bench", "delay", fallback=0.0)  # 每次请求之间的间隔秒数，缓解"猛打打满后端"

    # 每个接口一个测试集，只测配置里出现了对应 [段] 的接口
    specs = {
        "curve":   {"needs_meters": True,  "needs_time": True},
        "event":   {"needs_meters": True,  "needs_time": True},
        "alarm":   {"needs_meters": False, "needs_time": True},
        "commlog": {"needs_meters": False, "needs_time": False},
    }
    interfaces = {}
    for name, spec in specs.items():
        if not p.has_section(name):
            continue
        conf = dict(defaults)
        conf["rounds"] = p.getint(name, "rounds", fallback=conf["rounds"])
        conf["repeat"] = p.getint(name, "repeat", fallback=conf["repeat"])
        if p.has_option(name, "meters_per_round"):
            conf["meters_per_round"] = _parse_range(p.get(name, "meters_per_round"))
        if p.has_option(name, "window_days"):
            conf["window_days"] = _parse_range(p.get(name, "window_days"))
        if spec["needs_time"]:
            for key in ("time_start", "time_end"):
                if not p.has_option(name, key):
                    sys.exit(f"[错误] [{name}] 缺少 {key}（该接口需要指定时间范围测试集）")
            conf["time_start"] = _parse_dt(p.get(name, "time_start"))
            conf["time_end"] = _parse_dt(p.get(name, "time_end"))
            if conf["time_start"] >= conf["time_end"]:
                sys.exit(f"[错误] [{name}] time_start 必须早于 time_end")
        if spec["needs_meters"]:
            conf["meter_pool_spec"] = p.get(name, "meter_pool", fallback="auto:50")
        if name == "curve":
            conf["group_ids"] = [int(x) for x in p.get(name, "group_ids",
                                                         fallback="40930000011").split(",")]
        conf.update(spec)
        interfaces[name] = conf

    if not interfaces:
        sys.exit("[错误] 配置里没有任何接口测试集段（[curve]/[event]/[alarm]/[commlog]），"
                 "至少写一个才能压测")

    return {
        "base_url": p.get("api", "base_url").rstrip("/"),
        "context_path": context_path,
        "timeout": p.getfloat("api", "timeout", fallback=60.0),
        "headers": headers,
        "seed": seed,
        "delay": delay,
        "interfaces": interfaces,
    }


def _parse_range(s):
    """'2-20' → (2, 20)；'10' → (10, 10)。用于随机区间的上下界。"""
    s = str(s).strip()
    if "-" in s:
        lo, hi = s.split("-", 1)
        lo, hi = int(lo), int(hi)
    else:
        lo = hi = int(s)
    if lo > hi:
        lo, hi = hi, lo
    return (max(1, lo), max(1, hi))


def _parse_dt(s):
    """接受 'YYYY-MM-DD HH:MM:SS' 或 'YYYY-MM-DD'。"""
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    sys.exit(f"[错误] 时间格式无法识别: {s!r}（用 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM:SS'）")


# ── stats：数据量统计 ─────────────────────────────────────────────────────────

def stats_cmd(args):
    dsn = dbconfig.get_dsn(args.env_file)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()

    stats = {"generated_at": datetime.now().isoformat(timespec="seconds"),
             "exact": not args.approx, "categories": {}}
    print(f"==> 统计三大类数据量（{'近似值 reltuples' if args.approx else '精确 COUNT(*)，大表会慢'}）")

    for cat, tables in TABLES.items():
        cat_data = {"tables": {}, "total": 0}
        t_min, t_max = None, None
        for table, time_col in tables.items():
            try:
                if args.approx:
                    # 纯 PostgreSQL 近似计数：沿继承链把该表自身 + 所有子表的
                    # pg_class.reltuples 相加，不依赖任何 TimescaleDB 函数。
                    # 普通表没有子表 → 就是它自己的估算值；转成超表后数据落在
                    # 继承自父表的 chunk 里 → 把各 chunk 的估算值汇总。转换前后
                    # 用的是同一套逻辑，口径一致，可直接对比。
                    # 估算值来自 ANALYZE/autovacuum；刚转换完的超表建议先 ANALYZE。
                    cur.execute(
                        "WITH RECURSIVE inh AS ("
                        "  SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
                        "  WHERE n.nspname='public' AND c.relname=%s"
                        "  UNION ALL"
                        "  SELECT i.inhrelid FROM pg_inherits i JOIN inh ON i.inhparent=inh.oid"
                        ") SELECT COALESCE(sum(GREATEST(c.reltuples,0)),0)::bigint"
                        "  FROM inh JOIN pg_class c ON c.oid=inh.oid;", (table,))
                    n = int(cur.fetchone()[0])
                else:
                    cur.execute(f'SELECT count(*) FROM "{table}";')
                    n = cur.fetchone()[0]
                cur.execute(f'SELECT min("{time_col}"), max("{time_col}") FROM "{table}";')
                mn, mx = cur.fetchone()
            except psycopg2.Error as e:
                conn.rollback()
                print(f"  [跳过] {table}: {str(e).strip().splitlines()[0]}")
                cat_data["tables"][table] = {"rows": None, "error": str(e).strip().splitlines()[0]}
                continue
            cat_data["tables"][table] = {"rows": n, "time_column": time_col,
                                          "min_time": str(mn) if mn else None,
                                          "max_time": str(mx) if mx else None}
            cat_data["total"] += n
            if mn is not None:
                t_min = mn if t_min is None or mn < t_min else t_min
            if mx is not None:
                t_max = mx if t_max is None or mx > t_max else t_max
            print(f"  {table:<36} {n:>14,} 行   [{mn} ~ {mx}]")
        ok_tables = [t for t in cat_data["tables"].values() if t.get("rows") is not None]
        cat_data["table_count"] = len(ok_tables)
        cat_data["avg"] = round(cat_data["total"] / len(ok_tables)) if ok_tables else 0
        cat_data["time_min"] = str(t_min) if t_min else None
        cat_data["time_max"] = str(t_max) if t_max else None
        stats["categories"][cat] = cat_data
        print(f"  ── {CATEGORY_CN[cat]}类小计: {cat_data['total']:,} 行 / {len(ok_tables)} 张表，"
              f"平均 {cat_data['avg']:,} 行/表\n")

    # 压测 meterIds 用的真实 c_meter ID
    max_batch = 100
    cur.execute("SELECT meter_id FROM c_meter ORDER BY meter_id LIMIT %s;", (max_batch,))
    stats["meter_ids"] = [int(r[0]) for r in cur.fetchall()]
    conn.close()

    with open(STATS_FILE, "w", encoding="utf-8") as fh:
        json.dump(stats, fh, ensure_ascii=False, indent=2)
    print(f"✓ 统计结果已写入 {STATS_FILE}（bench 阶段会用其中的时间范围/meter ID 生成参数）")
    return stats


# ── meters：分析各类的活跃电表（命中数据多的 meterId） ────────────────────────
# 只有 curve/event 接口按 meterIds 过滤，所以只分析这两类。每类用一张代表表按
# mp_id 统计行数、再经 r_mp 映射回 meter_id：
#   curve → d_load_voltage（12 张曲线表都是同一批 mp_id 上报，取一张即代表活跃度）
#   event → d_standard_event_log（每个在线表都会定期上报标准事件，最能代表"活跃"）
# d_alarm_event 按 device_id 存，不用它做代表。
METER_REP = {
    "curve": ("d_load_voltage", "data_date"),
    "event": ("d_standard_event_log", "event_time"),
}
METERS_FILE = {"curve": os.path.join(_HERE, "curve_meters.txt"),
               "event": os.path.join(_HERE, "event_meters.txt")}


def meters_cmd(args):
    """扫库找出 curve/event 各自命中数据的活跃电表，按数据量排序写入文件，
    供 bench 用 meter_pool = file:xxx_meters.txt 引用。"""
    cfg = load_bench_config(args.config)
    conn = psycopg2.connect(dbconfig.get_dsn(args.env_file))
    conn.autocommit = True
    cur = conn.cursor()

    for cat in ("curve", "event"):
        conf = cfg["interfaces"].get(cat)
        if not conf:
            print(f"[跳过] 配置里没有 [{cat}] 段")
            continue
        table, tcol = METER_REP[cat]
        start, end = conf["time_start"], conf["time_end"]
        # 默认扫【整个配置时间窗口】——必须和压测实际用的时间范围一致，否则筛出的
        # 活跃电表在测试查询的时间段里未必有数据。--sample-days N 是提速快捷方式，
        # 只扫末尾 N 天，但那样分析窗口就和测试窗口不一致了，仅在你明确要提速时用。
        scan_start = start
        note = f"整个配置时间窗口（与压测一致）"
        if args.sample_days and (end - start).days > args.sample_days:
            scan_start = end - timedelta(days=args.sample_days)
            note = f"⚠只扫末尾 {args.sample_days} 天（提速，但与测试窗口不一致）"
        print(f"\n==> 分析{CATEGORY_CN[cat]}活跃电表：代表表 {table}，扫描 {note} "
              f"[{scan_start:%Y-%m-%d} ~ {end:%Y-%m-%d}] ...", flush=True)
        t0 = time.time()
        limit = f"LIMIT {args.top}" if args.top else ""
        cur.execute(
            f"SELECT rmp.meter_id, count(*) AS cnt "
            f'FROM "{table}" v JOIN r_mp rmp ON rmp.mp_id = v.mp_id AND rmp.is_delete=%s '
            f'WHERE v."{tcol}" >= %s AND v."{tcol}" < %s '
            f"GROUP BY rmp.meter_id ORDER BY cnt DESC {limit};",
            ("01", scan_start, end))
        rows = [(int(m), int(c)) for m, c in cur.fetchall()]
        elapsed = time.time() - t0

        if not rows:
            print(f"  ⚠ 这段时间没有任何{CATEGORY_CN[cat]}数据，跳过（换个时间范围？）")
            continue
        outfile = METERS_FILE[cat]
        with open(outfile, "w", encoding="utf-8") as fh:
            fh.write(f"# {CATEGORY_CN[cat]}活跃电表（命中数据的 meter_id，按数据量降序）\n")
            fh.write(f"# 代表表 {table}，扫描区间 {scan_start:%Y-%m-%d}~{end:%Y-%m-%d}，"
                     f"生成于 {datetime.now():%Y-%m-%d %H:%M}\n")
            fh.write(f"# 用法：在 bench.ini 的 [{cat}] 段写 meter_pool = file:{os.path.basename(outfile)}\n")
            fh.write("# meter_id  数据量\n")
            for m, c in rows:
                fh.write(f"{m}\t{c}\n")
        counts = [c for _, c in rows]
        print(f"  ✓ {len(rows)} 个活跃电表（用时 {elapsed:.0f}s），已写入 {os.path.basename(outfile)}")
        print(f"    数据量：最多 {counts[0]:,}、最少 {counts[-1]:,}、中位 {counts[len(counts)//2]:,} 行/电表")
        print(f"    命中最多的 Top5 meter_id：" +
              "，".join(f"{m}({c:,})" for m, c in rows[:5]))
        print(f"    → 在 bench.ini 的 [{cat}] 段把 meter_pool 改成："
              f" meter_pool = file:{os.path.basename(outfile)}")
    conn.close()


# ── bench：接口多轮压测 ───────────────────────────────────────────────────────

def _timeout_result(cfg, t0):
    return {"ok": False, "status": None, "ms": (time.time() - t0) * 1000,
            "error": f"请求超时（超过 {cfg['timeout']:.0f}s 未返回）——查询可能本身就慢"
                     f"（这正是压测要暴露的问题，尤其转超表前的普通表），或后端无响应。"
                     f"curl 默认不设超时所以你手动跑不报错、只是会一直等。"
                     f"想让它跑完看真实耗时就调大 [api] timeout"}


def _http_post(cfg, path, payload):
    url = cfg["base_url"] + path
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=cfg["headers"], method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
            raw = resp.read()
            elapsed = (time.time() - t0) * 1000
            status = resp.status
    except urllib.error.HTTPError as e:
        # 带上响应体片段，HES 通常在这里返回具体原因（如"token 失效"/"无权限"）
        try:
            snippet = e.read().decode("utf-8", "ignore").strip().replace("\n", " ")[:400]
        except Exception:
            snippet = ""
        reason = {401: "认证头无效或已过期", 403: "无权限", 404: "接口路径不存在",
                  406: "Accept 头不被接受", 415: "Content-Type 不被接受",
                  500: "后端处理报错"}.get(e.code, "")
        msg = f"HTTP {e.code}" + (f"（{reason}）" if reason else "")
        if snippet:
            msg += f" | 响应: {snippet[:160]}"
        return {"ok": False, "status": e.code, "ms": (time.time() - t0) * 1000,
                "error": msg, "resp_snippet": snippet}
    except (TimeoutError, socket.timeout):
        return _timeout_result(cfg, t0)
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            return _timeout_result(cfg, t0)
        return {"ok": False, "status": None, "ms": (time.time() - t0) * 1000,
                "error": f"连接失败: {e.reason}（检查 base_url 是否可达）"}
    except Exception as e:
        return {"ok": False, "status": None, "ms": (time.time() - t0) * 1000,
                "error": f"{type(e).__name__}: {e}"}
    total = None
    try:
        data = json.loads(raw)
        # 兼容常见分页返回结构，尽力提取总条数
        for probe in (("total",), ("data", "total"), ("data", "totalCount"),
                      ("result", "total"), ("body", "total")):
            node = data
            for key in probe:
                node = node.get(key) if isinstance(node, dict) else None
                if node is None:
                    break
            if isinstance(node, (int, float)):
                total = int(node)
                break
    except Exception:
        pass
    snippet = raw.decode("utf-8", "ignore").strip().replace("\n", " ")[:600]
    return {"ok": 200 <= status < 300, "status": status, "ms": elapsed,
            "total": total, "resp_snippet": snippet}


def _curl_hint(cfg, url, payload):
    hdrs = " ".join(f"-H '{k}: {v}'" for k, v in cfg["headers"].items())
    body = json.dumps(payload, ensure_ascii=False)
    # -w 让 curl 自己打印总耗时，方便和脚本报的时间对比（覆盖 DNS/连接/传输全程）
    timing = "\\n⏱ curl 实测: DNS %{time_namelookup}s 连接 %{time_connect}s 总耗时 %{time_total}s\\n"
    return f"curl -sS -w '{timing}' -X POST '{url}' {hdrs} -d '{body}'"


def _run_round(cfg, name, path, payload, repeat, desc, dumped=None):
    """同一组参数重复 repeat 次，汇总时延分布。
    dumped：已打印过完整请求详情的接口集合——每个接口只在第一次失败时打完整
    详情（接口/参数/curl），后续同接口失败只打简短原因，避免刷屏。"""
    url = cfg["base_url"] + path
    delay = cfg.get("delay", 0.0)
    results = []
    for _ in range(repeat):
        if delay:
            time.sleep(delay)   # 请求间隔，避免连续猛打把后端打满（503）
        results.append(_http_post(cfg, path, payload))
    lat = sorted(r["ms"] for r in results)
    ok = sum(1 for r in results if r["ok"])
    totals = [r["total"] for r in results if r.get("total") is not None]
    round_result = {
        "round": desc, "repeat": repeat, "success": ok,
        # 首次(冷)耗时：第一次请求，未命中后端缓存，最接近"实际操作"的真实时间
        "cold_ms": round(results[0]["ms"], 1),
        "avg_ms": round(statistics.mean(lat), 1),
        "p50_ms": round(lat[len(lat) // 2], 1),
        "p95_ms": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1),
        "max_ms": round(lat[-1], 1),
        "resp_total": totals[0] if totals else None,
        "errors": sorted({r.get("error") for r in results if not r["ok"]}),
        # 每轮的完整请求都留底（不只失败轮）：request_path 是去掉 base_url 的路径，
        # 回放（--replay）时用它 + 当前 base_url 重新发同样的请求，做转换前后对比。
        "request_url": url,
        "request_path": path,
        "request_payload": payload,
    }
    tag = "✓" if ok == repeat else f"✗ {ok}/{repeat}"
    tail = (f"  命中 {round_result['resp_total']:,} 条" if round_result["resp_total"] is not None else "")
    # 摘要同时给【首次(冷)】和【重复均值】：冷=贴近实际操作，均值=后端缓存后的热查询。
    # 两者差得多，说明后端有缓存——你实际感觉慢，看首次(冷)那个数。
    print(f"  [{tag}] {desc}  首次(冷)={round_result['cold_ms']:.0f}ms  "
          f"重复均值={round_result['avg_ms']:.0f}ms p95={round_result['p95_ms']:.0f}ms{tail}")

    verbose = cfg.get("verbose")
    # -v/--verbose：每次请求的单独耗时——首次明显慢、后续快 = 后端有缓存，
    # 平均值会被"缓存命中"的快请求拉低，看着比实际操作快就是这个原因。
    if verbose and repeat > 1:
        per = "  ".join(f"{r['ms']:.0f}" for r in results)
        print(f"        各次耗时(ms): {per}   ← 若首次明显慢于后续，多半是后端缓存")

    # 完整调试信息：请求参数 + 响应 + 一键 curl（带计时）。
    # -v 时每轮都打；不加 -v 时，某接口首次失败也打一次（帮定位），之后只打简短原因。
    fail_first = ok != repeat and (dumped is None or name not in dumped)
    if verbose or fail_first:
        if fail_first and dumped is not None:
            dumped.add(name)
        resp = next((r.get("resp_snippet") for r in results if r.get("resp_snippet")), "")
        print(f"        请求参数: {json.dumps(payload, ensure_ascii=False)}")
        print(f"        响应内容: {resp if resp else '（空）'}")
        print(f"        一键复现: {_curl_hint(cfg, url, payload)}")
    if ok != repeat:
        why = "；".join(str(e) for e in round_result["errors"]) or "未知错误"
        print(f"        ✗ 失败原因: {why}")
    return round_result


def _random_window(rng, t_start, t_end, days):
    """在 [t_start, t_end] 内随机取一段跨度约 days 天的子窗口（按天对齐）。"""
    total = (t_end.date() - t_start.date()).days
    if days >= total:
        return (datetime.combine(t_start.date(), datetime.min.time()),
                datetime.combine(t_end.date(), datetime.min.time()))
    off = rng.randint(0, total - days)
    ws = datetime.combine(t_start.date(), datetime.min.time()) + timedelta(days=off)
    we = ws + timedelta(days=days)
    return ws, we


def _fmt(dt, kind):
    if kind == "iso":
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if kind == "start":
        return dt.strftime("%Y-%m-%d 00:00:00")
    return dt.strftime("%Y-%m-%d 23:59:59")


def _curve_payload(group_ids, start, end, meter_ids):
    return {
        "pageNum": 1, "pageSize": 100, "sortFields": [], "orgNo": "100",
        "grpId": None, "grpType": None, "lineId": None, "tmnlId": None,
        "meterId": None, "serialNo": None,
        "groupIds": group_ids,                             # 按需求保持不变
        "rawType": "06", "rangePicker": None,
        "ranges": [_fmt(start, "iso"), _fmt(end, "iso")],
        "startTime": _fmt(start, "start"), "endTime": _fmt(end, "end"),
        "meterIds": meter_ids, "defaultGroup": "__93%", "ntmnlTypeCode": "01",
    }


def _event_payload(start, end, meter_ids):
    return {
        "pageNum": 1, "pageSize": 100, "sortFields": [],
        "groupIds": [],                                     # 按需求保持不变
        "rawType": "05",
        "ranges": [_fmt(start, "iso"), _fmt(end, "iso")],
        "startTime": _fmt(start, "start"), "endTime": _fmt(end, "end"),
        "meterIds": meter_ids,
    }


def _alarm_payload(start, end):
    return {
        "pageNum": 1, "pageSize": 100,
        "conditions": [{"fieldKey": "last_occurrence_time", "fieldType": "Date",
                        "operator": "between",
                        "values": [_fmt(start, "start"), _fmt(end, "end")],
                        "fieldUnit": ""}],
    }


# 各接口路径统一不含公共前缀，前缀由 [api] context_path 提供（默认 /hes-web-api），
# 四个接口共用，避免"有的加了前缀有的忘了"。最终 URL = base_url + context_path + 下面的路径。
INTERFACES = {
    "curve":   "/api/profile/profileLog/page?BACKTIME=METER_TMNL_PROFILE_PAGE",
    "event":   "/api/event/eventLog/page?BACKTIME=METER_TMNL_EVENT_PAGE",
    "alarm":   "/api/meters/alarmEnventDeviceListPage?BACKTIME=EVENT_ALARM_QUERY",
    "commlog": "/api/communication/listPage?BACKTIME=COMMUNICATION_LOG_PAGE",
}


def _resolve_pool(spec, args):
    """把 meter_pool 配置解析成一组真实电表 ID。三种写法：
    'auto:N'          → 从 c_meter 取前 N 个真实 ID（不保证有数据）
    'file:xxx.txt'    → 从文件读（api_bench.py meters 生成的活跃电表清单，每行一个 ID，
                        可带第二列数据量，只取第一列；# 开头为注释）
    '14232,15833,...' → 显式列表
    """
    spec = spec.strip()
    if spec.lower().startswith("auto:"):
        n = int(spec.split(":", 1)[1])
        conn = psycopg2.connect(dbconfig.get_dsn(args.env_file))
        cur = conn.cursor()
        cur.execute("SELECT meter_id FROM c_meter ORDER BY meter_id LIMIT %s;", (n,))
        ids = [int(r[0]) for r in cur.fetchall()]
        conn.close()
        if not ids:
            sys.exit("[错误] c_meter 里查不到任何电表 ID")
        return ids
    if spec.lower().startswith("file:"):
        fpath = spec.split(":", 1)[1].strip()
        if not os.path.isabs(fpath):
            fpath = os.path.join(_HERE, fpath)
        if not os.path.isfile(fpath):
            sys.exit(f"[错误] meter_pool 文件不存在: {fpath}"
                     f"（先跑 `api_bench.py meters` 生成活跃电表清单）")
        ids = []
        for line in open(fpath, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ids.append(int(line.split()[0]))   # 允许 "meter_id  数据量" 两列，取第一列
        if not ids:
            sys.exit(f"[错误] meter_pool 文件里没有有效 ID: {fpath}")
        return ids
    ids = [int(x) for x in spec.replace(",", " ").split()]
    if not ids:
        sys.exit(f"[错误] meter_pool 解析为空: {spec!r}")
    return ids


def _run_random_rounds(cfg, conf, name, path, results_key, rng, build_payload, dumped):
    """按测试集随机跑 conf['rounds'] 轮，每轮随机电表 + 随机时间子窗口。
    build_payload(meters, start, end) 返回请求体（告警传 meters=None）。"""
    print(f"\n==> {CATEGORY_CN.get(results_key, results_key)}接口 {path}")
    test_set = {"rounds": conf["rounds"], "repeat": conf["repeat"],
                "time_range": [_fmt(conf["time_start"], "start"), _fmt(conf["time_end"], "end")],
                "window_days": list(conf["window_days"])}
    pool = None
    if conf["needs_meters"]:
        pool = conf["_pool"]
        m_lo, m_hi = conf["meters_per_round"]
        m_hi = min(m_hi, len(pool))
        m_lo = min(m_lo, m_hi)
        test_set.update({"pool_size": len(pool), "meters_per_round": [m_lo, m_hi]})
        print(f"  测试集: {len(pool)} 个电表随机抽 {m_lo}~{m_hi} 个 / 时间范围 "
              f"{test_set['time_range'][0]} ~ {test_set['time_range'][1]} 随机取 "
              f"{conf['window_days'][0]}~{conf['window_days'][1]} 天子窗口 / {conf['rounds']} 轮")
    else:
        print(f"  测试集: 时间范围 {test_set['time_range'][0]} ~ {test_set['time_range'][1]} "
              f"随机取 {conf['window_days'][0]}~{conf['window_days'][1]} 天子窗口 / {conf['rounds']} 轮")

    rounds = []
    w_lo, w_hi = conf["window_days"]
    for i in range(1, conf["rounds"] + 1):
        days = rng.randint(w_lo, w_hi)
        start, end = _random_window(rng, conf["time_start"], conf["time_end"], days)
        if conf["needs_meters"]:
            k = rng.randint(m_lo, m_hi)
            meters = sorted(rng.sample(pool, k))
            desc = (f"第{i:02d}轮 {start:%Y-%m-%d}~{end:%Y-%m-%d}({days}天) "
                    f"电表{k}个 meterIds={meters}")
            payload = build_payload(meters, start, end)
        else:
            desc = f"第{i:02d}轮 {start:%Y-%m-%d}~{end:%Y-%m-%d}({days}天)"
            payload = build_payload(None, start, end)
        rounds.append(_run_round(cfg, name, path, payload, conf["repeat"], desc, dumped))
    return {"path": path, "test_set": test_set, "rounds": rounds}


def _abspath(p):
    """相对路径按 api_bench 目录解析。"""
    if not p:
        return p
    return p if os.path.isabs(p) else os.path.join(_HERE, p)


def _fmt_elapsed(sec):
    if sec < 60:
        return f"{sec:.1f} 秒"
    m, s = divmod(int(sec), 60)
    if m < 60:
        return f"{m}分{s}秒"
    h, m = divmod(m, 60)
    return f"{h}小时{m}分{s}秒"


def _finish(results, out, run_start):
    """收尾：记录总耗时、写结果文件、打印总耗时。"""
    total = time.time() - run_start
    results["total_seconds"] = round(total, 1)
    results["finished_at"] = datetime.now().isoformat(timespec="seconds")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    print(f"\n✓ 压测结果（含每轮请求参数，可用于 --replay 复测对比）已写入 {out}")
    print(f"⏱ 本次压测总耗时：{_fmt_elapsed(total)}")
    return results


def bench_cmd(args):
    cfg = load_bench_config(args.config)
    cfg["verbose"] = args.verbose
    if args.delay is not None:
        cfg["delay"] = args.delay
    out = _abspath(args.out)
    seed = args.seed if args.seed is not None else cfg["seed"]
    rng = random.Random(seed)
    run_start = time.time()
    print(f"==> 随机压测（seed={seed}，同种子=同一批随机参数，便于复现/对比）")
    print(f"    目标 {cfg['base_url']}｜单请求超时 {cfg['timeout']:.0f}s"
          f"（[api] timeout，超时会单独标注为'请求超时'而非普通失败）｜结果写入 {os.path.basename(out)}")

    # 提前解析各接口的电表池（auto:N 需要连库）
    for name, conf in cfg["interfaces"].items():
        if args.repeat is not None:
            conf["repeat"] = args.repeat
        if conf["needs_meters"]:
            conf["_pool"] = _resolve_pool(conf["meter_pool_spec"], args)

    results = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "base_url": cfg["base_url"], "seed": seed, "interfaces": {}}

    dumped = set()  # 已打印完整请求详情的接口，避免同一接口刷屏
    order = ["curve", "event", "alarm", "commlog"]
    for name in order:
        conf = cfg["interfaces"].get(name)
        if not conf:
            continue
        path = cfg["context_path"] + INTERFACES[name]
        if name == "curve":
            gids = conf["group_ids"]
            results["interfaces"][name] = _run_random_rounds(
                cfg, conf, name, path, "curve", rng,
                lambda meters, s, e: _curve_payload(gids, s, e, meters), dumped)
        elif name == "event":
            results["interfaces"][name] = _run_random_rounds(
                cfg, conf, name, path, "event", rng,
                lambda meters, s, e: _event_payload(s, e, meters), dumped)
        elif name == "alarm":
            results["interfaces"][name] = _run_random_rounds(
                cfg, conf, name, path, "alarm", rng,
                lambda meters, s, e: _alarm_payload(s, e), dumped)
        else:  # commlog：参数固定，重复测基线
            print(f"\n==> 通讯日志接口 {path}")
            payload = {"pageNum": 1, "pageSize": 100, "conditions": [], "orderByFields": []}
            rounds = [_run_round(cfg, name, path, payload, conf["repeat"],
                                  f"第{i:02d}轮 固定参数基线", dumped) for i in range(1, conf["rounds"] + 1)]
            results["interfaces"][name] = {"path": path,
                                            "test_set": {"rounds": conf["rounds"],
                                                         "repeat": conf["repeat"], "note": "参数固定"},
                                            "rounds": rounds}

    return _finish(results, out, run_start)


def replay_cmd(args):
    """回放：复用上次结果文件里保存的原始请求（同参数、同顺序）重新压测。
    连接设置（base_url/headers/token/timeout/delay）用【当前】配置——转成
    TimescaleDB 后 token 会变、库结构变了，但请求参数不变，这样前后对比才公平。"""
    cfg = load_bench_config(args.config)
    cfg["verbose"] = args.verbose
    if args.delay is not None:
        cfg["delay"] = args.delay
    src = _abspath(args.replay)
    out = _abspath(args.out)
    if not os.path.isfile(src):
        sys.exit(f"[错误] 找不到回放源文件: {src}")
    if os.path.abspath(src) == os.path.abspath(out):
        sys.exit(f"[错误] 回放结果不能写回源文件 {os.path.basename(src)}，"
                 f"用 -o 指定另一个文件（如 -o after.json）")
    old = json.load(open(src, encoding="utf-8"))

    run_start = time.time()
    print(f"==> 回放模式：复用 {os.path.basename(src)} 里的原始请求，同参数复测（转换前后对比用）")
    print(f"    连接用当前配置 {cfg['base_url']}｜超时 {cfg['timeout']:.0f}s｜结果写入 {os.path.basename(out)}")

    results = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "base_url": cfg["base_url"], "seed": old.get("seed"),
               "replay_of": os.path.basename(src), "interfaces": {}}
    dumped = set()
    for name, itf in old.get("interfaces", {}).items():
        old_rounds = itf.get("rounds", [])
        print(f"\n==> 回放{CATEGORY_CN.get(name, name)}接口（{len(old_rounds)} 轮，路径 {itf.get('path','?')}）")
        new_rounds = []
        for r in old_rounds:
            rp = r.get("request_path")
            if not rp and r.get("request_url"):   # 兼容没存 request_path 的旧文件
                rp = r["request_url"].split(old.get("base_url", ""), 1)[-1]
            if not rp or "request_payload" not in r:
                print(f"  [跳过] 「{r.get('round','?')}」缺 request_path/payload，无法回放")
                continue
            repeat = args.repeat if args.repeat is not None else r.get("repeat", 1)
            new_rounds.append(_run_round(cfg, name, rp, r["request_payload"],
                                          repeat, r.get("round", ""), dumped))
        results["interfaces"][name] = {"path": itf.get("path"),
                                        "test_set": itf.get("test_set", {}),
                                        "rounds": new_rounds}
    return _finish(results, out, run_start)


# ── report：生成 Markdown 报告 ────────────────────────────────────────────────

def report_cmd(args):
    resfile = _abspath(args.out)
    if not os.path.isfile(resfile):
        sys.exit(f"[错误] 缺少结果文件 {resfile}，先跑 bench（或直接 all）")
    results = json.load(open(resfile, encoding="utf-8"))
    stats = json.load(open(STATS_FILE, encoding="utf-8")) if os.path.isfile(STATS_FILE) else None
    # 报告文件名跟随结果文件名：after.json → after.md（default → REPORT.md）
    report_file = REPORT_FILE if os.path.basename(resfile) == "bench_results.json" \
        else os.path.splitext(resfile)[0] + ".md"

    total_line = (f"- 本次压测总耗时：{_fmt_elapsed(results['total_seconds'])}"
                  if results.get("total_seconds") is not None else None)
    lines = [
        "# HES 三大类查询接口性能压测报告",
        "",
        f"- 压测执行时间：{results['generated_at']}",
        f"- 目标环境：`{results['base_url']}`",
        f"- 随机种子：{results.get('seed', '?')}（同种子=同一批随机参数，转换前后用同种子跑即可对比）",
        "",
        "## 一、数据量级说明",
        "",
    ]
    extra = [x for x in (
        total_line,
        (f"- 回放来源：`{results['replay_of']}`（同参数复测）" if results.get("replay_of") else None),
    ) if x]
    lines[5:5] = extra
    if stats:
        lines[3:3] = [f"- 数据统计时间：{stats['generated_at']}"
                      f"（{'精确 COUNT' if stats.get('exact') else '近似估算'}）"]
        grand_total = 0
        for cat in ("curve", "event", "log"):
            c = stats["categories"].get(cat)
            if not c:
                continue
            grand_total += c["total"]
            lines += [f"### {CATEGORY_CN[cat]}类（{c['table_count']} 张表）", "",
                      "| 表 | 行数 | 数据时间范围 |", "|---|---:|---|"]
            for t, d in c["tables"].items():
                if d.get("rows") is None:
                    lines.append(f"| `{t}` | 统计失败 | {d.get('error','')} |")
                else:
                    lines.append(f"| `{t}` | {d['rows']:,} | {d.get('min_time','?')} ~ {d.get('max_time','?')} |")
            lines += ["", f"**{CATEGORY_CN[cat]}类合计 {c['total']:,} 行，平均 {c['avg']:,} 行/表**"
                          f"（整体时间范围 {c.get('time_min')} ~ {c.get('time_max')}）", ""]
        lines += [f"三大类总数据量 **{grand_total:,} 行**。", ""]
    else:
        lines += ["_（未找到 stats.json，单独运行 `api_bench.py stats` 生成数据量统计）_", ""]

    lines += ["## 二、接口随机压测结果", ""]
    if_cn = {"curve": "曲线数据", "event": "事件数据", "alarm": "告警事件", "commlog": "通讯日志"}
    for key, cn in if_cn.items():
        itf = results["interfaces"].get(key)
        if not itf:
            continue
        ts = itf.get("test_set", {})
        lines += [f"### {cn}接口", "", f"`{itf['path']}`", ""]
        # 测试集描述
        set_bits = []
        if "pool_size" in ts:
            mpr = ts.get("meters_per_round", [])
            set_bits.append(f"电表集合 {ts['pool_size']} 个，每轮随机抽 {mpr[0]}~{mpr[1]} 个")
        if "time_range" in ts:
            wd = ts.get("window_days", [])
            set_bits.append(f"时间范围 {ts['time_range'][0]} ~ {ts['time_range'][1]}，"
                            f"每轮随机取 {wd[0]}~{wd[1]} 天子窗口")
        if ts.get("note"):
            set_bits.append(ts["note"])
        set_bits.append(f"{ts.get('rounds','?')} 轮 × 每轮重复 {ts.get('repeat','?')} 次")
        lines += [f"**测试集**：{'；'.join(set_bits)}。",
                  "> 首次(冷)=第一次请求、未命中后端缓存，最贴近实际操作；重复均值/P95=缓存热身后的查询。两者差距大即后端有缓存。", "",
                  "| 轮次参数 | 成功率 | 首次(冷,ms) | 重复均值(ms) | P95(ms) | 最大(ms) | 接口返回总条数 |",
                  "|---|---|---:|---:|---:|---:|---:|"]
        for r in itf["rounds"]:
            succ = f"{r['success']}/{r['repeat']}"
            total = f"{r['resp_total']:,}" if r.get("resp_total") is not None else "—"
            cold = r.get("cold_ms", r["avg_ms"])
            lines.append(f"| {r['round']} | {succ} | {cold} | {r['avg_ms']} "
                         f"| {r['p95_ms']} | {r['max_ms']} | {total} |")
            if r.get("errors"):
                lines.append(f"| ↳ 错误 | {'; '.join(str(e) for e in r['errors'])} ||||||")
        # 该接口的汇总
        oks = [r for r in itf["rounds"] if r["success"] > 0]
        if oks:
            colds = [r.get("cold_ms", r["avg_ms"]) for r in oks]
            avgs = [r["avg_ms"] for r in oks]
            p95s = [r["p95_ms"] for r in oks]
            lines += ["",
                      f"小结：{len(itf['rounds'])} 轮，**首次(冷)平均 {round(statistics.mean(colds),1)}ms**"
                      f"（贴近实际操作），重复均值 {round(statistics.mean(avgs),1)}ms，"
                      f"最慢一轮 P95 {round(max(p95s),1)}ms。"]
        # 失败请求详情：报错信息 + 接口 + 参数，方便照着调试定位
        failed = [r for r in itf["rounds"] if r["success"] < r["repeat"] and r.get("request_url")]
        if failed:
            lines += ["", "**失败请求详情**（可照此复现定位）：", ""]
            for r in failed:
                lines += [
                    f"- {r['round']}｜原因：{'; '.join(str(e) for e in r['errors'])}",
                    f"    - 接口：`POST {r['request_url']}`",
                    f"    - 参数：`{json.dumps(r['request_payload'], ensure_ascii=False)}`",
                ]
        lines.append("")

    lines += [
        "## 三、说明",
        "",
        "- **测试集与随机方式**：每个接口在配置文件里指定一个「电表集合 + 时间范围」作为测试集；"
        "每一轮从电表集合里随机抽若干个电表、在时间范围内随机取一段子窗口，组成请求参数。"
        "抽多少电表、子窗口多宽、跑多少轮都在配置里控制。曲线接口的 `groupIds` 全程保持不变；"
        "告警接口只随机时间子窗口（无电表参数）；通讯日志接口参数固定，测的是重复请求基线。",
        "- **可复现**：随机由固定种子（seed）驱动，同一份配置 + 同一个种子每次生成完全相同的"
        "参数序列。**转换前后用同一个种子各跑一遍，两份报告逐轮一一对应，直接对比时延即可。**",
        "- **指标口径 / 缓存**：每轮同参数请求 N 次。**首次(冷)** = 第一次请求、未命中"
        "后端缓存，最贴近\"实际操作\"的真实耗时；**重复均值/P95** = 缓存热身后的查询，"
        "通常明显更快。两者差距大说明后端有缓存——想看真实慢查询看首次(冷)那列。"
        "想每次都测冷查询，把各接口的 `repeat` 设为 1、并让每轮参数不重复。"
        "\"接口返回总条数\"取自响应分页 total 字段（提取不到则为 —），可与第一节数据量级对照。",
    ]

    with open(report_file, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"✓ 报告已生成: {report_file}")


# ── compare：转换前后两份结果对比 ─────────────────────────────────────────────

def _cold(r):
    """一轮的首次(冷)耗时，兼容旧文件退回 avg_ms。"""
    return r.get("cold_ms", r.get("avg_ms"))


def _round_ok(r):
    return r.get("success", 0) >= r.get("repeat", 1) and r.get("success", 0) > 0


def compare_cmd(args):
    if not args.before or not args.after:
        sys.exit("[错误] compare 需要两份结果：--before before.json --after after.json")
    bfile, afile = _abspath(args.before), _abspath(args.after)
    for f in (bfile, afile):
        if not os.path.isfile(f):
            sys.exit(f"[错误] 找不到文件: {f}")
    B = json.load(open(bfile, encoding="utf-8"))
    A = json.load(open(afile, encoding="utf-8"))
    out = _abspath(args.out) if args.out != "bench_results.json" else _abspath("compare.md")

    if_cn = {"curve": "曲线数据", "event": "事件数据", "alarm": "告警事件", "commlog": "通讯日志"}
    lines = [
        "# HES 接口性能对比报告：转换前 vs 转换后",
        "",
        f"- 转换前（普通表）：`{os.path.basename(bfile)}`"
        f"（{B.get('generated_at','?')}，总耗时 {_fmt_elapsed(B['total_seconds']) if B.get('total_seconds') else '?'}）",
        f"- 转换后（TimescaleDB）：`{os.path.basename(afile)}`"
        f"（{A.get('generated_at','?')}，总耗时 {_fmt_elapsed(A['total_seconds']) if A.get('total_seconds') else '?'}）",
        "- 对比口径：**首次(冷)耗时**（未命中后端缓存，最贴近实际操作）。两份用同一批请求"
        "（`--replay`）逐轮一一对应。提速倍数 = 前/后，>1 表示转换后更快。",
        "",
    ]

    overall_b, overall_a = 0.0, 0.0   # 全局时间累加（只算两边都成功的轮）
    for name, cn in if_cn.items():
        bi, ai = B.get("interfaces", {}).get(name), A.get("interfaces", {}).get(name)
        if not bi or not ai:
            continue
        # 按请求参数配对（回放后逐轮 payload 完全一致）；配不上退回按序号
        a_by_key = {json.dumps(r.get("request_payload"), ensure_ascii=False, sort_keys=True): r
                    for r in ai.get("rounds", [])}
        lines += [f"## {cn}接口", "",
                  "| 轮次参数 | 命中条数 | 前·首次(冷)ms | 后·首次(冷)ms | 提速 | 变化 |",
                  "|---|---:|---:|---:|---:|---:|"]
        pair_b, pair_a = [], []
        for idx, rb in enumerate(bi.get("rounds", [])):
            key = json.dumps(rb.get("request_payload"), ensure_ascii=False, sort_keys=True)
            ra = a_by_key.get(key)
            if ra is None:
                arounds = ai.get("rounds", [])
                ra = arounds[idx] if idx < len(arounds) else None
            total = rb.get("resp_total")
            total_s = f"{total:,}" if total is not None else "—"
            if ra is None:
                lines.append(f"| {rb.get('round','?')} | {total_s} | {_cold(rb)} | 无对应轮 | — | — |")
                continue
            cb, ca = _cold(rb), _cold(ra)
            if not _round_ok(rb) or not _round_ok(ra) or not cb or not ca:
                flag = "前失败" if not _round_ok(rb) else ("后失败" if not _round_ok(ra) else "")
                lines.append(f"| {rb.get('round','?')} | {total_s} | {cb}{'⚠' if not _round_ok(rb) else ''} "
                             f"| {ca}{'⚠' if not _round_ok(ra) else ''} | — | {flag} |")
                continue
            speed = f"{cb/ca:.1f}x" if ca > 0 else "—"
            pct = f"{(ca-cb)/cb*100:+.0f}%" if cb > 0 else "—"
            lines.append(f"| {rb.get('round','?')} | {total_s} | {cb:.0f} | {ca:.0f} | {speed} | {pct} |")
            pair_b.append(cb); pair_a.append(ca)
        # 接口小结
        if pair_b:
            sb, sa = sum(pair_b), sum(pair_a)
            overall_b += sb; overall_a += sa
            faster = sum(1 for x, y in zip(pair_b, pair_a) if y < x)
            mb, ma = statistics.mean(pair_b), statistics.mean(pair_a)
            verdict = (f"整体提速 **{sb/sa:.1f}x**" if sa > 0 and sa < sb
                       else (f"整体变慢 {sa/sb:.1f}x" if sa > sb else "基本持平"))
            lines += ["",
                      f"小结：{len(pair_b)} 轮有效对比，首次(冷)平均 {mb:.0f}ms → {ma:.0f}ms，"
                      f"{verdict}（{faster}/{len(pair_b)} 轮变快）。", ""]
        else:
            lines += ["", "小结：无可对比的成功轮次（两边有失败/超时，见上表标记）。", ""]

    # 全局结论放最前面
    if overall_a > 0:
        headline = (f"**总体：转换后按首次(冷)总耗时提速 {overall_b/overall_a:.1f} 倍**"
                    f"（转换前累计 {overall_b/1000:.1f}s → 转换后 {overall_a/1000:.1f}s，"
                    f"仅统计两边都成功的轮次）。")
    else:
        headline = "**总体：没有两边都成功的可对比轮次。**"
    lines.insert(6, headline)
    lines.insert(7, "")

    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"✓ 对比报告已生成: {out}")
    print("  " + headline.replace("**", ""))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["stats", "meters", "bench", "report", "compare", "all"])
    p.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="接口/压测配置（默认 bench.ini）")
    p.add_argument("--env-file", help="数据库连接 db.env 路径（默认同目录，统计数据量/auto:N 电表池用）")
    p.add_argument("--sample-days", type=int, default=0,
                   help="meters 命令：默认 0=扫整个配置时间窗口（与压测一致，推荐）；"
                        "设 N>0 只扫末尾 N 天提速，但分析窗口会与测试窗口不一致")
    p.add_argument("--top", type=int, default=1000,
                   help="meters 命令：每类保留数据量最多的前 N 个电表（默认 1000，设 0 保留全部活跃电表）")
    p.add_argument("--approx", action="store_true",
                   help="数据量用 pg_class.reltuples 近似值（大表秒出，误差通常 <1%%）")
    p.add_argument("--repeat", type=int, help="覆盖配置里各接口的每轮重复请求次数")
    p.add_argument("--seed", type=int, help="覆盖配置里的随机种子（同种子=同一批随机参数）")
    p.add_argument("-v", "--verbose", "--show-params", dest="verbose", action="store_true",
                   help="每轮打印【请求参数 + 响应内容 + 一键复现 curl（带计时）+ 每次请求单独耗时】，"
                        "用于核对参数/响应、以及排查'时间偏快'（看首次 vs 后续是否缓存）")
    p.add_argument("--delay", type=float,
                   help="每次请求之间的间隔秒数，缓解连续猛打把后端打满（503）；覆盖配置里的 delay")
    p.add_argument("-o", "--out", default="bench_results.json",
                   help="结果文件（bench/replay 写入、report 读取；默认 bench_results.json）。"
                        "转换前后各存一份，如 -o before.json / -o after.json")
    p.add_argument("--replay", metavar="FILE",
                   help="回放模式：读取上次结果文件里保存的原始请求，同参数复测（转成 TimescaleDB "
                        "后用它复测对比）。连接/token 用当前配置，请求参数用文件里的。需配合 -o 指定新输出")
    p.add_argument("--before", metavar="FILE", help="compare 命令：转换前的结果文件")
    p.add_argument("--after", metavar="FILE", help="compare 命令：转换后的结果文件")
    args = p.parse_args()

    if args.command == "stats":
        stats_cmd(args)
    elif args.command == "meters":
        meters_cmd(args)
    elif args.command == "bench":
        replay_cmd(args) if args.replay else bench_cmd(args)
    elif args.command == "report":
        report_cmd(args)
    elif args.command == "compare":
        compare_cmd(args)
    else:
        stats_cmd(args)
        bench_cmd(args)
        report_cmd(args)


if __name__ == "__main__":
    main()
