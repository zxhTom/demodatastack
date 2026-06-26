#!/usr/bin/env bash
# seed_meter_data.sh — 为 12 张负荷/抄表曲线表生成种子数据
#
# 用法示例：
#   ./seed_meter_data.sh --start 2025-01-01 --end 2025-01-31
#   ./seed_meter_data.sh --start 2025-01-01 --days 7 --mode rebuild --yes
#   ./seed_meter_data.sh --start 2025-01-01 --end 2025-01-31 --dry-run
#   ./seed_meter_data.sh --start 2025-01-01 --end 2025-01-31 --sql-out out.sql
#   ./seed_meter_data.sh --start 2025-01-01 --end 2025-01-31 \
#       --meters 14213,14215 --tables d_load_voltage,d_load_power
#
# 参数：
#   --start DATE          开始日期 YYYY-MM-DD
#   --end   DATE          结束日期 YYYY-MM-DD
#   --days  N             时间跨度（天），配合 --start 或 --end 使用
#   --mode  rebuild|overwrite|fill   默认 fill
#   --meters ID,...       逗号分隔的 meter_id，默认 c_meter 全部
#   --tables NAME,...     逗号分隔的表名，默认全部 12 张
#   --profile-id N        默认 1
#   --batch-size N        默认 2000
#   --env-file PATH       db.env 路径，默认脚本同目录下的 db.env
#   --dry-run             只打印预计行数，不连库写入
#   --sql-out FILE        将 INSERT/DELETE 语句写入文件，不直接写库
#   --yes                 rebuild 模式删除确认

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$SCRIPT_DIR/seed_meter_data.py" "$@"
