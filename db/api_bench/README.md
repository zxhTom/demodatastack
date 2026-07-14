# api_bench —— HES 三大类查询接口性能压测

对应 `db/req.md` 的需求：统计曲线 / 事件 / 通讯日志三大类的数据量级，对 4 个查询
接口做多轮**随机**压测，最终生成一份 Markdown 报告。

**压测模型**：你为每个接口指定一个「测试集」= 一个电表集合（`meter_pool`）+ 一个
时间范围（`time_start`~`time_end`）。每一轮从电表集合里**随机**抽若干个电表、在
时间范围内**随机**取一段子窗口，组成请求参数去打接口。抽多少电表、子窗口多宽、
跑多少轮都在配置里控制；随机由固定种子驱动，同种子可完全复现（转换前后对比用）。

**关于耗时（重要）**：每轮同参数请求 `repeat` 次，输出**两个**时间——
`首次(冷)`=第一次请求、未命中后端缓存，最贴近你实际操作的真实耗时；
`重复均值/P95`=缓存热身后的查询，通常明显更快。**如果你觉得脚本报的时间比实际快，
看"首次(冷)"那个数**——差距大就是后端有缓存。想每次都测冷查询，把 `repeat` 设为 1。
加 `-v` 可看每次请求的单独耗时、完整请求参数、响应内容和一键复现 curl（带计时）。

## 快速开始

```bash
cd db/api_bench
cp bench.example.ini bench.ini
vim bench.ini          # 填 base_url、认证头，以及每个接口的测试集（电表集合 + 时间范围）
                        # 数据库连接沿用 db.env 机制（默认 eco_ma，可放 ENC(...) 密文）

python3 api_bench.py all          # 统计 → 压测 → 报告，一条命令
cat REPORT.md
```

分阶段跑：

```bash
python3 api_bench.py stats --approx   # 只统计数据量（纯 PG reltuples 估算，秒出；
                                       # 去掉 --approx 用精确 COUNT，亿级表会很慢）
python3 api_bench.py meters           # 分析 curve/event 命中数据多的活跃电表，写文件供 meter_pool 引用
python3 api_bench.py bench             # 只压测（不依赖 stats）
python3 api_bench.py bench --seed 123  # 覆盖随机种子
python3 api_bench.py bench --repeat 10 # 覆盖每轮重复请求次数
python3 api_bench.py bench -v           # 每轮打印 请求参数+响应内容+一键复现curl(带计时)+每次单独耗时
python3 api_bench.py bench --delay 0.5  # 请求间隔 0.5s，缓解连续猛打导致的后端 503
python3 api_bench.py bench -o before.json          # 结果（含每轮请求参数）存到指定文件
python3 api_bench.py bench --replay before.json -o after.json  # 用上次的请求同参数复测
python3 api_bench.py report -o after.json          # 对指定结果文件出报告（after.md）
```

### 转 TimescaleDB 前后对比（同参数复测，最有说服力）

结果文件里保存了**每一轮的完整请求参数**，所以转换前后可以用**完全相同的请求**对比：

```bash
python3 api_bench.py bench -o before.json          # 1. 普通表下压测，存 before.json
#    → 用 db/timescale_tools/table_convert.py 转成 TimescaleDB 超表
python3 api_bench.py bench --replay before.json -o after.json  # 2. 同参数复测，存 after.json
python3 api_bench.py compare --before before.json --after after.json  # 3. 一键生成对比报告 compare.md
```

`--replay` 只复用文件里的**请求参数**（接口、meterIds、时间窗口全部一致）；连接地址、
token、超时用的是**当前** `bench.ini`（转换后 token 会变，但库是同一个）。before/after
逐轮一一对应，直接比「首次(冷)」耗时即可。每次结果末尾也会打印**本次压测总耗时**。

