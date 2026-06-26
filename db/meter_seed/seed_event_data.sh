#!/usr/bin/env bash
# seed_event_data.sh — 为 10 张事件日志表生成种子数据
#
# 用法示例：
#   ./seed_event_data.sh --start 2025-01-01 --end 2025-06-30
#   ./seed_event_data.sh --start 2025-01-01 --end 2025-06-30 --count 500
#   ./seed_event_data.sh --start 2025-01-01 --end 2025-06-30 --dry-run
#   ./seed_event_data.sh --start 2025-01-01 --end 2025-06-30 --sql-out out.sql
#   ./seed_event_data.sh --start 2025-01-01 --end 2025-06-30 \
#       --meters 14213,14215 --tables d_alarm_event,d_power_failure_event_log
#
# 参数：
#   --start DATE          开始日期 YYYY-MM-DD（必填）
#   --end   DATE          结束日期 YYYY-MM-DD（必填，含）
#   --count N             每张表生成的条数，默认 1000
#   --meters ID,...       逗号分隔的 meter_id，默认 c_meter 全部
#   --tables NAME,...     逗号分隔的表名，默认全部 10 张
#   --batch-size N        默认 500
#   --seed N              随机种子，默认 42
#   --env-file PATH       db.env 路径，默认脚本同目录下的 db.env
#   --dry-run             只打印预计行数，不连库写入
#   --sql-out FILE        将 INSERT 语句写入文件，不直接写库

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$SCRIPT_DIR/seed_event_data.py" "$@"