**`compare` 对比报告**（`compare.md`）逐轮列出：命中条数、转换前首次(冷)、转换后首次(冷)、
提速倍数、变化百分比；每个接口给小结，顶部给总体结论（如"转换后总耗时提速 8.3 倍"）。
只统计两边都成功的轮次，某轮超时/失败会单独标记不计入提速。`-o` 可改对比报告文件名。
```

## 覆盖的接口与测试集

| 接口 | 每轮随机什么 | 测试集配置段 |
|---|---|---|
| 曲线 `/hes-web-api/api/profile/profileLog/page` | 随机电表 + 随机时间子窗口（`groupIds` 不变） | `[curve]` |
| 事件 `/hes-web-api/api/event/eventLog/page` | 随机电表 + 随机时间子窗口 | `[event]` |
| 告警 `/api/meters/alarmEnventDeviceListPage` | 只随机 `conditions` 里的时间段 | `[alarm]` |
| 通讯日志 `/api/communication/listPage` | 参数固定，重复请求测基线 | `[commlog]` |

只测配置里写了对应 `[段]` 的接口——不想测某个接口，删掉它的段即可。

**电表集合（`meter_pool`）**三种写法：`file:xxx_meters.txt`（用 `api_bench.py meters`
分析出的命中数据多的活跃电表，**最推荐**）、`auto:N`（`c_meter` 前 N 个，不保证有数据）、
或显式列表 `14232,15831,15833`。**时间范围**用 `YYYY-MM-DD`（或带时分秒）。
每轮随机抽取的电表数（`meters_per_round`）和子窗口天数（`window_days`）写成区间
如 `2-20` / `1-7`，也可写单值固定。这些都可在 `[bench]` 设默认、在各接口段单独覆盖。

### 先跑 meters 选对电表（性能测试关键）

随机抽到"没数据的电表"→ 查询秒回 → 测不出接口真实耗时。`meters` 命令扫库找出
在配置时间范围内**命中数据最多**的活跃电表，按数据量降序写入文件：

```bash
python3 api_bench.py meters                 # 默认扫【每个模块配置的完整时间窗口】，与压测一致
python3 api_bench.py meters --top 500        # 每类只保留数据量最多的前 500 个
python3 api_bench.py meters --sample-days 30 # 提速快捷方式：只扫末尾 30 天（⚠分析窗口会和测试窗口不一致）
# 然后把对应 [段] 的 meter_pool 改成 file:xxx_meters.txt
```

**分析用的时间窗口就是各模块 `[段]` 里配置的 `time_start~time_end`**——必须和压测
实际查询的窗口一致，否则筛出的活跃电表在测试查的时间段里未必有数据。改了模块的
时间范围就要重跑 `meters`。

只有 curve/event 接口按 meterIds 过滤，所以只分析这两类；alarm 只按时间、commlog
参数固定，选电表对它俩无影响。事件类电表数据量差异极大（同一窗口内中位十几条
vs 最多上千条/电表，差近百倍），选对电表尤其关键。

## 输出文件（均已 gitignore，不进版本库）

| 文件 | 内容 |
|---|---|
| `stats.json` | 23 张表逐表行数/时间范围 + 三大类合计与平均 |
| `bench_results.json` | 每接口的测试集 + 每轮参数/成功率/avg/P50/P95/max 时延/接口返回总条数 |
| `REPORT.md` | 最终报告：数据量级说明 + 各接口测试集与随机压测结果表 + 口径说明 |

## 当前库的数据量级（2026-07 实测，`--approx` reltuples 估算）

| 类别 | 表数 | 合计 | 平均/表 |
|---|---:|---:|---:|
| 曲线 | 12 | 9.25 亿 | 7,710 万 |
| 事件 | 10 | 1.49 亿 | 1,495 万 |
| 通讯日志 | 1 | 7,461 万 | 7,461 万 |

## 转换前 / 转换后对比（无 TimescaleDB 依赖）

数据量统计不使用任何 TimescaleDB 专有函数，`--approx` 走的是纯 PostgreSQL 的
`pg_class.reltuples` 沿继承链汇总：

- **普通表**：没有子表，取它自己的 reltuples 估算值。
- **超表**：数据落在继承自父表的 chunk 里，脚本会把各 chunk 的估算值相加。

转换前后是同一套逻辑、同一份口径，可以直接对比。做法：

压测本身也无 TimescaleDB 依赖，随机参数由固定种子驱动，**转换前后用同一个种子
各跑一遍，两份报告逐轮参数一一对应，直接对比时延即可**。做法：

```bash
# 1. 转换前：普通表状态下跑一遍，报告另存（seed 用配置里的固定值，别改）
python3 api_bench.py all
mv REPORT.md REPORT_before.md ; mv stats.json stats_before.json ; mv bench_results.json bench_before.json

# 2. 用 db/timescale_tools/table_convert.py 把表转成超表
#    （转完建议先 ANALYZE 各表，reltuples 才准；精确对比可直接用不带 --approx 的 COUNT）

# 3. 转换后：再跑一遍（同一份 bench.ini、同一个 seed），对比两份报告
python3 api_bench.py all
mv REPORT.md REPORT_after.md
```

> 追求精确、可复现的对比时，两次都别加 `--approx`，用精确 `COUNT(*)`——虽然
> 亿级表慢，但转换前后的行数应当完全一致，正好用来验证转换没丢数据；接口时延
> 才是真正要对比的指标。

## 注意

- 压测会对生产接口产生真实查询压力（亿级表的宽时间窗口查询可能很重），
  在业务低峰执行，`repeat`/窗口天数从小往大加。
- 认证头写在 `bench.ini`（已 gitignore）；数据库密码可用 pg-cdc-agent 的
  `encrypt-password` 生成 `ENC(...)` 密文放进 `db.env`。
- 脚本只依赖 `psycopg2` + 标准库（HTTP 用 urllib），无需额外安装。
